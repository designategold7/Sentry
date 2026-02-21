import os
import json
import gevent
import pprint
import signal
import inspect
import humanize
import functools
import contextlib
from datetime import datetime, timedelta, timezone
from holster.emitter import Priority, Emitter
from disco.bot import Bot
from disco.types.message import MessageEmbed
from disco.api.http import APIException
from disco.bot.command import CommandEvent
from disco.util.sanitize import S
from sentry import ENV
from sentry.util import LocalProxy
from sentry.util.stats import timed
from sentry.plugins import BasePlugin as Plugin
from sentry.plugins import CommandResponse
from sentry.sql import init_db
from sentry.redis import rdb
import sentry.models
from peewee import ModelInsert
if not hasattr(ModelInsert, 'upsert'):
    ModelInsert.upsert = lambda self, *args, **kwargs: self.on_conflict_replace()
from sentry.models.guild import Guild, GuildBan
from sentry.models.message import Command
from sentry.models.notification import Notification
from sentry.plugins.modlog import Actions
from sentry.constants import (
    GREEN_TICK_EMOJI, RED_TICK_EMOJI, SENTRY_GUILD_ID, SENTRY_USER_ROLE_ID,
    SENTRY_CONTROL_CHANNEL
)
PY_CODE_BLOCK = '```py\n{}\n```'
BOT_INFO = 'Sentry is a moderation and utilitarian bot built for large Discord servers.'
GUILDS_WAITING_SETUP_KEY = 'gws'
class CorePlugin(Plugin):
    def load(self, ctx):
        init_db(ENV)
        self.startup = ctx.get('startup', datetime.now(timezone.utc))
        self.guilds = ctx.get('guilds', {})
        self.emitter = Emitter(gevent.spawn)
        super(CorePlugin, self).load(ctx)
        self.bot.add_plugin = self.our_add_plugin
        self._wait_for_actions_greenlet = self.spawn(self.wait_for_actions)
    def spawn_wait_for_actions(self, *args, **kwargs):
        self._wait_for_actions_greenlet = self.spawn(self.wait_for_actions)
        self._wait_for_actions_greenlet.link_exception(self.spawn_wait_for_actions)
    def our_add_plugin(self, cls, *args, **kwargs):
        if getattr(cls, 'global_plugin', False):
            Bot.add_plugin(self.bot, cls, *args, **kwargs)
            return
        inst = cls(self.bot, None)
        inst.register_trigger('command', 'pre', functools.partial(self.on_pre, inst))
        inst.register_trigger('listener', 'pre', functools.partial(self.on_pre, inst))
        Bot.add_plugin(self.bot, inst, *args, **kwargs)
    def wait_for_actions(self):
        ps = rdb.pubsub()
        ps.subscribe('actions')
        for item in ps.listen():
            if item['type'] != 'message':
                continue
            data = json.loads(item['data'])
            if data['type'] == 'GUILD_UPDATE' and data['id'] in self.guilds:
                with self.send_control_message() as embed:
                    embed.title = 'Reloaded config for {}'.format(self.guilds[data['id']].name)
                self.log.info('Reloading guild %s', self.guilds[data['id']].name)
                try:
                    config = self.guilds[data['id']].get_config(refresh=True)
                    self.guilds[data['id']] = Guild.with_id(data['id'])
                    self.update_sentry_guild_access()
                    self.emitter.emit('GUILD_CONFIG_UPDATE', self.guilds[data['id']], config)
                except:
                    self.log.exception('Failed to reload config for guild %s', self.guilds[data['id']].name)
                    continue
            elif data['type'] == 'RESTART':
                self.log.info('Restart requested, signaling parent')
                os.kill(os.getppid(), signal.SIGUSR1)
            elif data['type'] == 'GUILD_DELETE' and data['id'] in self.guilds:
                with self.send_control_message() as embed:
                    embed.color = 0xff6961
                    embed.title = 'Guild Force Deleted {}'.format(self.guilds[data['id']].name,)
                self.log.info('Leaving guild %s', self.guilds[data['id']].name)
                self.guilds[data['id']].leave()
    def unload(self, ctx):
        ctx['guilds'] = self.guilds
        ctx['startup'] = self.startup
        super(CorePlugin, self).unload(ctx)
    def update_sentry_guild_access(self):
        if SENTRY_GUILD_ID not in self.state.guilds or ENV != 'prod':
            return
        rb_guild = self.state.guilds.get(SENTRY_GUILD_ID)
        if not rb_guild:
            return
        self.log.info('Updating sentry guild access')
        guilds = Guild.select(Guild.guild_id, Guild.config).where((Guild.enabled == 1))
        users_who_should_have_access = set()
        for guild in guilds:
            if 'web' not in guild.config:
                continue
            for user_id in guild.config['web'].keys():
                try:
                    users_who_should_have_access.add(int(user_id))
                except:
                    self.log.warning('Guild %s has invalid user ACLs: %s', guild.guild_id, guild.config['web'])
        users_who_have_access = {i.id for i in rb_guild.members.values() if SENTRY_USER_ROLE_ID in i.roles}
        remove_access = set(users_who_have_access) - set(users_who_should_have_access)
        add_access = set(users_who_should_have_access) - set(users_who_have_access)
        for user_id in remove_access:
            member = rb_guild.members.get(user_id)
            if member:
                member.remove_role(SENTRY_USER_ROLE_ID)
        for user_id in add_access:
            member = rb_guild.members.get(user_id)
            if member:
                member.add_role(SENTRY_USER_ROLE_ID)
    def on_pre(self, plugin, func, event, args, kwargs):
        if hasattr(event, 'guild') and event.guild:
            guild_id = event.guild.id
        elif hasattr(event, 'guild_id') and event.guild_id:
            guild_id = event.guild_id
        else:
            guild_id = None
        if guild_id not in self.guilds:
            if isinstance(event, CommandEvent):
                if event.command.metadata.get('global_', False):
                    return event
            elif hasattr(func, 'subscriptions'):
                if func.subscriptions[0].metadata.get('global_', False):
                    return event
            return
        if hasattr(plugin, 'WHITELIST_FLAG'):
            if not int(plugin.WHITELIST_FLAG) in self.guilds[guild_id].whitelist:
                return
        event.base_config = self.guilds[guild_id].get_config()
        if not event.base_config:
            return
        plugin_name = plugin.name.lower().replace('plugin', '')
        if not getattr(event.base_config.plugins, plugin_name, None):
            return
        self._attach_local_event_data(event, plugin_name, guild_id)
        return event
    def get_config(self, guild_id, *args, **kwargs):
        return self.guilds[guild_id].get_config(*args, **kwargs)
    def get_guild(self, guild_id):
        return self.guilds[guild_id]
    def _attach_local_event_data(self, event, plugin_name, guild_id):
        if not hasattr(event, 'config'):
            event.config = LocalProxy()
        if not hasattr(event, 'rowboat_guild'):
            event.rowboat_guild = LocalProxy()
        event.config.set(getattr(event.base_config.plugins, plugin_name))
        event.rowboat_guild.set(self.guilds[guild_id])
    @Plugin.schedule(290, init=False)
    def update_guild_bans(self):
        to_update = [guild for guild in Guild.select().where((Guild.last_ban_sync < (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1))) | (Guild.last_ban_sync >> None)) if guild.guild_id in self.client.state.guilds]
        for guild in to_update[:10]:
            guild.sync_bans(self.client.state.guilds.get(guild.guild_id))
    @Plugin.listen('GuildUpdate')
    def on_guild_update(self, event):
        self.log.info('Got guild update for guild %s (%s)', event.guild.id, event.guild.channels)
    @Plugin.listen('GuildBanAdd')
    def on_guild_ban_add(self, event):
        GuildBan.ensure(self.client.state.guilds.get(event.guild_id), event.user)
    @Plugin.listen('GuildBanRemove')
    def on_guild_ban_remove(self, event):
        GuildBan.delete().where((GuildBan.user_id == event.user.id) & (GuildBan.guild_id == event.guild_id))
    @contextlib.contextmanager
    def send_control_message(self):
        embed = MessageEmbed()
        embed.set_footer(text='Sentry {}'.format('Production' if ENV == 'prod' else 'Testing'))
        embed.timestamp = datetime.now(timezone.utc).isoformat()
        embed.color = 0x779ecb
        try:
            yield embed
            self.bot.client.api.channels_messages_create(SENTRY_CONTROL_CHANNEL, embed=embed)
        except:
            self.log.exception('Failed to send control message:')
            return
    @Plugin.listen('Resumed')
    def on_resumed(self, event):
        Notification.dispatch(Notification.Types.RESUME, trace=event.trace, env=ENV)
        with self.send_control_message() as embed:
            embed.title = 'Resumed'
            embed.color = 0xffb347
            trace_str = ' / '.join(event.trace) if event.trace else 'Unknown'
            embed.add_field(name='Gateway Trace', value=trace_str, inline=False)
            embed.add_field(name='Replayed Events', value=str(self.client.gw.replayed_events))
    @Plugin.listen('Ready', priority=Priority.BEFORE)
    def on_ready(self, event):
        self.log.info('Started session %s', event.session_id)
        Notification.dispatch(Notification.Types.CONNECT, trace=event.trace, env=ENV)
        with self.send_control_message() as embed:
            embed.title = 'Connected'
            embed.color = 0x77dd77
            trace_str = ' / '.join(event.trace) if event.trace else 'Unknown'
            embed.add_field(name='Gateway Trace', value=trace_str, inline=False)
    @Plugin.listen('GuildCreate', priority=Priority.BEFORE, conditional=lambda e: not e.created)
    def on_guild_create(self, event):
        try:
            guild = Guild.with_id(event.id)
        except Guild.DoesNotExist:
            if not rdb.sismember(GUILDS_WAITING_SETUP_KEY, str(event.id)) and event.id != SENTRY_GUILD_ID:
                self.log.warning('Leaving guild %s (%s), not within setup list', event.id, event.name)
                event.guild.leave()
            return
        if not guild.enabled:
            return
        config = guild.get_config()
        if not config:
            return
        self.log.info('Syncing guild %s', event.guild.id)
        guild.sync(event.guild)
        self.guilds[event.id] = guild
        if config.nickname:
            def set_nickname():
                m = event.members.select_one(id=self.state.me.id)
                if m and m.nick != config.nickname:
                    try:
                        m.set_nickname(config.nickname)
                    except APIException as e:
                        self.log.warning('Failed to set nickname for guild %s (%s)', event.guild, e.content)
            self.spawn_later(5, set_nickname)
    def get_level(self, guild, user):
        config = (guild.id in self.guilds and self.guilds.get(guild.id).get_config())
        user_level = 0
        if config:
            member = guild.get_member(user)
            if not member:
                return user_level
            for oid in member.roles:
                if oid in config.levels and config.levels[oid] > user_level:
                    user_level = config.levels[oid]
            if member.id in config.levels:
                user_level = config.levels[member.id]
        return user_level
    @Plugin.listen('MessageCreate')
    def on_message_create(self, event):
        if event.message.author.bot:
            return
        if hasattr(event, 'guild') and event.guild:
            guild_id = event.guild.id
        elif hasattr(event, 'guild_id') and event.guild_id:
            guild_id = event.guild_id
        else:
            guild_id = None
        guild = self.guilds.get(event.guild.id) if guild_id else None
        config = guild and guild.get_config()
        if config and config.commands:
            commands = list(self.bot.get_commands_for_message(config.commands.mention, {}, config.commands.prefix, event.message))
        elif guild_id:
            commands = list(self.bot.get_commands_for_message(True, {}, '', event.message))
        else:
            if ENV != 'prod':
                if not event.message.content.startswith(ENV + '!'):
                    return
                event.message.content = event.message.content[len(ENV) + 1:]
            commands = list(self.bot.get_commands_for_message(False, {}, '', event.message))
        if not len(commands):
            return
        event.user_level = self.get_level(event.guild, event.author) if event.guild else 0
        global_admin = rdb.sismember('global_admins', event.author.id)
        for command, match in commands:
            if command.level == -1 and not global_admin:
                continue
            level = command.level
            if guild and not config and command.triggers[0] != 'setup':
                continue
            elif config and config.commands and command.plugin != self:
                overrides = {}
                for obj in config.commands.get_command_override(command):
                    overrides.update(obj)
                if overrides.get('disabled'):
                    continue
                level = overrides.get('level', level)
            if not global_admin and event.user_level < level:
                continue
            with timed('sentry.command.duration', tags={'plugin': command.plugin.name, 'command': command.name}):
                try:
                    command_event = CommandEvent(command, event.message, match)
                    command_event.user_level = event.user_level
                    command.plugin.execute(command_event)
                except CommandResponse as e:
                    event.reply(e.response)
                except:
                    tracked = Command.track(event, command, exception=True)
                    self.log.exception('Command error:')
                    with self.send_control_message() as embed:
                        embed.title = 'Command Error: {}'.format(command.name)
                        embed.color = 0xff6961
                        embed.add_field(name='Author', value='({}) `{}`'.format(event.author, event.author.id), inline=True)
                        embed.add_field(name='Channel', value='({}) `{}`'.format(event.channel.name, event.channel.id), inline=True)
                        embed.description = '```{}```'.format('\n'.join(tracked.traceback.split('\n')[-8:]))
                    return event.reply('<:{}> something went wrong, perhaps try again later'.format(RED_TICK_EMOJI))
            Command.track(event, command)
            if config:
                modlog_config = getattr(config.plugins, 'modlog', None)
                if not modlog_config:
                    return
                self._attach_local_event_data(event, 'modlog', event.guild.id)
                plugin = self.bot.plugins.get('ModLogPlugin')
                if plugin:
                    plugin.log_action(Actions.COMMAND_USED, event)
            return
    @Plugin.command('setup')
    def command_setup(self, event):
        if not event.guild:
            return event.msg.reply(':warning: this command can only be used in servers')
        if event.guild.id in self.guilds:
            return event.msg.reply(':warning: this server is already setup')
        global_admin = rdb.sismember('global_admins', event.author.id)
        if not global_admin:
            if not event.guild.owner_id == event.author.id:
                return event.msg.reply(':warning: only the server owner can setup Sentry')
        m = event.guild.members.select_one(id=self.state.me.id)
        if not m.permissions.administrator and not global_admin:
            return event.msg.reply(':warning: bot must have the Administrator permission')
        guild = Guild.setup(event.guild)
        rdb.srem(GUILDS_WAITING_SETUP_KEY, str(event.guild.id))
        self.guilds[event.guild.id] = guild
        event.msg.reply(':ok_hand: successfully loaded configuration')
    @Plugin.command('about')
    def command_about(self, event):
        embed = MessageEmbed()
        embed.set_author(name='Sentry', icon_url=self.client.state.me.avatar_url)
        embed.description = BOT_INFO
        embed.add_field(name='Servers', value=str(Guild.select().count()), inline=True)
        embed.add_field(name='Uptime', value=humanize.naturaldelta(datetime.now(timezone.utc) - self.startup), inline=True)
        event.msg.reply(embed=embed)
    @Plugin.command('uptime', level=-1)
    def command_uptime(self, event):
        event.msg.reply('Sentry was started {}'.format(humanize.naturaldelta(datetime.now(timezone.utc) - self.startup)))
    @Plugin.command('source', '<command>', level=-1)
    def command_source(self, event, command=None):
        for cmd in self.bot.commands:
            if command.lower() in cmd.triggers:
                break
        else:
            event.msg.reply("Couldn't find command for `{}`".format(S(command, escape_codeblocks=True)))
            return
        code = cmd.func.__code__
        lines, firstlineno = inspect.getsourcelines(code)
        event.msg.reply('<https://github.com/b1naryth1ef/rowboat/blob/master/{}#L{}-{}>'.format(code.co_filename, firstlineno, firstlineno + len(lines)))
    @Plugin.command('eval', level=-1)
    def command_eval(self, event):
        ctx = {'bot': self.bot, 'client': self.bot.client, 'state': self.bot.client.state, 'event': event, 'msg': event.msg, 'guild': event.msg.guild, 'channel': event.msg.channel, 'author': event.msg.author}
        src = event.codeblock
        if src.count('\n'):
            lines = list(filter(bool, src.split('\n')))
            if lines[-1] and 'return' not in lines[-1]:
                lines[-1] = 'return ' + lines[-1]
            lines = '\n'.join('    ' + i for i in lines)
            code = 'def f():\n{}\nx = f()'.format(lines)
            local = {}
            try:
                exec(compile(code, '<eval>', 'exec'), ctx, local)
            except Exception as e:
                event.msg.reply(PY_CODE_BLOCK.format(type(e).__name__ + ': ' + str(e)))
                return
            result = pprint.pformat(local['x'])
        else:
            try:
                result = str(eval(src, ctx))
            except Exception as e:
                event.msg.reply(PY_CODE_BLOCK.format(type(e).__name__ + ': ' + str(e)))
                return
        if len(result) > 1990:
            event.msg.reply('', attachments=[('result.txt', result)])
        else:
            event.msg.reply(PY_CODE_BLOCK.format(result))
    @Plugin.command('sync-bans', group='control', level=-1)
    def control_sync_bans(self, event):
        guilds = list(Guild.select().where(Guild.enabled == 1))
        msg = event.msg.reply(':timer: pls wait while I sync...')
        for guild in guilds:
            guild.sync_bans(self.client.state.guilds.get(guild.guild_id))
        msg.edit('<:{}> synced {} guilds'.format(GREEN_TICK_EMOJI, len(guilds)))
    @Plugin.command('reconnect', group='control', level=-1)
    def control_reconnect(self, event):
        event.msg.reply('Ok, closing connection')
        self.client.gw.ws.close()
    @Plugin.command('invite', '<guild:snowflake>', group='guilds', level=-1)
    def guild_join(self, event, guild):
        guild = self.state.guilds.get(guild)
        if not guild:
            return event.msg.reply(':no_entry_sign: invalid or unknown guild ID')
        msg = event.msg.reply('Ok, hold on while I get you setup with an invite link to {}'.format(guild.name,))
        general_channel = next(iter(guild.channels.values()))
        try:
            invite = general_channel.create_invite(max_age=300, max_uses=1, unique=True,)
        except:
            return msg.edit(':no_entry_sign: Hmmm, something went wrong creating an invite for {}'.format(guild.name,))
        msg.edit('Ok, here is a temporary invite for you: {}'.format(invite.code,))
    @Plugin.command('wh', '<guild:snowflake>', group='guilds', level=-1)
    def guild_whitelist(self, event, guild):
        rdb.sadd(GUILDS_WAITING_SETUP_KEY, str(guild))
        event.msg.reply('Ok, guild %s is now in the whitelist' % guild)
    @Plugin.command('unwh', '<guild:snowflake>', group='guilds', level=-1)
    def guild_unwhitelist(self, event, guild):
        rdb.srem(GUILDS_WAITING_SETUP_KEY, str(guild))
        event.msg.reply('Ok, I\'ve made sure guild %s is no longer in the whitelist' % guild)
    @Plugin.command('disable', '<plugin:str>', group='plugins', level=-1)
    def plugin_disable(self, event, plugin):
        plugin = self.bot.plugins.get(plugin)
        if not plugin:
            return event.msg.reply('Hmmm, it appears that plugin doesn\'t exist!?')
        self.bot.rmv_plugin(plugin.__class__)
        event.msg.reply('Ok, that plugin has been disabled and unloaded')