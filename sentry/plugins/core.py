import os
import json
import asyncio
import pprint
import signal
import inspect
import functools
import contextlib
import traceback
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks
import humanize

# Sentry internal imports
from sentry import ENV
from sentry.util import LocalProxy
from sentry.util.stats import timed
from sentry.sql import init_db
from sentry.redis import rdb
import sentry.models

# Peewee ORM patch
from peewee import ModelInsert

def patched_upsert(self, *args, **kwargs):
    target = kwargs.get('target') or (args[0] if args else None)
    if target:
        if isinstance(target, str): target = [target]
        return self.on_conflict(conflict_target=target, preserve=target)
    return self.on_conflict_ignore()

ModelInsert.upsert = patched_upsert

from sentry.models.guild import Guild, GuildBan
from sentry.models.message import Command as DBCommand
from sentry.models.notification import Notification
from sentry.plugins.modlog import Actions
from sentry.constants import (
    GREEN_TICK_EMOJI, RED_TICK_EMOJI, SENTRY_GUILD_ID, SENTRY_USER_ROLE_ID,
    SENTRY_CONTROL_CHANNEL
)

PY_CODE_BLOCK = '```py\n{}\n```'
BOT_INFO = 'Sentry is a moderation and utilitarian bot built for large Discord servers.'
GUILDS_WAITING_SETUP_KEY = 'gws'

# Custom Exception for command handling
class CommandResponse(Exception):
    def __init__(self, response):
        self.response = response

class CorePlugin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        init_db(ENV)
        self.startup = datetime.now(timezone.utc)
        self.guilds = getattr(self.bot, 'sentry_guilds', {})
        self.bot.sentry_guilds = self.guilds  # Store centrally on bot
        
        # Start background tasks
        self.wait_for_actions_task = self.bot.loop.create_task(self.wait_for_actions())
        self.update_guild_bans.start()

    async def cog_unload(self):
        self.wait_for_actions_task.cancel()
        self.update_guild_bans.cancel()

    async def wait_for_actions(self):
        # We must run the blocking Redis pubsub in a thread to prevent locking the asyncio loop
        def redis_listen():
            ps = rdb.pubsub()
            ps.subscribe('actions')
            for item in ps.listen():
                if item['type'] == 'message':
                    yield item

        while True:
            try:
                # Iterate through blocking redis listener in a thread-safe manner
                item = await asyncio.to_thread(next, redis_listen())
                data = json.loads(item['data'])

                if data['type'] == 'GUILD_UPDATE' and int(data['id']) in self.guilds:
                    guild_id = int(data['id'])
                    async with self.send_control_message() as embed:
                        embed.title = 'Reloaded config for {}'.format(self.guilds[guild_id].name)
                    
                    try:
                        config = await asyncio.to_thread(self.guilds[guild_id].get_config, refresh=True)
                        self.guilds[guild_id] = await asyncio.to_thread(Guild.with_id, guild_id)
                        await self.update_sentry_guild_access()
                        self.bot.dispatch('guild_config_update', self.guilds[guild_id], config)
                    except Exception as e:
                        print(f'Failed to reload config for guild {self.guilds[guild_id].name}: {e}')
                        continue
                
                elif data['type'] == 'RESTART':
                    print('Restart requested, signaling parent')
                    os.kill(os.getppid(), signal.SIGUSR1)
                
                elif data['type'] == 'GUILD_DELETE' and int(data['id']) in self.guilds:
                    guild_id = int(data['id'])
                    async with self.send_control_message() as embed:
                        embed.color = 0xff6961
                        embed.title = 'Guild Force Deleted {}'.format(self.guilds[guild_id].name)
                    
                    discord_guild = self.bot.get_guild(guild_id)
                    if discord_guild:
                        await discord_guild.leave()
            
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Error in wait_for_actions loop: {e}")
                await asyncio.sleep(5)

    async def update_sentry_guild_access(self):
        if SENTRY_GUILD_ID not in [g.id for g in self.bot.guilds] or ENV != 'prod':
            return
            
        rb_guild = self.bot.get_guild(SENTRY_GUILD_ID)
        if not rb_guild:
            return

        print('Updating sentry guild access')
        
        def fetch_guilds():
            return list(Guild.select(Guild.guild_id, Guild.config).where((Guild.enabled == 1)))
            
        guilds = await asyncio.to_thread(fetch_guilds)
        users_who_should_have_access = set()
        
        for guild in guilds:
            if 'web' not in guild.config:
                continue
            for user_id in guild.config['web'].keys():
                try:
                    users_who_should_have_access.add(int(user_id))
                except:
                    print(f'Guild {guild.guild_id} has invalid user ACLs: {guild.config["web"]}')

        sentry_role = rb_guild.get_role(SENTRY_USER_ROLE_ID)
        if not sentry_role:
            return

        users_who_have_access = {member.id for member in rb_guild.members if sentry_role in member.roles}
        
        remove_access = set(users_who_have_access) - set(users_who_should_have_access)
        add_access = set(users_who_should_have_access) - set(users_who_have_access)

        for user_id in remove_access:
            member = rb_guild.get_member(user_id)
            if member:
                await member.remove_roles(sentry_role)

        for user_id in add_access:
            member = rb_guild.get_member(user_id)
            if member:
                await member.add_roles(sentry_role)

    async def cog_check(self, ctx):
        if ctx.guild:
            guild_id = ctx.guild.id
        else:
            guild_id = None

        if guild_id not in self.guilds:
            return True

        if hasattr(self, 'WHITELIST_FLAG'):
            whitelist = await asyncio.to_thread(lambda: self.guilds[guild_id].whitelist)
            if not int(self.WHITELIST_FLAG) in whitelist:
                return False

        base_config = await asyncio.to_thread(self.guilds[guild_id].get_config)
        if not base_config:
            return False

        ctx.base_config = base_config
        plugin_name = self.qualified_name.lower().replace('plugin', '')
        
        if not getattr(ctx.base_config.plugins, plugin_name, None):
            return False

        self._attach_local_event_data(ctx, plugin_name, guild_id)
        return True

    def _attach_local_event_data(self, ctx, plugin_name, guild_id):
        if not hasattr(ctx, 'config'):
            ctx.config = LocalProxy()
        if not hasattr(ctx, 'rowboat_guild'):
            ctx.rowboat_guild = LocalProxy()
            
        ctx.config.set(getattr(ctx.base_config.plugins, plugin_name))
        ctx.rowboat_guild.set(self.guilds[guild_id])

    def get_config(self, guild_id, *args, **kwargs):
        return self.guilds[guild_id].get_config(*args, **kwargs)

    def get_guild(self, guild_id):
        return self.guilds[guild_id]

    @tasks.loop(seconds=290)
    async def update_guild_bans(self):
        def fetch_to_update():
            return [guild for guild in Guild.select().where(
                (Guild.last_ban_sync < (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1))) | 
                (Guild.last_ban_sync >> None)
            ) if guild.guild_id in [g.id for g in self.bot.guilds]]
            
        to_update = await asyncio.to_thread(fetch_to_update)
        for guild in to_update[:10]:
            discord_guild = self.bot.get_guild(guild.guild_id)
            if discord_guild:
                await asyncio.to_thread(guild.sync_bans, discord_guild)

    @update_guild_bans.before_loop
    async def before_update_guild_bans(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_guild_update(self, before, after):
        print(f'Got guild update for guild {after.id}')

    @commands.Cog.listener()
    async def on_member_ban(self, guild, user):
        await asyncio.to_thread(GuildBan.ensure, self.bot.get_guild(guild.id), user)

    @commands.Cog.listener()
    async def on_member_unban(self, guild, user):
        def delete_ban():
            GuildBan.delete().where((GuildBan.user_id == user.id) & (GuildBan.guild_id == guild.id)).execute()
        await asyncio.to_thread(delete_ban)

    @contextlib.asynccontextmanager
    async def send_control_message(self):
        embed = discord.Embed()
        embed.set_footer(text='Sentry {}'.format('Production' if ENV == 'prod' else 'Testing'))
        embed.timestamp = datetime.now(timezone.utc)
        embed.color = 0x779ecb
        try:
            yield embed
            control_channel = self.bot.get_channel(SENTRY_CONTROL_CHANNEL)
            if control_channel:
                await control_channel.send(embed=embed)
        except Exception as e:
            print(f'Failed to send control message: {e}')

    @commands.Cog.listener()
    async def on_resumed(self):
        await asyncio.to_thread(Notification.dispatch, Notification.Types.RESUME, env=ENV)
        async with self.send_control_message() as embed:
            embed.title = 'Resumed'
            embed.color = 0xffb347

    @commands.Cog.listener()
    async def on_ready(self):
        print('Started session')
        await asyncio.to_thread(Notification.dispatch, Notification.Types.CONNECT, env=ENV)
        async with self.send_control_message() as embed:
            embed.title = 'Connected'
            embed.color = 0x77dd77

    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        try:
            db_guild = await asyncio.to_thread(Guild.with_id, guild.id)
        except Guild.DoesNotExist:
            is_waiting = await asyncio.to_thread(rdb.sismember, GUILDS_WAITING_SETUP_KEY, str(guild.id))
            if not is_waiting and guild.id != SENTRY_GUILD_ID:
                print(f'Leaving guild {guild.id} ({guild.name}), not within setup list')
                await guild.leave()
            return

        if not db_guild.enabled:
            return

        config = await asyncio.to_thread(db_guild.get_config)
        if not config:
            return

        print(f'Syncing guild {guild.id}')
        await asyncio.to_thread(db_guild.sync, guild)
        self.guilds[guild.id] = db_guild

        if config.nickname:
            await asyncio.sleep(5)
            me = guild.me
            if me and me.nick != config.nickname:
                try:
                    await me.edit(nick=config.nickname)
                except discord.HTTPException as e:
                    print(f'Failed to set nickname for guild {guild.name}: {e}')

    def get_level(self, guild_id, user):
        config = self.guilds[guild_id].get_config() if guild_id in self.guilds else None
        user_level = 0
        if config:
            member = user
            if not member:
                return user_level
            for role in member.roles:
                if role.id in config.levels and config.levels[role.id] > user_level:
                    user_level = config.levels[role.id]
            if member.id in config.levels:
                user_level = config.levels[member.id]
        return user_level

    @commands.command()
    async def setup(self, ctx):
        if not ctx.guild:
            return await ctx.send(':warning: this command can only be used in servers')
        if ctx.guild.id in self.guilds:
            return await ctx.send(':warning: this server is already setup')

        global_admin = await asyncio.to_thread(rdb.sismember, 'global_admins', ctx.author.id)
        if not global_admin:
            if not ctx.guild.owner_id == ctx.author.id:
                return await ctx.send(':warning: only the server owner can setup Sentry')

        me = ctx.guild.me
        if not me.guild_permissions.administrator and not global_admin:
            return await ctx.send(':warning: bot must have the Administrator permission')

        guild = await asyncio.to_thread(Guild.setup, ctx.guild)
        await asyncio.to_thread(rdb.srem, GUILDS_WAITING_SETUP_KEY, str(ctx.guild.id))
        self.guilds[ctx.guild.id] = guild
        await ctx.send(':ok_hand: successfully loaded configuration')

    @commands.command()
    async def about(self, ctx):
        embed = discord.Embed(description=BOT_INFO)
        if self.bot.user.display_avatar:
            embed.set_author(name='Sentry', icon_url=self.bot.user.display_avatar.url)
        else:
            embed.set_author(name='Sentry')
            
        guild_count = await asyncio.to_thread(lambda: Guild.select().count())
        embed.add_field(name='Servers', value=str(guild_count), inline=True)
        
        uptime_delta = datetime.now(timezone.utc) - self.startup
        embed.add_field(name='Uptime', value=humanize.naturaldelta(uptime_delta), inline=True)
        
        await ctx.send(embed=embed)

    @commands.command()
    async def uptime(self, ctx):
        uptime_delta = datetime.now(timezone.utc) - self.startup
        await ctx.send('Sentry was started {}'.format(humanize.naturaldelta(uptime_delta)))

    @commands.command()
    async def source(self, ctx, *, command_name: str = None):
        if not command_name:
            return await ctx.send("Please provide a command name.")
            
        cmd = self.bot.get_command(command_name)
        if not cmd:
            await ctx.send("Couldn't find command for `{}`".format(discord.utils.escape_markdown(command_name)))
            return
            
        code = cmd.callback.__code__
        lines, firstlineno = inspect.getsourcelines(code)
        filename = os.path.basename(code.co_filename)
        
        await ctx.send('<https://github.com/designategold7/Sentry/blob/master/{}#L{}-{}>'.format(
            filename, firstlineno, firstlineno + len(lines)
        ))

    @commands.command(name='eval')
    async def _eval(self, ctx, *, codeblock: str):
        src = codeblock.strip('` ')
        if src.startswith('py\n'):
            src = src[3:]

        env = {
            'bot': self.bot,
            'client': self.bot,  
            'state': self.bot,   
            'ctx': ctx,
            'msg': ctx.message,
            'guild': ctx.guild,
            'channel': ctx.channel,
            'author': ctx.author
        }
        env.update(globals())

        if '\n' in src:
            lines = list(filter(bool, src.split('\n')))
            if lines[-1] and 'return' not in lines[-1]:
                lines[-1] = 'return ' + lines[-1]
            lines = '\n'.join('    ' + i for i in lines)
            code = 'async def f():\n{}'.format(lines)
            local_env = {}
            try:
                exec(compile(code, '<eval>', 'exec'), env, local_env)
                result_obj = await local_env['f']()
                result = pprint.pformat(result_obj)
            except Exception as e:
                await ctx.send(PY_CODE_BLOCK.format(type(e).__name__ + ': ' + str(e)))
                return
        else:
            try:
                result = str(eval(src, env))
            except Exception as e:
                await ctx.send(PY_CODE_BLOCK.format(type(e).__name__ + ': ' + str(e)))
                return

        if len(result) > 1990:
            import io
            fp = io.BytesIO(result.encode('utf-8'))
            await ctx.send(file=discord.File(fp, 'result.txt'))
        else:
            await ctx.send(PY_CODE_BLOCK.format(result))

    @commands.group(invoke_without_command=True)
    async def control(self, ctx):
        pass

    @control.command(name='sync-bans')
    async def control_sync_bans(self, ctx):
        def fetch_enabled_guilds():
            return list(Guild.select().where(Guild.enabled == 1))
            
        guilds = await asyncio.to_thread(fetch_enabled_guilds)
        msg = await ctx.send(':timer: pls wait while I sync...')
        
        for guild in guilds:
            discord_guild = self.bot.get_guild(guild.guild_id)
            if discord_guild:
                await asyncio.to_thread(guild.sync_bans, discord_guild)
                
        await msg.edit(content='<:{}> synced {} guilds'.format(GREEN_TICK_EMOJI, len(guilds)))

    @control.command(name='reconnect')
    async def control_reconnect(self, ctx):
        await ctx.send('Ok, closing connection')
        await self.bot.close()

    @commands.group(invoke_without_command=True)
    async def guilds_group(self, ctx):
        pass

    @guilds_group.command(name='invite')
    async def guild_join(self, ctx, guild_id: int):
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return await ctx.send(':no_entry_sign: invalid or unknown guild ID')
            
        msg = await ctx.send('Ok, hold on while I get you setup with an invite link to {}'.format(guild.name))
        
        general_channel = next((c for c in guild.text_channels if c.permissions_for(guild.me).create_instant_invite), None)
        if not general_channel:
            return await msg.edit(content=':no_entry_sign: I lack permissions to create an invite for {}'.format(guild.name))
            
        try:
            invite = await general_channel.create_invite(max_age=300, max_uses=1, unique=True)
        except Exception:
            return await msg.edit(content=':no_entry_sign: Hmmm, something went wrong creating an invite for {}'.format(guild.name))
            
        await msg.edit(content='Ok, here is a temporary invite for you: {}'.format(invite.url))

    @guilds_group.command(name='wh')
    async def guild_whitelist(self, ctx, guild_id: int):
        await asyncio.to_thread(rdb.sadd, GUILDS_WAITING_SETUP_KEY, str(guild_id))
        await ctx.send('Ok, guild %s is now in the whitelist' % guild_id)

    @guilds_group.command(name='unwh')
    async def guild_unwhitelist(self, ctx, guild_id: int):
        await asyncio.to_thread(rdb.srem, GUILDS_WAITING_SETUP_KEY, str(guild_id))
        await ctx.send('Ok, I\'ve made sure guild %s is no longer in the whitelist' % guild_id)

    @commands.group(invoke_without_command=True)
    async def plugins(self, ctx):
        pass

    @plugins.command(name='disable')
    async def plugin_disable(self, ctx, plugin_name: str):
        if not self.bot.get_cog(plugin_name):
            return await ctx.send('Hmmm, it appears that plugin doesn\'t exist!?')
            
        await self.bot.remove_cog(plugin_name)
        await ctx.send('Ok, that plugin has been disabled and unloaded')


async def setup(bot):
    await bot.add_cog(CorePlugin(bot))