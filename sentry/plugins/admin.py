import re
import time
import asyncio
import humanize
import operator
import functools
from datetime import datetime, timedelta, timezone
from fuzzywuzzy import fuzz

# Peewee imports
from peewee import fn

import discord
from discord.ext import commands

# Sentry internal imports
from sentry.redis import rdb
from sentry.types import Field, DictField, ListField, snowflake, SlottedModel
from sentry.types.plugin import PluginConfig
from sentry.plugins.modlog import Actions
from sentry.models.user import User
from sentry.models.guild import GuildMemberBackup, GuildEmoji, GuildVoiceSession
from sentry.models.message import Message, Reaction, MessageArchive
from sentry.constants import (
    GREEN_TICK_EMOJI_ID, RED_TICK_EMOJI_ID, GREEN_TICK_EMOJI, RED_TICK_EMOJI
)

EMOJI_RE = re.compile(r'<:[a-zA-Z0-9_]+:([0-9]+)>')

CUSTOM_EMOJI_STATS_SERVER_SQL = """
SELECT gm.emoji_id, gm.name, count(*) FROM guild_emojis gm
JOIN messages m ON m.emojis @> ARRAY[gm.emoji_id]
WHERE gm.deleted=false AND gm.guild_id={guild} AND m.guild_id={guild}
GROUP BY 1, 2
ORDER BY 3 {}
LIMIT 30
"""

CUSTOM_EMOJI_STATS_GLOBAL_SQL = """
SELECT gm.emoji_id, gm.name, count(*) FROM guild_emojis gm
JOIN messages m ON m.emojis @> ARRAY[gm.emoji_id]
WHERE gm.deleted=false AND gm.guild_id={guild}
GROUP BY 1, 2
ORDER BY 3 {}
LIMIT 30
"""

# Utility replacement for disco's MessageTable
class MessageTable:
    def __init__(self):
        self.headers = []
        self.rows = []

    def set_header(self, *args):
        self.headers = [str(arg) for arg in args]

    def add(self, *args):
        self.rows.append([str(arg) for arg in args])

    def compile(self):
        if not self.headers and not self.rows:
            return ""
        
        col_widths = [len(h) for h in self.headers]
        for row in self.rows:
            for i, col in enumerate(row):
                if len(col) > col_widths[i]:
                    col_widths[i] = len(col)
                    
        header_row = " | ".join(h.ljust(w) for h, w in zip(self.headers, col_widths))
        separator = "-+-".join("-" * w for w in col_widths)
        
        lines = [header_row, separator]
        for row in self.rows:
            lines.append(" | ".join(c.ljust(w) for c, w in zip(row, col_widths)))
            
        return "```\n" + "\n".join(lines) + "\n```"

class PersistConfig(SlottedModel):
    roles = Field(bool, default=False)
    nickname = Field(bool, default=False)
    voice = Field(bool, default=False)
    role_ids = ListField(snowflake, default=[])

class AdminConfig(PluginConfig):
    confirm_actions = Field(bool, default=True)
    persist = Field(PersistConfig, default=None)
    role_aliases = DictField(str, snowflake)
    group_roles = DictField(lambda value: str(value).lower(), snowflake)
    group_confirm_reactions = Field(bool, default=False)
    locked_roles = ListField(snowflake)

class AdminPlugin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.cleans = {}
        self.unlocked_roles = {}
        self.role_debounces = {}

    async def restore_user(self, ctx_or_event, member):
        guild_id = member.guild.id
        try:
            backup = await asyncio.to_thread(GuildMemberBackup.get, guild_id=guild_id, user_id=member.id)
        except GuildMemberBackup.DoesNotExist:
            return

        config = ctx_or_event.config if hasattr(ctx_or_event, 'config') else getattr(member.guild, 'base_config', None)
        if not config or not hasattr(config, 'persist'):
            return

        kwargs = {}
        if config.persist.roles:
            server_roles = {r.id: r for r in member.guild.roles}
            allowed_roles = set(server_roles.keys())
            if config.persist.role_ids:
                allowed_roles &= set(config.persist.role_ids)
            
            roles_to_apply = set(backup.roles) & allowed_roles
            if roles_to_apply:
                kwargs['roles'] = [server_roles[r_id] for r_id in roles_to_apply]

        if config.persist.nickname and backup.nick is not None:
            kwargs['nick'] = backup.nick

        if config.persist.voice and (backup.mute or backup.deaf):
            kwargs['mute'] = backup.mute
            kwargs['deaf'] = backup.deaf

        if not kwargs:
            return

        # Sentry dynamic plugin calling adaptation
        modlog = self.bot.get_cog('ModLogPlugin')
        if modlog:
            await modlog.create_debounce(ctx_or_event, ['on_member_update'])
        
        try:
            await member.edit(**kwargs)
            if modlog:
                await modlog.log_action_ext(Actions.MEMBER_RESTORE, guild_id, member=member)
        except discord.HTTPException as e:
            print(f"Failed to restore user {member.id}: {e}")

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        def backup_member():
            GuildMemberBackup.create_from_member(member)
        await asyncio.to_thread(backup_member)

    @commands.Cog.listener()
    async def on_member_join(self, member):
        guild_config = getattr(member.guild, 'base_config', None)
        if not guild_config or not getattr(guild_config, 'persist', None):
            return
        # Dummy event object to pass config mimicking disco context
        class MockEvent:
            pass
        event = MockEvent()
        event.config = guild_config
        await self.restore_user(event, member)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before, after):
        guild_config = getattr(after.guild, 'base_config', None)
        if not guild_config or after.id not in getattr(guild_config, 'locked_roles', []):
            return

        if after.id in self.unlocked_roles and self.unlocked_roles[after.id] > time.time():
            return

        if after.id in self.role_debounces:
            if self.role_debounces.pop(after.id) > time.time():
                return

        to_update = {}
        for field in ('name', 'hoist', 'color', 'permissions', 'position'):
            if getattr(before, field) != getattr(after, field):
                to_update[field] = getattr(before, field)

        if to_update:
            print(f'Rolling back update to roll {after.id} (in {after.guild.id}), roll is locked')
            self.role_debounces[after.id] = time.time() + 60
            await after.edit(**to_update)

    # Permission check helper
    def is_mod(self, ctx):
        core = self.bot.get_cog('CorePlugin')
        if not core: return False
        return core.get_level(ctx.guild.id, ctx.author) >= 50 # Assuming 50 is MOD level in Sentry

    @commands.command()
    async def roles(self, ctx):
        if not self.is_mod(ctx):
            return await ctx.send("Invalid permissions.")
            
        buff = ''
        for role in ctx.guild.roles:
            role_text = discord.utils.escape_markdown(f'{role.id} - {role.name}\n')
            if len(role_text) + len(buff) > 1990:
                await ctx.send(f'```\n{buff}```')
                buff = ''
            buff += role_text
            
        if buff:
            await ctx.send(f'```\n{buff}```')

    @commands.group(invoke_without_command=True)
    async def backups(self, ctx):
        pass

    @backups.command(name='restore')
    async def backups_restore(self, ctx, user: discord.Member):
        if not self.is_mod(ctx):
            return await ctx.send("Invalid permissions.")
        await self.restore_user(ctx, user)
        await ctx.send(f":ok_hand: Attempted restoration for {user.name}")

    @backups.command(name='clear')
    async def backups_clear(self, ctx, user_id: int):
        if not self.is_mod(ctx):
            return await ctx.send("Invalid permissions.")
            
        def delete_backup():
            return bool(GuildMemberBackup.delete().where(
                (GuildMemberBackup.user_id == user_id) &
                (GuildMemberBackup.guild_id == ctx.guild.id)
            ).execute())

        deleted = await asyncio.to_thread(delete_backup)

        if deleted:
            await ctx.send(':ok_hand: I\'ve cleared the member backup for that user')
        else:
            await ctx.send('I couldn\'t find any member backups for that user')

    async def can_act_on(self, ctx, victim_id, throw=True):
        if ctx.author.id == victim_id:
            if not throw: return False
            raise commands.CommandError('cannot execute that action on yourself')

        core = self.bot.get_cog('CorePlugin')
        victim_level = core.get_level(ctx.guild.id, ctx.guild.get_member(victim_id)) if core else 0
        actor_level = core.get_level(ctx.guild.id, ctx.author) if core else 0
        
        if actor_level <= victim_level:
            if not throw: return False
            raise commands.CommandError('invalid permissions')
        return True

    @commands.group(invoke_without_command=True)
    async def archive(self, ctx):
        pass

    @archive.command(name='here')
    async def archive_here(self, ctx, size: int = 50):
        await self._run_archive(ctx, size=size, mode='all', channel=ctx.channel)

    @archive.command(name='all')
    async def archive_all(self, ctx, size: int = 50):
        await self._run_archive(ctx, size=size, mode='all', channel=ctx.channel)

    @archive.command(name='user')
    async def archive_user(self, ctx, user: discord.User, size: int = 50):
        await self._run_archive(ctx, size=size, mode='user', user=user)

    @archive.command(name='channel')
    async def archive_channel(self, ctx, channel: discord.TextChannel, size: int = 50):
        await self._run_archive(ctx, size=size, mode='channel', channel=channel)

    async def _run_archive(self, ctx, size=50, mode=None, user=None, channel=None):
        if not self.is_mod(ctx): return await ctx.send("Invalid permissions.")
        
        if 0 > size >= 15000:
            return await ctx.send('too many messages must be between 1-15000')

        def fetch_archive_ids():
            q = Message.select(Message.id).join(User).order_by(Message.id.desc()).limit(size)
            if mode in ('all', 'channel'):
                q = q.where((Message.channel_id == (channel.id if channel else ctx.channel.id)))
            else:
                user_id = user.id if user else ctx.author.id
                q = q.where(
                    (Message.author_id == user_id) &
                    (Message.guild_id == ctx.guild.id)
                )
            archive = MessageArchive.create_from_message_ids([i.id for i in q])
            return archive

        archive = await asyncio.to_thread(fetch_archive_ids)
        await ctx.send('OK, archived {} messages at {}'.format(len(archive.message_ids), archive.url))

    @archive.command(name='extend')
    async def archive_extend(self, ctx, archive_id: str, duration: str):
        if not self.is_mod(ctx): return await ctx.send("Invalid permissions.")
        try:
            archive = await asyncio.to_thread(MessageArchive.get, archive_id=archive_id)
        except MessageArchive.DoesNotExist:
            return await ctx.send('invalid message archive id')

        from sentry.util.input import parse_duration
        parsed_duration = parse_duration(duration)
        
        def update_archive():
            archive.expires_at = parsed_duration
            MessageArchive.update(
                expires_at=parsed_duration
            ).where(
                (MessageArchive.archive_id == archive_id)
            ).execute()

        await asyncio.to_thread(update_archive)
        await ctx.send(f'duration of archive {archive_id} has been extended (<{archive.url}>)')

    @commands.group(invoke_without_command=True)
    async def clean(self, ctx):
        pass

    @clean.command(name='cancel')
    async def clean_cancel(self, ctx):
        if not self.is_mod(ctx): return await ctx.send("Invalid permissions.")
        if ctx.channel.id not in self.cleans:
            return await ctx.send('no clean is running in this channel')

        self.cleans[ctx.channel.id].cancel()
        await ctx.send('Ok, the running clean was cancelled')

    @clean.command(name='all')
    async def clean_all(self, ctx, size: int = 25):
        await self._run_clean(ctx, size=size, mode='all')

    @clean.command(name='bots')
    async def clean_bots(self, ctx, size: int = 25):
        await self._run_clean(ctx, size=size, mode='bots')

    @clean.command(name='user')
    async def clean_user(self, ctx, user: discord.User, size: int = 25):
        await self._run_clean(ctx, size=size, mode='user', user=user)

    async def _run_clean(self, ctx, size=25, mode='all', user=None):
        if not self.is_mod(ctx): return await ctx.send("Invalid permissions.")
        if 0 > size >= 10000:
            return await ctx.send('too many messages must be between 1-10000')

        if ctx.channel.id in self.cleans:
            return await ctx.send('a clean is already running on this channel')

        now = datetime.now(timezone.utc).replace(tzinfo=None)

        def fetch_messages():
            query = Message.select(Message.id).where(
                (Message.deleted >> False) &
                (Message.channel_id == ctx.channel.id) &
                (Message.timestamp > (now - timedelta(days=13)))
            ).join(User).order_by(Message.timestamp.desc()).limit(size)

            if mode == 'bots':
                query = query.where((User.bot >> True))
            elif mode == 'user':
                query = query.where((User.user_id == user.id))

            return [i[0] for i in query.tuples()]

        messages = await asyncio.to_thread(fetch_messages)

        if len(messages) > 100:
            msg = await ctx.send(f'Woah there, that will delete a total of {len(messages)} messages, please confirm.')
            await msg.add_reaction(GREEN_TICK_EMOJI)
            await msg.add_reaction(RED_TICK_EMOJI)

            def check(reaction, r_user):
                return r_user == ctx.author and reaction.message.id == msg.id and str(reaction.emoji) in (GREEN_TICK_EMOJI, RED_TICK_EMOJI)

            try:
                reaction, _ = await self.bot.wait_for('reaction_add', timeout=10.0, check=check)
            except asyncio.TimeoutError:
                await msg.delete()
                return
            
            await msg.delete()
            if str(reaction.emoji) != GREEN_TICK_EMOJI:
                return

            notify_msg = await ctx.send(':wastebasket: Ok please hold on while I delete those messages...')
            self.bot.loop.call_later(5, lambda: asyncio.create_task(notify_msg.delete()))

        async def run_clean():
            try:
                chunk_size = 100
                for i in range(0, len(messages), chunk_size):
                    chunk = [discord.Object(id=m_id) for m_id in messages[i:i + chunk_size]]
                    await ctx.channel.delete_messages(chunk)
                    await asyncio.sleep(1) # Prevent aggressive rate limits
            except asyncio.CancelledError:
                pass
            except discord.HTTPException as e:
                print(f"Failed bulk delete: {e}")
            finally:
                if ctx.channel.id in self.cleans:
                    del self.cleans[ctx.channel.id]

        self.cleans[ctx.channel.id] = self.bot.loop.create_task(run_clean())

    @commands.group(invoke_without_command=True)
    async def role(self, ctx):
        pass

    @role.command(name='add')
    async def role_add(self, ctx, user: discord.Member, role: str, *, reason: str = None):
        await self._modify_role(ctx, user, role, reason, mode='add')

    @role.command(name='rmv', aliases=['remove'])
    async def role_remove(self, ctx, user: discord.Member, role: str, *, reason: str = None):
        await self._modify_role(ctx, user, role, reason, mode='remove')

    async def _modify_role(self, ctx, member, role_query, reason, mode):
        if not self.is_mod(ctx): return await ctx.send("Invalid permissions.")
        
        role_obj = None
        guild_roles = {r.id: r for r in ctx.guild.roles}
        
        if role_query.isdigit() and int(role_query) in guild_roles:
            role_obj = guild_roles[int(role_query)]
        elif hasattr(ctx, 'base_config') and role_query.lower() in ctx.base_config.plugins.admin.role_aliases:
            role_obj = guild_roles.get(ctx.base_config.plugins.admin.role_aliases[role_query.lower()])
        else:
            exact_matches = [r for r in ctx.guild.roles if r.name.lower().replace(' ', '') == role_query.lower()]
            if len(exact_matches) == 1:
                role_obj = exact_matches[0]
            else:
                rated = sorted([
                    (fuzz.partial_ratio(role_query, r.name.replace(' ', '')), r) for r in ctx.guild.roles
                ], key=lambda i: i[0], reverse=True)
                if rated and rated[0][0] > 40:
                    if len(rated) == 1 or (len(rated) > 1 and rated[0][0] - rated[1][0] > 20):
                        role_obj = rated[0][1]

        if not role_obj:
            return await ctx.send('too many matches for that role, try something more exact or the role ID')

        highest_role = max(ctx.author.roles, key=lambda r: r.position)

        if ctx.author.id != ctx.guild.owner_id and highest_role.position <= role_obj.position:
            return await ctx.send(f'you can only {mode} roles that are ranked lower than your highest role')

        if mode == 'add' and role_obj in member.roles:
            return await ctx.send(f'{member} already has the {role_obj.name} role')
        elif mode == 'remove' and role_obj not in member.roles:
            return await ctx.send(f"{member} doesn't have the {role_obj.name} role")

        modlog = self.bot.get_cog('ModLogPlugin')
        if modlog:
            await modlog.create_debounce(ctx, ['on_member_update'], role_id=role_obj.id)

        try:
            if mode == 'add':
                await member.add_roles(role_obj, reason=reason)
            else:
                await member.remove_roles(role_obj, reason=reason)
        except discord.Forbidden:
            return await ctx.send("I do not have permission to modify that role.")

        if modlog:
            action = Actions.MEMBER_ROLE_ADD if mode == 'add' else Actions.MEMBER_ROLE_REMOVE
            await modlog.log_action_ext(action, ctx.guild.id, member=member, role=role_obj, actor=str(ctx.author), reason=reason or 'no reason')

        action_word = 'added' if mode == 'add' else 'removed'
        await ctx.send(f':ok_hand: {action_word} role {role_obj.name} to {member}')

    @commands.command(name='stats')
    async def msgstats(self, ctx, user: discord.User):
        if not self.is_mod(ctx): return await ctx.send("Invalid permissions.")
        
        def run_queries():
            message_stats = list(Message.select(
                fn.Count('*'),
                fn.Sum(fn.char_length(Message.content)),
                fn.Sum(fn.array_length(Message.emojis, 1)),
                fn.Sum(fn.array_length(Message.mentions, 1)),
                fn.Sum(fn.array_length(Message.attachments, 1)),
            ).where((Message.author_id == user.id)).tuples())

            reactions_given = list(Reaction.select(
                fn.Count('*'),
                Reaction.emoji_id,
                Reaction.emoji_name,
            ).join(
                Message, on=(Message.id == Reaction.message_id)
            ).where(
                (Reaction.user_id == user.id)
            ).group_by(
                Reaction.emoji_id, Reaction.emoji_name
            ).order_by(fn.Count('*').desc()).tuples())

            emojis = list(Message.raw('''
                SELECT gm.emoji_id, gm.name, count(*)
                FROM (
                    SELECT unnest(emojis) as id
                    FROM messages
                    WHERE author_id=%s
                ) q
                JOIN guild_emojis gm ON gm.emoji_id=q.id
                GROUP BY 1, 2
                ORDER BY 3 DESC
                LIMIT 1
            ''', (user.id, )).tuples())

            deleted = list(Message.select(fn.Count('*')).where(
                (Message.author_id == user.id) &
                (Message.deleted == 1)
            ).tuples())
            
            return message_stats, reactions_given, emojis, deleted

        # Since we can't easily wait_many on Peewee in asyncio, we run them sequentially or in a thread pool
        message_stats, reactions_given, emojis, deleted = await asyncio.to_thread(run_queries)

        if not message_stats:
            return await ctx.send("No stats found.")

        q = message_stats[0]
        embed = discord.Embed()
        embed.add_field(name='Total Messages Sent', value=str(q[0] or '0'), inline=True)
        embed.add_field(name='Total Characters Sent', value=str(q[1] or '0'), inline=True)

        if deleted and deleted[0][0]:
            embed.add_field(name='Total Deleted Messages', value=str(deleted[0][0]), inline=True)

        embed.add_field(name='Total Custom Emojis', value=str(q[2] or '0'), inline=True)
        embed.add_field(name='Total Mentions', value=str(q[3] or '0'), inline=True)
        embed.add_field(name='Total Attachments', value=str(q[4] or '0'), inline=True)

        if reactions_given:
            embed.add_field(name='Total Reactions', value=str(sum(i[0] for i in reactions_given)), inline=True)
            emoji_str = reactions_given[0][2] if not reactions_given[0][1] else f'<:{reactions_given[0][2]}:{reactions_given[0][1]}>'
            embed.add_field(name='Most Used Reaction', value=f'{emoji_str} (used {reactions_given[0][0]} times)', inline=True)

        if emojis:
            embed.add_field(name='Most Used Emoji', value=f'<:{emojis[0][1]}:{emojis[0][0]}> (`{emojis[0][1]}`, used {emojis[0][2]} times)')

        if user.display_avatar:
            embed.set_thumbnail(url=user.display_avatar.url)
            
        from sentry.util.images import get_dominant_colors_user
        try:
            color = await asyncio.to_thread(get_dominant_colors_user, user)
            embed.color = color
        except Exception:
            pass # Fallback if image utility fails

        await ctx.send(embed=embed)
     import re
import time
import asyncio
import humanize
import operator
import functools
from datetime import datetime, timedelta, timezone
from fuzzywuzzy import fuzz

# Peewee imports
from peewee import fn

import discord
from discord.ext import commands

# Sentry internal imports
from sentry.redis import rdb
from sentry.types import Field, DictField, ListField, snowflake, SlottedModel
from sentry.types.plugin import PluginConfig
from sentry.plugins.modlog import Actions
from sentry.models.user import User
from sentry.models.guild import GuildMemberBackup, GuildEmoji, GuildVoiceSession
from sentry.models.message import Message, Reaction, MessageArchive
from sentry.constants import (
    GREEN_TICK_EMOJI_ID, RED_TICK_EMOJI_ID, GREEN_TICK_EMOJI, RED_TICK_EMOJI
)

EMOJI_RE = re.compile(r'<:[a-zA-Z0-9_]+:([0-9]+)>')

CUSTOM_EMOJI_STATS_SERVER_SQL = """
SELECT gm.emoji_id, gm.name, count(*) FROM guild_emojis gm
JOIN messages m ON m.emojis @> ARRAY[gm.emoji_id]
WHERE gm.deleted=false AND gm.guild_id={guild} AND m.guild_id={guild}
GROUP BY 1, 2
ORDER BY 3 {}
LIMIT 30
"""

CUSTOM_EMOJI_STATS_GLOBAL_SQL = """
SELECT gm.emoji_id, gm.name, count(*) FROM guild_emojis gm
JOIN messages m ON m.emojis @> ARRAY[gm.emoji_id]
WHERE gm.deleted=false AND gm.guild_id={guild}
GROUP BY 1, 2
ORDER BY 3 {}
LIMIT 30
"""

# Utility replacement for disco's MessageTable
class MessageTable:
    def __init__(self):
        self.headers = []
        self.rows = []

    def set_header(self, *args):
        self.headers = [str(arg) for arg in args]

    def add(self, *args):
        self.rows.append([str(arg) for arg in args])

    def compile(self):
        if not self.headers and not self.rows:
            return ""
        
        col_widths = [len(h) for h in self.headers]
        for row in self.rows:
            for i, col in enumerate(row):
                if len(col) > col_widths[i]:
                    col_widths[i] = len(col)
                    
        header_row = " | ".join(h.ljust(w) for h, w in zip(self.headers, col_widths))
        separator = "-+-".join("-" * w for w in col_widths)
        
        lines = [header_row, separator]
        for row in self.rows:
            lines.append(" | ".join(c.ljust(w) for c, w in zip(row, col_widths)))
            
        return "```\n" + "\n".join(lines) + "\n```"

class PersistConfig(SlottedModel):
    roles = Field(bool, default=False)
    nickname = Field(bool, default=False)
    voice = Field(bool, default=False)
    role_ids = ListField(snowflake, default=[])

class AdminConfig(PluginConfig):
    confirm_actions = Field(bool, default=True)
    persist = Field(PersistConfig, default=None)
    role_aliases = DictField(str, snowflake)
    group_roles = DictField(lambda value: str(value).lower(), snowflake)
    group_confirm_reactions = Field(bool, default=False)
    locked_roles = ListField(snowflake)

class AdminPlugin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.cleans = {}
        self.unlocked_roles = {}
        self.role_debounces = {}

    async def restore_user(self, ctx_or_event, member):
        guild_id = member.guild.id
        try:
            backup = await asyncio.to_thread(GuildMemberBackup.get, guild_id=guild_id, user_id=member.id)
        except GuildMemberBackup.DoesNotExist:
            return

        config = ctx_or_event.config if hasattr(ctx_or_event, 'config') else getattr(member.guild, 'base_config', None)
        if not config or not hasattr(config, 'persist'):
            return

        kwargs = {}
        if config.persist.roles:
            server_roles = {r.id: r for r in member.guild.roles}
            allowed_roles = set(server_roles.keys())
            if config.persist.role_ids:
                allowed_roles &= set(config.persist.role_ids)
            
            roles_to_apply = set(backup.roles) & allowed_roles
            if roles_to_apply:
                kwargs['roles'] = [server_roles[r_id] for r_id in roles_to_apply]

        if config.persist.nickname and backup.nick is not None:
            kwargs['nick'] = backup.nick

        if config.persist.voice and (backup.mute or backup.deaf):
            kwargs['mute'] = backup.mute
            kwargs['deaf'] = backup.deaf

        if not kwargs:
            return

        # Sentry dynamic plugin calling adaptation
        modlog = self.bot.get_cog('ModLogPlugin')
        if modlog:
            await modlog.create_debounce(ctx_or_event, ['on_member_update'])
        
        try:
            await member.edit(**kwargs)
            if modlog:
                await modlog.log_action_ext(Actions.MEMBER_RESTORE, guild_id, member=member)
        except discord.HTTPException as e:
            print(f"Failed to restore user {member.id}: {e}")

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        def backup_member():
            GuildMemberBackup.create_from_member(member)
        await asyncio.to_thread(backup_member)

    @commands.Cog.listener()
    async def on_member_join(self, member):
        guild_config = getattr(member.guild, 'base_config', None)
        if not guild_config or not getattr(guild_config, 'persist', None):
            return
        # Dummy event object to pass config mimicking disco context
        class MockEvent:
            pass
        event = MockEvent()
        event.config = guild_config
        await self.restore_user(event, member)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before, after):
        guild_config = getattr(after.guild, 'base_config', None)
        if not guild_config or after.id not in getattr(guild_config, 'locked_roles', []):
            return

        if after.id in self.unlocked_roles and self.unlocked_roles[after.id] > time.time():
            return

        if after.id in self.role_debounces:
            if self.role_debounces.pop(after.id) > time.time():
                return

        to_update = {}
        for field in ('name', 'hoist', 'color', 'permissions', 'position'):
            if getattr(before, field) != getattr(after, field):
                to_update[field] = getattr(before, field)

        if to_update:
            print(f'Rolling back update to roll {after.id} (in {after.guild.id}), roll is locked')
            self.role_debounces[after.id] = time.time() + 60
            await after.edit(**to_update)

    # Permission check helper
    def is_mod(self, ctx):
        core = self.bot.get_cog('CorePlugin')
        if not core: return False
        return core.get_level(ctx.guild.id, ctx.author) >= 50 # Assuming 50 is MOD level in Sentry

    @commands.command()
    async def roles(self, ctx):
        if not self.is_mod(ctx):
            return await ctx.send("Invalid permissions.")
            
        buff = ''
        for role in ctx.guild.roles:
            role_text = discord.utils.escape_markdown(f'{role.id} - {role.name}\n')
            if len(role_text) + len(buff) > 1990:
                await ctx.send(f'```\n{buff}```')
                buff = ''
            buff += role_text
            
        if buff:
            await ctx.send(f'```\n{buff}```')

    @commands.group(invoke_without_command=True)
    async def backups(self, ctx):
        pass

    @backups.command(name='restore')
    async def backups_restore(self, ctx, user: discord.Member):
        if not self.is_mod(ctx):
            return await ctx.send("Invalid permissions.")
        await self.restore_user(ctx, user)
        await ctx.send(f":ok_hand: Attempted restoration for {user.name}")

    @backups.command(name='clear')
    async def backups_clear(self, ctx, user_id: int):
        if not self.is_mod(ctx):
            return await ctx.send("Invalid permissions.")
            
        def delete_backup():
            return bool(GuildMemberBackup.delete().where(
                (GuildMemberBackup.user_id == user_id) &
                (GuildMemberBackup.guild_id == ctx.guild.id)
            ).execute())

        deleted = await asyncio.to_thread(delete_backup)

        if deleted:
            await ctx.send(':ok_hand: I\'ve cleared the member backup for that user')
        else:
            await ctx.send('I couldn\'t find any member backups for that user')

    async def can_act_on(self, ctx, victim_id, throw=True):
        if ctx.author.id == victim_id:
            if not throw: return False
            raise commands.CommandError('cannot execute that action on yourself')

        core = self.bot.get_cog('CorePlugin')
        victim_level = core.get_level(ctx.guild.id, ctx.guild.get_member(victim_id)) if core else 0
        actor_level = core.get_level(ctx.guild.id, ctx.author) if core else 0
        
        if actor_level <= victim_level:
            if not throw: return False
            raise commands.CommandError('invalid permissions')
        return True

    @commands.group(invoke_without_command=True)
    async def archive(self, ctx):
        pass

    @archive.command(name='here')
    async def archive_here(self, ctx, size: int = 50):
        await self._run_archive(ctx, size=size, mode='all', channel=ctx.channel)

    @archive.command(name='all')
    async def archive_all(self, ctx, size: int = 50):
        await self._run_archive(ctx, size=size, mode='all', channel=ctx.channel)

    @archive.command(name='user')
    async def archive_user(self, ctx, user: discord.User, size: int = 50):
        await self._run_archive(ctx, size=size, mode='user', user=user)

    @archive.command(name='channel')
    async def archive_channel(self, ctx, channel: discord.TextChannel, size: int = 50):
        await self._run_archive(ctx, size=size, mode='channel', channel=channel)

    async def _run_archive(self, ctx, size=50, mode=None, user=None, channel=None):
        if not self.is_mod(ctx): return await ctx.send("Invalid permissions.")
        
        if 0 > size >= 15000:
            return await ctx.send('too many messages must be between 1-15000')

        def fetch_archive_ids():
            q = Message.select(Message.id).join(User).order_by(Message.id.desc()).limit(size)
            if mode in ('all', 'channel'):
                q = q.where((Message.channel_id == (channel.id if channel else ctx.channel.id)))
            else:
                user_id = user.id if user else ctx.author.id
                q = q.where(
                    (Message.author_id == user_id) &
                    (Message.guild_id == ctx.guild.id)
                )
            archive = MessageArchive.create_from_message_ids([i.id for i in q])
            return archive

        archive = await asyncio.to_thread(fetch_archive_ids)
        await ctx.send('OK, archived {} messages at {}'.format(len(archive.message_ids), archive.url))

    @archive.command(name='extend')
    async def archive_extend(self, ctx, archive_id: str, duration: str):
        if not self.is_mod(ctx): return await ctx.send("Invalid permissions.")
        try:
            archive = await asyncio.to_thread(MessageArchive.get, archive_id=archive_id)
        except MessageArchive.DoesNotExist:
            return await ctx.send('invalid message archive id')

        from sentry.util.input import parse_duration
        parsed_duration = parse_duration(duration)
        
        def update_archive():
            archive.expires_at = parsed_duration
            MessageArchive.update(
                expires_at=parsed_duration
            ).where(
                (MessageArchive.archive_id == archive_id)
            ).execute()

        await asyncio.to_thread(update_archive)
        await ctx.send(f'duration of archive {archive_id} has been extended (<{archive.url}>)')

    @commands.group(invoke_without_command=True)
    async def clean(self, ctx):
        pass

    @clean.command(name='cancel')
    async def clean_cancel(self, ctx):
        if not self.is_mod(ctx): return await ctx.send("Invalid permissions.")
        if ctx.channel.id not in self.cleans:
            return await ctx.send('no clean is running in this channel')

        self.cleans[ctx.channel.id].cancel()
        await ctx.send('Ok, the running clean was cancelled')

    @clean.command(name='all')
    async def clean_all(self, ctx, size: int = 25):
        await self._run_clean(ctx, size=size, mode='all')

    @clean.command(name='bots')
    async def clean_bots(self, ctx, size: int = 25):
        await self._run_clean(ctx, size=size, mode='bots')

    @clean.command(name='user')
    async def clean_user(self, ctx, user: discord.User, size: int = 25):
        await self._run_clean(ctx, size=size, mode='user', user=user)

    async def _run_clean(self, ctx, size=25, mode='all', user=None):
        if not self.is_mod(ctx): return await ctx.send("Invalid permissions.")
        if 0 > size >= 10000:
            return await ctx.send('too many messages must be between 1-10000')

        if ctx.channel.id in self.cleans:
            return await ctx.send('a clean is already running on this channel')

        now = datetime.now(timezone.utc).replace(tzinfo=None)

        def fetch_messages():
            query = Message.select(Message.id).where(
                (Message.deleted >> False) &
                (Message.channel_id == ctx.channel.id) &
                (Message.timestamp > (now - timedelta(days=13)))
            ).join(User).order_by(Message.timestamp.desc()).limit(size)

            if mode == 'bots':
                query = query.where((User.bot >> True))
            elif mode == 'user':
                query = query.where((User.user_id == user.id))

            return [i[0] for i in query.tuples()]

        messages = await asyncio.to_thread(fetch_messages)

        if len(messages) > 100:
            msg = await ctx.send(f'Woah there, that will delete a total of {len(messages)} messages, please confirm.')
            await msg.add_reaction(GREEN_TICK_EMOJI)
            await msg.add_reaction(RED_TICK_EMOJI)

            def check(reaction, r_user):
                return r_user == ctx.author and reaction.message.id == msg.id and str(reaction.emoji) in (GREEN_TICK_EMOJI, RED_TICK_EMOJI)

            try:
                reaction, _ = await self.bot.wait_for('reaction_add', timeout=10.0, check=check)
            except asyncio.TimeoutError:
                await msg.delete()
                return
            
            await msg.delete()
            if str(reaction.emoji) != GREEN_TICK_EMOJI:
                return

            notify_msg = await ctx.send(':wastebasket: Ok please hold on while I delete those messages...')
            self.bot.loop.call_later(5, lambda: asyncio.create_task(notify_msg.delete()))

        async def run_clean():
            try:
                chunk_size = 100
                for i in range(0, len(messages), chunk_size):
                    chunk = [discord.Object(id=m_id) for m_id in messages[i:i + chunk_size]]
                    await ctx.channel.delete_messages(chunk)
                    await asyncio.sleep(1) # Prevent aggressive rate limits
            except asyncio.CancelledError:
                pass
            except discord.HTTPException as e:
                print(f"Failed bulk delete: {e}")
            finally:
                if ctx.channel.id in self.cleans:
                    del self.cleans[ctx.channel.id]

        self.cleans[ctx.channel.id] = self.bot.loop.create_task(run_clean())

    @commands.group(invoke_without_command=True)
    async def role(self, ctx):
        pass

    @role.command(name='add')
    async def role_add(self, ctx, user: discord.Member, role: str, *, reason: str = None):
        await self._modify_role(ctx, user, role, reason, mode='add')

    @role.command(name='rmv', aliases=['remove'])
    async def role_remove(self, ctx, user: discord.Member, role: str, *, reason: str = None):
        await self._modify_role(ctx, user, role, reason, mode='remove')

    async def _modify_role(self, ctx, member, role_query, reason, mode):
        if not self.is_mod(ctx): return await ctx.send("Invalid permissions.")
        
        role_obj = None
        guild_roles = {r.id: r for r in ctx.guild.roles}
        
        if role_query.isdigit() and int(role_query) in guild_roles:
            role_obj = guild_roles[int(role_query)]
        elif hasattr(ctx, 'base_config') and role_query.lower() in ctx.base_config.plugins.admin.role_aliases:
            role_obj = guild_roles.get(ctx.base_config.plugins.admin.role_aliases[role_query.lower()])
        else:
            exact_matches = [r for r in ctx.guild.roles if r.name.lower().replace(' ', '') == role_query.lower()]
            if len(exact_matches) == 1:
                role_obj = exact_matches[0]
            else:
                rated = sorted([
                    (fuzz.partial_ratio(role_query, r.name.replace(' ', '')), r) for r in ctx.guild.roles
                ], key=lambda i: i[0], reverse=True)
                if rated and rated[0][0] > 40:
                    if len(rated) == 1 or (len(rated) > 1 and rated[0][0] - rated[1][0] > 20):
                        role_obj = rated[0][1]

        if not role_obj:
            return await ctx.send('too many matches for that role, try something more exact or the role ID')

        highest_role = max(ctx.author.roles, key=lambda r: r.position)

        if ctx.author.id != ctx.guild.owner_id and highest_role.position <= role_obj.position:
            return await ctx.send(f'you can only {mode} roles that are ranked lower than your highest role')

        if mode == 'add' and role_obj in member.roles:
            return await ctx.send(f'{member} already has the {role_obj.name} role')
        elif mode == 'remove' and role_obj not in member.roles:
            return await ctx.send(f"{member} doesn't have the {role_obj.name} role")

        modlog = self.bot.get_cog('ModLogPlugin')
        if modlog:
            await modlog.create_debounce(ctx, ['on_member_update'], role_id=role_obj.id)

        try:
            if mode == 'add':
                await member.add_roles(role_obj, reason=reason)
            else:
                await member.remove_roles(role_obj, reason=reason)
        except discord.Forbidden:
            return await ctx.send("I do not have permission to modify that role.")

        if modlog:
            action = Actions.MEMBER_ROLE_ADD if mode == 'add' else Actions.MEMBER_ROLE_REMOVE
            await modlog.log_action_ext(action, ctx.guild.id, member=member, role=role_obj, actor=str(ctx.author), reason=reason or 'no reason')

        action_word = 'added' if mode == 'add' else 'removed'
        await ctx.send(f':ok_hand: {action_word} role {role_obj.name} to {member}')

    @commands.command(name='stats')
    async def msgstats(self, ctx, user: discord.User):
        if not self.is_mod(ctx): return await ctx.send("Invalid permissions.")
        
        def run_queries():
            message_stats = list(Message.select(
                fn.Count('*'),
                fn.Sum(fn.char_length(Message.content)),
                fn.Sum(fn.array_length(Message.emojis, 1)),
                fn.Sum(fn.array_length(Message.mentions, 1)),
                fn.Sum(fn.array_length(Message.attachments, 1)),
            ).where((Message.author_id == user.id)).tuples())

            reactions_given = list(Reaction.select(
                fn.Count('*'),
                Reaction.emoji_id,
                Reaction.emoji_name,
            ).join(
                Message, on=(Message.id == Reaction.message_id)
            ).where(
                (Reaction.user_id == user.id)
            ).group_by(
                Reaction.emoji_id, Reaction.emoji_name
            ).order_by(fn.Count('*').desc()).tuples())

            emojis = list(Message.raw('''
                SELECT gm.emoji_id, gm.name, count(*)
                FROM (
                    SELECT unnest(emojis) as id
                    FROM messages
                    WHERE author_id=%s
                ) q
                JOIN guild_emojis gm ON gm.emoji_id=q.id
                GROUP BY 1, 2
                ORDER BY 3 DESC
                LIMIT 1
            ''', (user.id, )).tuples())

            deleted = list(Message.select(fn.Count('*')).where(
                (Message.author_id == user.id) &
                (Message.deleted == 1)
            ).tuples())
            
            return message_stats, reactions_given, emojis, deleted

        # Since we can't easily wait_many on Peewee in asyncio, we run them sequentially or in a thread pool
        message_stats, reactions_given, emojis, deleted = await asyncio.to_thread(run_queries)

        if not message_stats:
            return await ctx.send("No stats found.")

        q = message_stats[0]
        embed = discord.Embed()
        embed.add_field(name='Total Messages Sent', value=str(q[0] or '0'), inline=True)
        embed.add_field(name='Total Characters Sent', value=str(q[1] or '0'), inline=True)

        if deleted and deleted[0][0]:
            embed.add_field(name='Total Deleted Messages', value=str(deleted[0][0]), inline=True)

        embed.add_field(name='Total Custom Emojis', value=str(q[2] or '0'), inline=True)
        embed.add_field(name='Total Mentions', value=str(q[3] or '0'), inline=True)
        embed.add_field(name='Total Attachments', value=str(q[4] or '0'), inline=True)

        if reactions_given:
            embed.add_field(name='Total Reactions', value=str(sum(i[0] for i in reactions_given)), inline=True)
            emoji_str = reactions_given[0][2] if not reactions_given[0][1] else f'<:{reactions_given[0][2]}:{reactions_given[0][1]}>'
            embed.add_field(name='Most Used Reaction', value=f'{emoji_str} (used {reactions_given[0][0]} times)', inline=True)

        if emojis:
            embed.add_field(name='Most Used Emoji', value=f'<:{emojis[0][1]}:{emojis[0][0]}> (`{emojis[0][1]}`, used {emojis[0][2]} times)')

        if user.display_avatar:
            embed.set_thumbnail(url=user.display_avatar.url)
            
        from sentry.util.images import get_dominant_colors_user
        try:
            color = await asyncio.to_thread(get_dominant_colors_user, user)
            embed.color = color
        except Exception:
            pass # Fallback if image utility fails

        await ctx.send(embed=embed)
    @commands.command(name='emojistats')
    async def emojistats_custom(self, ctx, mode: str, sort: str):
        if not self.is_mod(ctx): return await ctx.send("Invalid permissions.")
        if mode not in ('server', 'global'):
            return await ctx.send('invalid emoji mode, must be `server` or `global`')
        if sort not in ('least', 'most'):
            return await ctx.send('invalid emoji sort, must be `least` or `most`')

        order = 'DESC' if sort == 'most' else 'ASC'
        
        def fetch_emoji_stats():
            if mode == 'server':
                q = CUSTOM_EMOJI_STATS_SERVER_SQL.format(order, guild=ctx.guild.id)
            else:
                q = CUSTOM_EMOJI_STATS_GLOBAL_SQL.format(order, guild=ctx.guild.id)
            return list(GuildEmoji.raw(q).tuples())

        q = await asyncio.to_thread(fetch_emoji_stats)
        
        tbl = MessageTable()
        tbl.set_header('Count', 'Name', 'ID')
        for emoji_id, name, count in q:
            tbl.add(count, name, emoji_id)
            
        await ctx.send(tbl.compile())

    @commands.group(invoke_without_command=True)
    async def invites(self, ctx):
        pass

    @invites.command(name='prune')
    async def invites_prune(self, ctx, uses: int = 1):
        if not self.is_mod(ctx): return await ctx.send("Invalid permissions.")
        
        # d.py async fetch
        guild_invites = await ctx.guild.invites()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        
        invites_to_prune = [
            i for i in guild_invites
            if i.uses <= uses and i.created_at and i.created_at.replace(tzinfo=None) < (now - timedelta(hours=1))
        ]
        
        if not invites_to_prune:
            return await ctx.send('I didn\'t find any invites matching your criteria')

        msg = await ctx.send(
            'Ok, a total of {} invites created by {} users with {} total uses would be pruned. Please confirm.'.format(
                len(invites_to_prune),
                len({i.inviter.id for i in invites_to_prune if i.inviter}),
                sum(i.uses for i in invites_to_prune)
            ))
            
        await msg.add_reaction(GREEN_TICK_EMOJI)
        await msg.add_reaction(RED_TICK_EMOJI)

        def check(reaction, r_user):
            return r_user == ctx.author and reaction.message.id == msg.id and str(reaction.emoji) in (GREEN_TICK_EMOJI, RED_TICK_EMOJI)

        try:
            reaction, _ = await self.bot.wait_for('reaction_add', timeout=10.0, check=check)
        except asyncio.TimeoutError:
            await msg.edit(content='Not executing invite prune')
            await msg.clear_reactions()
            return

        if str(reaction.emoji) == GREEN_TICK_EMOJI:
            await msg.edit(content='Pruning invites...')
            for invite in invites_to_prune:
                try:
                    await invite.delete()
                    await asyncio.sleep(0.5) # Rate limit safety
                except discord.HTTPException:
                    pass
            await msg.edit(content='Ok, invite prune completed')
        else:
            await msg.edit(content='Not pruning invites')

    @commands.group(invoke_without_command=True)
    async def reactions(self, ctx):
        pass

    @reactions.command(name='clean')
    async def reactions_clean(self, ctx, user: discord.User, count: int = 10, emoji: str = None):
        if not self.is_mod(ctx): return await ctx.send("Invalid permissions.")
        if count > 50:
            return await ctx.send('cannot clean more than 50 reactions')

        def acquire_lock():
            lock = rdb.lock(f'clean-reactions-{user.id}')
            if not lock.acquire(blocking=False):
                return None
            return lock
            
        lock = await asyncio.to_thread(acquire_lock)
        if not lock:
            return await ctx.send('already running a clean on user')

        try:
            query_filters = [
                (Reaction.user_id == user.id),
                (Message.guild_id == ctx.guild.id),
                (Message.deleted == 0),
            ]

            if emoji:
                emoji_id = EMOJI_RE.findall(emoji)
                if emoji_id:
                    query_filters.append((Reaction.emoji_id == int(emoji_id[0])))
                else:
                    query_filters.append((Reaction.emoji_name == emoji))

            def fetch_reactions():
                reactions_query = Reaction.select(
                    Reaction.message_id,
                    Reaction.emoji_id,
                    Reaction.emoji_name,
                    Message.channel_id,
                ).join(
                    Message,
                    on=(Message.id == Reaction.message_id),
                ).where(
                    functools.reduce(operator.and_, query_filters)
                ).order_by(Reaction.message_id.desc()).limit(count).tuples()
                return list(reactions_query)

            reactions_list = await asyncio.to_thread(fetch_reactions)

            if not reactions_list:
                return await ctx.send('no reactions to purge')

            msg = await ctx.send(f'Hold on while I clean {len(reactions_list)} reactions')
            
            for message_id, emoji_id, emoji_name, channel_id in reactions_list:
                try:
                    # In d.py, we directly hit the HTTP endpoint to bypass object caching
                    emoji_str = f"{emoji_name}:{emoji_id}" if emoji_id else emoji_name
                    await self.bot.http.remove_reaction(channel_id, message_id, emoji_str, user.id)
                except discord.HTTPException:
                    pass
                await asyncio.sleep(0.5)
                
            await msg.edit(content=f'Ok, I cleaned {len(reactions_list)} reactions')
        finally:
            await asyncio.to_thread(lock.release)

    @commands.group(invoke_without_command=True)
    async def voice(self, ctx):
        pass

    @voice.command(name='log')
    async def voice_log(self, ctx, user: discord.User):
        if not self.is_mod(ctx): return await ctx.send("Invalid permissions.")
        
        def fetch_voice_sessions():
            sessions = GuildVoiceSession.select(
                GuildVoiceSession.user_id,
                GuildVoiceSession.channel_id,
                GuildVoiceSession.started_at,
                GuildVoiceSession.ended_at
            ).where(
                (GuildVoiceSession.user_id == user.id) &
                (GuildVoiceSession.guild_id == ctx.guild.id)
            ).order_by(GuildVoiceSession.started_at.desc()).limit(10)
            return list(sessions)

        sessions = await asyncio.to_thread(fetch_voice_sessions)

        tbl = MessageTable()
        tbl.set_header('Channel', 'Joined At', 'Duration')
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        
        for session in sessions:
            channel = self.bot.get_channel(session.channel_id)
            channel_name = str(channel.name) if channel else 'UNKNOWN'
            
            joined_str = '{} ({} ago)'.format(
                session.started_at.isoformat(),
                humanize.naturaldelta(now - session.started_at)
            )
            duration_str = humanize.naturaldelta(session.ended_at - session.started_at) if session.ended_at else 'Active'
            
            tbl.add(channel_name, joined_str, duration_str)
            
        await ctx.send(tbl.compile())

    @commands.command(aliases=['add', 'give'])
    async def join(self, ctx, name: str):
        config = getattr(ctx, 'base_config', None)
        if not config or not hasattr(config.plugins.admin, 'group_roles') or not config.plugins.admin.group_roles:
            return

        role_id = config.plugins.admin.group_roles.get(name.lower())
        if not role_id:
            return await ctx.send('invalid or unknown group')
            
        role = ctx.guild.get_role(role_id)
        if not role:
            return await ctx.send('invalid or unknown group')

        has_any_admin_perms = any(getattr(role.permissions, perm) for perm in (
            'kick_members', 'ban_members', 'administrator', 'manage_channels',
            'manage_guild', 'manage_messages', 'mention_everyone', 'mute_members',
            'move_members', 'manage_nicknames', 'manage_roles', 'manage_webhooks',
            'manage_emojis'
        ))

        if has_any_admin_perms:
            return await ctx.send('cannot join group with admin permissions')

        if role in ctx.author.roles:
            return await ctx.send('you are already a member of that group')

        await ctx.author.add_roles(role)
        
        if config.plugins.admin.group_confirm_reactions:
            await ctx.message.add_reaction(GREEN_TICK_EMOJI)
        else:
            await ctx.send(f'you have joined the {name} group')

    @commands.command(aliases=['remove', 'take'])
    async def leave(self, ctx, name: str):
        config = getattr(ctx, 'base_config', None)
        if not config or not hasattr(config.plugins.admin, 'group_roles') or not config.plugins.admin.group_roles:
            return

        role_id = config.plugins.admin.group_roles.get(name.lower())
        if not role_id:
            return await ctx.send('invalid or unknown group')
            
        role = ctx.guild.get_role(role_id)
        if not role or role not in ctx.author.roles:
            return await ctx.send('you are not a member of that group')

        await ctx.author.remove_roles(role)
        
        if config.plugins.admin.group_confirm_reactions:
            await ctx.message.add_reaction(GREEN_TICK_EMOJI)
        else:
            await ctx.send(f'you have left the {name} group')

    @role.command(name='unlock')
    async def unlock_role(self, ctx, role_id: int):
        if not self.is_mod(ctx): return await ctx.send("Invalid permissions.")
        
        config = getattr(ctx, 'base_config', None)
        locked_roles = getattr(config.plugins.admin, 'locked_roles', []) if config else []
        
        if role_id not in locked_roles:
            return await ctx.send(f'role {role_id} is not locked')

        if role_id in self.unlocked_roles and self.unlocked_roles[role_id] > time.time():
            return await ctx.send(f'role {role_id} is already unlocked')

        self.unlocked_roles[role_id] = time.time() + 300
        await ctx.send('role is unlocked for 5 minutes')

async def setup(bot):
    await bot.add_cog(AdminPlugin(bot))   