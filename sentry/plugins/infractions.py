import csv
import io
import asyncio
import humanize
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands

from sentry.util.input import parse_duration
from sentry.types import Field, snowflake
from sentry.types.plugin import PluginConfig
from sentry.plugins.modlog import Actions
from sentry.models.user import User, Infraction
from sentry.models.guild import GuildMemberBackup, GuildBan
from sentry.constants import (
    GREEN_TICK_EMOJI_ID, RED_TICK_EMOJI_ID, GREEN_TICK_EMOJI, RED_TICK_EMOJI
)

def clamp(string, size):
    if len(string) > size:
        return string[:size] + '...'
    return string

def maybe_string(obj, exists, notexists, **kwargs):
    if obj:
        return exists.format(o=obj, **kwargs)
    return notexists.format(**kwargs)

class InfractionsConfig(PluginConfig):
    confirm_actions = Field(bool, default=True)
    confirm_actions_reaction = Field(bool, default=False)
    confirm_actions_expiry = Field(int, default=0)
    notify_actions = Field(bool, default=False)
    mute_role = Field(snowflake, default=None)
    # Sentry standard levels: 100=Admin, 50=Mod
    reason_edit_level = Field(int, default=100) 

class InfractionsPlugin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.recalculate_event = asyncio.Event()
        self.infraction_task = self.bot.loop.create_task(self.infraction_loop())

    async def cog_unload(self):
        self.infraction_task.cancel()

    def queue_infractions(self):
        # Trigger the event to wake up the background loop immediately
        self.recalculate_event.set()

    async def infraction_loop(self):
        await self.bot.wait_until_ready()
        
        while not self.bot.is_closed():
            self.recalculate_event.clear()
            
            def get_next():
                return list(Infraction.select().where(
                    (Infraction.active == 1) &
                    (~(Infraction.expires_at >> None))
                ).order_by(Infraction.expires_at.asc()).limit(1))

            next_infraction_list = await asyncio.to_thread(get_next)
            
            if not next_infraction_list:
                # No active infractions, wait until one is added
                await self.recalculate_event.wait()
                continue
                
            next_infraction = next_infraction_list[0]
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            
            if next_infraction.expires_at > now:
                sleep_seconds = (next_infraction.expires_at - now).total_seconds()
                try:
                    # Sleep until expiration, UNLESS recalculate_event is triggered
                    await asyncio.wait_for(self.recalculate_event.wait(), timeout=sleep_seconds)
                    # If we didn't timeout, a new infraction was added. Loop restarts.
                    continue
                except asyncio.TimeoutError:
                    # Timeout reached, time to clear the infraction
                    pass
            
            await self.clear_infractions()

    async def clear_infractions(self):
        def get_expired():
            return list(Infraction.select().where(
                (Infraction.active == 1) &
                (Infraction.expires_at < datetime.now(timezone.utc).replace(tzinfo=None))
            ))
            
        expired = await asyncio.to_thread(get_expired)
        
        for item in expired:
            guild = self.bot.get_guild(item.guild_id)
            if not guild:
                continue

            type_ = {i.index: i for i in Infraction.Types.attrs}[item.type_]
            modlog = self.bot.get_cog('ModLogPlugin')

            if type_ == Infraction.Types.TEMPBAN:
                if modlog:
                    await modlog.create_debounce(guild.id, ['on_member_unban'], user_id=item.user_id)
                    
                try:
                    await guild.unban(discord.Object(id=item.user_id))
                except discord.HTTPException:
                    pass
                    
                if modlog:
                    await modlog.log_action_ext(
                        Actions.MEMBER_TEMPBAN_EXPIRE,
                        guild.id,
                        user_id=item.user_id,
                        user=str(self.bot.get_user(item.user_id) or item.user_id),
                        inf=item
                    )
                    
            elif type_ in (Infraction.Types.TEMPMUTE, Infraction.Types.TEMPROLE):
                member = guild.get_member(item.user_id)
                role_id = item.metadata.get('role')
                
                if member and role_id:
                    role = guild.get_role(role_id)
                    if role and role in member.roles:
                        if modlog:
                            await modlog.create_debounce(guild.id, ['on_member_update'], user_id=item.user_id, role_id=role_id)
                        
                        try:
                            await member.remove_roles(role)
                        except discord.HTTPException:
                            pass
                            
                        if modlog:
                            await modlog.log_action_ext(
                                Actions.MEMBER_TEMPMUTE_EXPIRE,
                                guild.id,
                                member=member,
                                inf=item
                            )
                elif not member and role_id:
                    await asyncio.to_thread(GuildMemberBackup.remove_role, item.guild_id, item.user_id, role_id)

            def mark_inactive():
                item.active = False
                item.save()
            await asyncio.to_thread(mark_inactive)
            
        # Re-trigger loop to check for more
        self.queue_infractions()

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        pre_roles = set(before.roles)
        post_roles = set(after.roles)
        if pre_roles == post_roles:
            return
            
        removed = pre_roles - post_roles
        
        core = self.bot.get_cog('CorePlugin')
        if not core: return
        guild_config = core.get_config(after.guild.id)
        if not guild_config or not hasattr(guild_config.plugins, 'infractions'):
            return
            
        mute_role_id = guild_config.plugins.infractions.mute_role
        
        # If the user was unmuted, mark any temp-mutes as inactive
        if any(r.id == mute_role_id for r in removed):
            # Pass a dummy object mirroring disco event config access if needed by clear_active,
            # or directly handle it depending on how models.py handles clear_active.
            await asyncio.to_thread(Infraction.clear_active, None, after.id, [Infraction.Types.TEMPMUTE])

    @commands.Cog.listener()
    async def on_member_unban(self, guild, user):
        await asyncio.to_thread(Infraction.clear_active, None, user.id, [Infraction.Types.BAN, Infraction.Types.TEMPBAN])

    def is_mod(self, ctx):
        core = self.bot.get_cog('CorePlugin')
        if not core: return False
        return core.get_level(ctx.guild.id, ctx.author) >= 50

    def is_admin(self, ctx):
        core = self.bot.get_cog('CorePlugin')
        if not core: return False
        return core.get_level(ctx.guild.id, ctx.author) >= 100

    def can_act_on(self, ctx, victim_id, throw=True):
        if ctx.author.id == victim_id:
            if not throw:
                return False
            raise commands.CommandError('cannot execute that action on yourself')
            
        core = self.bot.get_cog('CorePlugin')
        victim_level = core.get_level(ctx.guild.id, ctx.guild.get_member(victim_id)) if core else 0
        actor_level = core.get_level(ctx.guild.id, ctx.author) if core else 0
        
        if actor_level <= victim_level:
            if not throw:
                return False
            raise commands.CommandError('invalid permissions')
        return True

    async def confirm_action(self, ctx, message):
        config = getattr(ctx, 'base_config', None)
        if not config or not hasattr(config.plugins, 'infractions'):
            return
        inf_config = config.plugins.infractions
        
        if not inf_config.confirm_actions:
            return
            
        if inf_config.confirm_actions_reaction:
            try:
                await ctx.message.add_reaction(GREEN_TICK_EMOJI)
            except discord.HTTPException:
                pass
            return
            
        msg = await ctx.send(message)
        if inf_config.confirm_actions_expiry > 0:
            self.bot.loop.call_later(inf_config.confirm_actions_expiry, lambda: asyncio.create_task(msg.delete()))

    @commands.command()
    async def unban(self, ctx, user_id: int, *, reason: str = None):
        if not self.is_mod(ctx): return await ctx.send("Invalid permissions.")
        
        def check_and_log():
            try:
                GuildBan.get(user_id=user_id, guild_id=ctx.guild.id)
            except GuildBan.DoesNotExist:
                return False
                
            Infraction.create(
                guild_id=ctx.guild.id,
                user_id=user_id,
                actor_id=ctx.author.id,
                type_=Infraction.Types.UNBAN,
                reason=reason
            )
            return True

        is_banned = await asyncio.to_thread(check_and_log)
        if not is_banned:
            return await ctx.send(f'user with id `{user_id}` is not banned')
            
        try:
            await ctx.guild.unban(discord.Object(id=user_id), reason=reason)
            await ctx.send(f'unbanned user with id `{user_id}`')
        except discord.HTTPException:
            await ctx.send(f'Failed to unban user `{user_id}` on Discord.')

    @commands.group(invoke_without_command=True)
    async def infractions(self, ctx):
        pass

    @infractions.command(name='archive')
    async def infractions_archive(self, ctx):
        if not self.is_admin(ctx): return await ctx.send("Invalid permissions.")
        
        def generate_csv():
            user = User.alias()
            actor = User.alias()
            q = Infraction.select(Infraction, user, actor).join(
                user,
                on=((Infraction.user_id == user.user_id).alias('user'))
            ).switch(Infraction).join(
                actor,
                on=((Infraction.actor_id == actor.user_id).alias('actor'))
            ).where(Infraction.guild_id == ctx.guild.id)
            
            buff = io.StringIO()
            w = csv.writer(buff)
            for inf in q:
                w.writerow([
                    inf.id,
                    inf.user_id,
                    str(inf.user),
                    inf.actor_id,
                    str(inf.actor),
                    str({i.index: i for i in Infraction.Types.attrs}[inf.type_]),
                    str(inf.reason),
                ])
            return buff.getvalue()

        csv_data = await asyncio.to_thread(generate_csv)
        file = discord.File(io.BytesIO(csv_data.encode('utf-8')), filename='infractions.csv')
        await ctx.send('Ok, here is an archive of all infractions', file=file)

    @infractions.command(name='info')
    async def infraction_info(self, ctx, infraction_id: int):
        if not self.is_mod(ctx): return await ctx.send("Invalid permissions.")
        
        def fetch_inf():
            try:
                user = User.alias()
                actor = User.alias()
                return Infraction.select(Infraction, user, actor).join(
                    user,
                    on=((Infraction.user_id == user.user_id).alias('user'))
                ).switch(Infraction).join(
                    actor,
                    on=((Infraction.actor_id == actor.user_id).alias('actor'))
                ).where(
                    (Infraction.id == infraction_id) &
                    (Infraction.guild_id == ctx.guild.id)
                ).get()
            except Infraction.DoesNotExist:
                return None

        infraction = await asyncio.to_thread(fetch_inf)
        if not infraction:
            return await ctx.send(f'cannot find an infraction with ID `{infraction_id}`')
            
        type_ = {i.index: i for i in Infraction.Types.attrs}[infraction.type_]
        embed = discord.Embed()
        
        if type_ in (Infraction.Types.MUTE, Infraction.Types.TEMPMUTE, Infraction.Types.TEMPROLE):
            embed.color = 0xfdfd96
        elif type_ in (Infraction.Types.KICK, Infraction.Types.SOFTBAN):
            embed.color = 0xffb347
        else:
            embed.color = 0xff6961
            
        embed.title = str(type_).title()
        
        # User method adapting sentry.models.user to get correct URL
        avatar_url = getattr(infraction.user, 'get_avatar_url', lambda: discord.Embed.Empty)()
        if avatar_url and isinstance(avatar_url, str):
            embed.set_thumbnail(url=avatar_url)
            
        embed.add_field(name='User', value=str(infraction.user), inline=True)
        embed.add_field(name='Moderator', value=str(infraction.actor), inline=True)
        embed.add_field(name='Active', value='yes' if infraction.active else 'no', inline=True)
        
        if infraction.active and infraction.expires_at:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            embed.add_field(name='Expires', value=humanize.naturaldelta(infraction.expires_at - now))
            
        embed.add_field(name='Reason', value=infraction.reason or '_No Reason Given_', inline=False)
        embed.timestamp = infraction.created_at
        await ctx.send(embed=embed)

    @infractions.command(name='search')
    async def infraction_search(self, ctx, *, query: str = None):
        if not self.is_mod(ctx): return await ctx.send("Invalid permissions.")
        
        # Handle User/Member mentions cleanly in d.py
        if ctx.message.mentions:
            query = str(ctx.message.mentions[0].id)
            
        def do_search():
            q = (Infraction.guild_id == ctx.guild.id)
            if query and query.isdigit():
                q &= (
                    (Infraction.id == int(query)) |
                    (Infraction.user_id == int(query)) |
                    (Infraction.actor_id == int(query)))
            elif query:
                q &= (Infraction.reason ** f'%{query}%') 

            user = User.alias()
            actor = User.alias()
            return list(Infraction.select(Infraction, user, actor).join(
                user,
                on=((Infraction.user_id == user.user_id).alias('user'))
            ).switch(Infraction).join(
                actor,
                on=((Infraction.actor_id == actor.user_id).alias('actor'))
            ).where(q).order_by(Infraction.created_at.desc()).limit(6))

        infractions_list = await asyncio.to_thread(do_search)
        
        # Borrowing the MessageTable utility from our admin.py port
        from sentry.plugins.admin import MessageTable
        tbl = MessageTable()
        tbl.set_header('ID', 'Created', 'Type', 'User', 'Moderator', 'Active', 'Reason')
        
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        
        for inf in infractions_list:
            type_ = {i.index: i for i in Infraction.Types.attrs}[inf.type_]
            reason = inf.reason or ''
            if len(reason) > 256:
                reason = reason[:256] + '...'
                
            if inf.active:
                active = 'yes'
                if inf.expires_at:
                    active += ' (expires in {})'.format(humanize.naturaldelta(inf.expires_at - now))
            else:
                active = 'no'
                
            tbl.add(
                inf.id,
                inf.created_at.isoformat(),
                str(type_),
                str(inf.user),
                str(inf.actor),
                active,
                clamp(reason, 128)
            )
            
        await ctx.send(tbl.compile())

    @infractions.command(name='duration')
    async def infraction_duration(self, ctx, infraction_id: int, duration: str):
        if not self.is_mod(ctx): return await ctx.send("Invalid permissions.")
        
        def update_duration():
            try:
                inf = Infraction.get(id=infraction_id)
            except Infraction.DoesNotExist:
                return False, 'invalid infraction (try `!infractions recent`)'
                
            if inf.actor_id != ctx.author.id and not self.is_admin(ctx):
                return False, 'only administrators can modify the duration of infractions created by other moderators'
            if not inf.active:
                return False, 'that infraction is not active and cannot be updated'
                
            expires_dt = parse_duration(duration, inf.created_at)
            converted = False
            
            if inf.type_ in [Infraction.Types.MUTE.index, Infraction.Types.BAN.index]:
                inf.type_ = (
                    Infraction.Types.TEMPMUTE.index
                    if inf.type_ == Infraction.Types.MUTE.index else
                    Infraction.Types.TEMPBAN.index
                )
                converted = True
            elif inf.type_ not in [
                    Infraction.Types.TEMPMUTE.index,
                    Infraction.Types.TEMPBAN.index,
                    Infraction.Types.TEMPROLE.index]:
                return False, 'cannot set the duration for that type of infraction'
                
            inf.expires_at = expires_dt
            inf.save()
            return True, (converted, inf.expires_at)

        success, result = await asyncio.to_thread(update_duration)
        if not success:
            return await ctx.send(f":warning: {result}")
            
        converted, expires_dt = result
        self.queue_infractions()
        
        if converted:
            await ctx.send(f":ok_hand: ok, I've made that infraction temporary, it will now expire on {expires_dt.isoformat()}")
        else:
            await ctx.send(f":ok_hand: ok, I've updated that infractions duration, it will now expire on {expires_dt.isoformat()}")

    @infractions.command(name='reason')
    async def infraction_reason(self, ctx, infraction_id: int, *, reason: str):
        if not self.is_mod(ctx): return await ctx.send("Invalid permissions.")
        
        edit_level = getattr(getattr(getattr(ctx, 'base_config', None), 'plugins', None), 'infractions', None)
        edit_level = edit_level.reason_edit_level if edit_level else 100

        def update_reason():
            try:
                inf = Infraction.get(id=infraction_id)
            except Infraction.DoesNotExist:
                return False, 'Unknown infraction ID'
                
            if inf.guild_id != ctx.guild.id:
                return False, 'Unknown infraction ID'
                
            if not inf.actor_id:
                inf.actor_id = ctx.author.id
                
            core = self.bot.get_cog('CorePlugin')
            author_level = core.get_level(ctx.guild.id, ctx.author) if core else 0
                
            if inf.actor_id != ctx.author.id and author_level < edit_level:
                return False, 'you do not have the permissions required to edit other moderators infractions'
                
            inf.reason = reason
            inf.save()
            return True, None

        success, err = await asyncio.to_thread(update_reason)
        if not success:
            return await ctx.send(f":warning: {err}")
            
        await ctx.send(f":ok_hand: I've updated the reason for infraction #{infraction_id}")
     @commands.command(aliases=['tempmute'])
    async def mute(self, ctx, user: discord.User, duration: str = None, *, reason: str = None):
        if not self.is_mod(ctx): return await ctx.send("Invalid permissions.")
        
        # Mimic Sentry's string parsing for optional durations
        if not duration and reason:
            duration = parse_duration(reason.split(' ')[0], safe=True)
            if duration:
                if ' ' in reason:
                    reason = reason.split(' ', 1)[-1]
                else:
                    reason = None
        elif duration:
            parsed_dur = parse_duration(duration, safe=True)
            if not parsed_dur and not reason:
                # Duration was actually the reason
                reason = duration
                duration = None
            else:
                duration = parsed_dur

        member = ctx.guild.get_member(user.id)
        if not member:
            return await ctx.send('invalid user')

        self.can_act_on(ctx, member.id)
        
        core = self.bot.get_cog('CorePlugin')
        guild_config = core.get_config(ctx.guild.id) if core else None
        mute_role_id = guild_config.plugins.infractions.mute_role if guild_config and hasattr(guild_config.plugins, 'infractions') else None
        
        if not mute_role_id:
            return await ctx.send('mute is not setup on this server')
            
        mute_role = ctx.guild.get_role(mute_role_id)
        if mute_role and mute_role in member.roles:
            return await ctx.send(f'{member.name} is already muted')

        if duration:
            await Infraction.tempmute(self, ctx, member, reason, duration)
            self.queue_infractions()
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            await self.confirm_action(ctx, maybe_string(
                reason,
                ':ok_hand: {u} is now muted for {t} (`{o}`)',
                ':ok_hand: {u} is now muted for {t}',
                u=member.name,
                t=humanize.naturaldelta(duration - now),
                o=reason
            ))
        else:
            existed = False
            if mute_role and mute_role in member.roles:
                existed = await asyncio.to_thread(Infraction.clear_active, ctx, member.id, [Infraction.Types.TEMPMUTE])
                if not existed:
                    return await ctx.send(f'{member.name} is already muted')
                    
            await Infraction.mute(self, ctx, member, reason)
            existed_str = ' [was temp-muted]' if existed else ''
            await self.confirm_action(ctx, maybe_string(
                reason,
                ':ok_hand: {u} is now muted (`{o}`)' + existed_str,
                ':ok_hand: {u} is now muted' + existed_str,
                u=member.name,
                o=reason
            ))

    @commands.command()
    async def temprole(self, ctx, user: discord.User, role_query: str, duration: str, *, reason: str = None):
        if not self.is_mod(ctx): return await ctx.send("Invalid permissions.")
        
        member = ctx.guild.get_member(user.id)
        if not member:
            return await ctx.send('invalid user')
            
        self.can_act_on(ctx, member.id)
        
        role_id = None
        if role_query.isdigit():
            role_id = int(role_query)
        else:
            config = getattr(ctx, 'base_config', None)
            if config and hasattr(config.plugins.admin, 'role_aliases'):
                role_id = config.plugins.admin.role_aliases.get(role_query.lower())
                
        if not role_id:
            role = discord.utils.get(ctx.guild.roles, name=role_query)
            if role: role_id = role.id
            
        role_obj = ctx.guild.get_role(role_id) if role_id else None
        if not role_obj:
            return await ctx.send('invalid or unknown role')
            
        if role_obj in member.roles:
            return await ctx.send(f'{member.name} is already in that role')
            
        expire_dt = parse_duration(duration)
        await Infraction.temprole(self, ctx, member, role_id, reason, expire_dt)
        self.queue_infractions()
        
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        await self.confirm_action(ctx, maybe_string(
            reason,
            ':ok_hand: {u} is now in the {r} role for {t} (`{o}`)',
            ':ok_hand: {u} is now in the {r} role for {t}',
            r=role_obj.name,
            u=member.name,
            t=humanize.naturaldelta(expire_dt - now),
            o=reason
        ))

    @commands.command()
    async def unmute(self, ctx, user: discord.User, *, reason: str = None):
        if not self.is_mod(ctx): return await ctx.send("Invalid permissions.")
        
        member = ctx.guild.get_member(user.id)
        if not member:
            return await ctx.send('invalid user')
            
        self.can_act_on(ctx, member.id)
        
        core = self.bot.get_cog('CorePlugin')
        guild_config = core.get_config(ctx.guild.id) if core else None
        mute_role_id = guild_config.plugins.infractions.mute_role if guild_config and hasattr(guild_config.plugins, 'infractions') else None
        
        if not mute_role_id:
            return await ctx.send('mute is not setup on this server')
            
        mute_role = ctx.guild.get_role(mute_role_id)
        if not mute_role or mute_role not in member.roles:
            return await ctx.send(f'{member.name} is not muted')
            
        await asyncio.to_thread(Infraction.clear_active, ctx, member.id, [Infraction.Types.MUTE, Infraction.Types.TEMPMUTE])
        
        modlog = self.bot.get_cog('ModLogPlugin')
        if modlog:
            await modlog.create_debounce(ctx, ['on_member_update'], role_id=mute_role_id)
            
        try:
            await member.remove_roles(mute_role)
        except discord.HTTPException:
            pass
            
        if modlog:
            await modlog.log_action_ext(
                Actions.MEMBER_UNMUTED,
                ctx.guild.id,
                member=member,
                actor=str(ctx.author) if ctx.author.id != member.id else 'Automatic',
            )
            
        await self.confirm_action(ctx, f':ok_hand: {member.name} is now unmuted')

    @commands.command()
    async def kick(self, ctx, user: discord.User, *, reason: str = None):
        if not self.is_mod(ctx): return await ctx.send("Invalid permissions.")
        
        member = ctx.guild.get_member(user.id)
        if not member:
            return await ctx.send('invalid user')
            
        self.can_act_on(ctx, member.id)
        await Infraction.kick(self, ctx, member, reason)
        
        await self.confirm_action(ctx, maybe_string(
            reason,
            ':ok_hand: kicked {u} (`{o}`)',
            ':ok_hand: kicked {u}',
            u=member.name,
            o=reason
        ))

    @commands.command()
    async def mkick(self, ctx, *args):
        if not self.is_mod(ctx): return await ctx.send("Invalid permissions.")
        
        users = []
        reason = "no reason"
        
        # Basic parser equivalent to mimicking Sentry's argparse
        for arg in args:
            if arg.isdigit():
                users.append(int(arg))
            else:
                reason = " ".join(args[args.index(arg):])
                break

        members = []
        for user_id in users:
            member = ctx.guild.get_member(user_id)
            if not member:
                return await ctx.send(f'failed to kick {user_id}, user not found')
            if not self.can_act_on(ctx, member.id, throw=False):
                return await ctx.send(f'failed to kick {user_id}, invalid permissions')
            members.append(member)

        if not members:
            return await ctx.send('No valid users provided to kick.')

        msg = await ctx.send(f'Ok, kick {len(members)} users for `{reason}`?')
        await msg.add_reaction(GREEN_TICK_EMOJI)
        await msg.add_reaction(RED_TICK_EMOJI)

        def check(reaction, r_user):
            return r_user == ctx.author and reaction.message.id == msg.id and str(reaction.emoji) in (GREEN_TICK_EMOJI, RED_TICK_EMOJI)

        try:
            reaction, _ = await self.bot.wait_for('reaction_add', timeout=10.0, check=check)
        except asyncio.TimeoutError:
            pass
        finally:
            try:
                await msg.delete()
            except discord.HTTPException:
                pass

        if 'reaction' in locals() and str(reaction.emoji) == GREEN_TICK_EMOJI:
            for member in members:
                await Infraction.kick(self, ctx, member, reason)
            await ctx.send(f'kicked {len(members)} users')

    @commands.command(aliases=['forceban'])
    async def ban(self, ctx, user: discord.User, *, reason: str = None):
        if not self.is_mod(ctx): return await ctx.send("Invalid permissions.")
        
        self.can_act_on(ctx, user.id)
        member = ctx.guild.get_member(user.id)
        
        await Infraction.ban(self, ctx, member or user.id, reason, guild=ctx.guild)
        
        await self.confirm_action(ctx, maybe_string(
            reason,
            ':ok_hand: banned {u} (`{o}`)',
            ':ok_hand: banned {u}',
            u=member.name if member else str(user.id),
            o=reason
        ))

    @commands.command()
    async def softban(self, ctx, user: discord.User, *, reason: str = None):
        if not self.is_mod(ctx): return await ctx.send("Invalid permissions.")
        
        member = ctx.guild.get_member(user.id)
        if not member:
            return await ctx.send('invalid user')
            
        self.can_act_on(ctx, member.id)
        await Infraction.softban(self, ctx, member, reason)
        
        await self.confirm_action(ctx, maybe_string(
            reason,
            ':ok_hand: soft-banned {u} (`{o}`)',
            ':ok_hand: soft-banned {u}',
            u=member.name,
            o=reason
        ))

    @commands.command()
    async def tempban(self, ctx, user: discord.User, duration: str, *, reason: str = None):
        if not self.is_mod(ctx): return await ctx.send("Invalid permissions.")
        
        member = ctx.guild.get_member(user.id)
        if not member:
            return await ctx.send('invalid user')
            
        self.can_act_on(ctx, member.id)
        expires_dt = parse_duration(duration)
        
        await Infraction.tempban(self, ctx, member, reason, expires_dt)
        self.queue_infractions()
        
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        await self.confirm_action(ctx, maybe_string(
            reason,
            ':ok_hand: temp-banned {u} for {t} (`{o}`)',
            ':ok_hand: temp-banned {u} for {t}',
            u=member.name,
            t=humanize.naturaldelta(expires_dt - now),
            o=reason
        ))

    @commands.command()
    async def warn(self, ctx, user: discord.User, *, reason: str = None):
        if not self.is_mod(ctx): return await ctx.send("Invalid permissions.")
        
        member = ctx.guild.get_member(user.id)
        if not member:
            return await ctx.send('invalid user')
            
        self.can_act_on(ctx, member.id)
        await Infraction.warn(self, ctx, member, reason, guild=ctx.guild)
        
        await self.confirm_action(ctx, maybe_string(
            reason,
            ':ok_hand: warned {u} (`{o}`)',
            ':ok_hand: warned {u}',
            u=member.name,
            o=reason
        ))

async def setup(bot):
    await bot.add_cog(InfractionsPlugin(bot))