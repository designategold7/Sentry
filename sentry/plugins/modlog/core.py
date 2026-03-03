import re
import time
import pytz
import string
import asyncio
import operator
import humanize
from functools import reduce
from holster.enum import Enum
from datetime import datetime, timezone
from collections import defaultdict

import discord
from discord.ext import commands, tasks

# Sentry internal imports
from sentry.types import SlottedModel, Field, ListField, DictField, ChannelField, snowflake
from sentry.types.plugin import PluginConfig
from sentry.models.message import Message, MessageArchive
from sentry.models.guild import Guild
from sentry.util import ordered_load, MetaException
from sentry.plugins.modlog.pump import ModLogPump

Actions = Enum()
URL_REGEX = re.compile(r'(https?://[^\s]+)')

def filter_urls(content):
    return URL_REGEX.sub(r'<\1>', content)

class ChannelConfig(SlottedModel):
    compact = Field(bool, default=True)
    include = ListField(Actions)
    exclude = ListField(Actions)
    rich = ListField(Actions)
    timestamps = Field(bool, default=False)
    timezone = Field(str, default='US/Eastern')

    def validate(self):
        assert pytz.timezone(self.timezone) is not None

    @property
    def tz(self):
        if not hasattr(self, '_tz'):
            self._tz = pytz.timezone(self.timezone)
        return self._tz

    @property
    def subscribed(self):
        if not hasattr(self, '_subscribed'):
            include = set(self.include if self.include else Actions.attrs)
            exclude = set(self.exclude if self.exclude else [])
            self._subscribed = include - exclude
        return self._subscribed

class CustomFormat(SlottedModel):
    emoji = Field(str, default=None)
    format = Field(str, default=None)

class ModLogConfig(PluginConfig):
    resolved = Field(bool, default=False, private=True)
    ignored_users = ListField(snowflake)
    ignored_channels = ListField(snowflake)
    custom = DictField(str, CustomFormat)
    channels = DictField(ChannelField, ChannelConfig)
    new_member_threshold = Field(int, default=(15 * 60))
    _custom = DictField(dict, private=True)
    _channels = DictField(ChannelConfig, private=True)

    @property
    def subscribed(self):
        if not hasattr(self, '_subscribed'):
            self._subscribed = reduce(operator.or_, (i.subscribed for i in self.channels.values())) if self.channels else set()
        return self._subscribed

class Formatter(string.Formatter):
    def convert_field(self, value, conversion):
        if conversion in ('z', 's'):
            return discord.utils.escape_code_blocks(discord.utils.escape_markdown(str(value)))
        return str(value)

class Debounce(object):
    def __init__(self, plugin, guild_id, selector, events):
        self.plugin = plugin
        self.guild_id = guild_id
        self.selector = selector
        self.events = events
        self.timestamp = time.time()

    def is_expired(self):
        return time.time() - self.timestamp > 60

    def remove(self, event=None):
        self.plugin.debounces.remove(self, event)

class DebouncesCollection(object):
    def __init__(self):
        self._data = defaultdict(lambda: defaultdict(list))

    def __iter__(self):
        for top in self._data.values():
            for bot in top.values():
                for obj in bot:
                    yield obj

    def add(self, obj):
        for event_name in obj.events:
            self._data[obj.guild_id][event_name].append(obj)

    def remove(self, obj, event=None):
        for event_name in ([event] if event else obj.events):
            if event_name in obj.events:
                obj.events.remove(event_name)
            if obj in self._data[obj.guild_id][event_name]:
                self._data[obj.guild_id][event_name].remove(obj)

    def find(self, event, delete=True, **kwargs):
        guild_id = getattr(event, 'guild_id', getattr(getattr(event, 'guild', None), 'id', None))
        if not guild_id: return None
        
        event_name = event.__class__.__name__ if not isinstance(event, str) else event
        
        for obj in list(self._data[guild_id][event_name]):
            if obj.is_expired():
                obj.remove()
                continue
            
            match = True
            for k, v in kwargs.items():
                if obj.selector.get(k) != v:
                    match = False
                    break
                    
            if not match:
                continue
                
            if delete:
                obj.remove(event=event_name)
            return obj
        return None


class ModLogPlugin(commands.Cog):
    fmt = Formatter()

    def __init__(self, bot):
        self.bot = bot
        self.hushed = {}
        self.pumps = {}
        
        # State loaded natively
        self.action_simple = {}
        if not Actions.attrs:
            with open('data/actions_simple.yaml') as f:
                simple = ordered_load(f.read())
            for k, v in simple.items():
                self.register_action(k, v)
        else:
            # Fallback if actions were pre-loaded globally
            pass 
            
        self.debounces = getattr(self.bot, 'modlog_debounces', DebouncesCollection())
        self.bot.modlog_debounces = self.debounces
        
        self.cleanup_debounce.start()

    async def cog_unload(self):
        self.cleanup_debounce.cancel()

    async def create_debounce(self, event_or_guild_id, events, **kwargs):
        if isinstance(event_or_guild_id, int):
            guild_id = event_or_guild_id
        else:
            guild_id = getattr(event_or_guild_id, 'guild_id', getattr(getattr(event_or_guild_id, 'guild', None), 'id', None))
            
        bounce = Debounce(self, guild_id, kwargs, events)
        self.debounces.add(bounce)
        return bounce

    async def resolve_channels(self, guild, config):
        channels = {}
        for key, channel_config in config.channels.items():
            if isinstance(key, int):
                chan = guild.get_channel(key)
            else:
                chan = discord.utils.get(guild.channels, name=key)
                
            if not chan:
                print(f"Failed to ModLog.resolve_channels for {guild.name}")
                continue
            channels[chan.id] = channel_config
            
        config._channels = channels
        config._custom = None
        
        if config.custom:
            core = self.bot.get_cog('CorePlugin')
            if core:
                sentry_guild = core.get_guild(guild.id)
                if sentry_guild and sentry_guild.is_whitelisted(Guild.WhitelistFlags.MODLOG_CUSTOM_FORMAT):
                    custom = {}
                    for action_name, override in config.custom.items():
                        action = Actions.get(action_name)
                        if not action:
                            continue
                        custom[action] = override.to_dict()
                        if not custom[action].get('emoji'):
                            custom[action]['emoji'] = self.action_simple[action]['emoji']
                    config._custom = custom
                    
        config.resolved = True

    def register_action(self, name, simple):
        action = Actions.add(name)
        self.action_simple[action] = simple

    async def log_action_ext(self, action, guild_id, **details):
        core = self.bot.get_cog('CorePlugin')
        if not core: return
        
        config = core.get_config(guild_id)
        if not config or not hasattr(config.plugins, 'modlog'):
            return
            
        guild = self.bot.get_guild(guild_id)
        if not guild: return
        
        await self.log_action_raw(
            action,
            guild,
            getattr(config.plugins, 'modlog'),
            **details)

    async def log_action(self, action, event, **details):
        details['e'] = event
        guild = getattr(event, 'guild', None)
        if not guild: return
        
        core = self.bot.get_cog('CorePlugin')
        if not core: return
        config = core.get_config(guild.id)
        if not config or not hasattr(config.plugins, 'modlog'):
            return
            
        await self.log_action_raw(action, guild, config.plugins.modlog, **details)

    async def log_action_raw(self, action, guild, config, **details):
        if not config: return
        
        if not config.resolved:
            await self.resolve_channels(guild, config)
            
        if not {action} & config.subscribed:
            return

        def generate_simple(chan_config):
            info = self.action_simple.get(action)
            if config._custom and action in config._custom:
                info = config._custom[action]
                
            contents = self.fmt.format(str(info['format']), **details)
            msg = f":{info['emoji']}: {contents}"
            
            if chan_config.timestamps:
                ts = datetime.now(timezone.utc).astimezone(chan_config.tz)
                msg = f"`[{ts.strftime('%H:%M:%S')}]` {msg}"
                
            if len(msg) > 2000:
                msg = msg[0:1997] + '...'
            return msg

        for channel_id, chan_config in config._channels.items():
            target_channel = guild.get_channel(channel_id)
            if not target_channel:
                config._channels = {}
                config.resolved = False
                return
                
            if not {action} & chan_config.subscribed:
                continue
                
            msg = generate_simple(chan_config)
            
            if channel_id not in self.pumps:
                self.pumps[channel_id] = ModLogPump(self.bot, target_channel)
                
            await self.pumps[channel_id].send(msg)

    def is_admin(self, ctx):
        core = self.bot.get_cog('CorePlugin')
        if not core: return False
        return core.get_level(ctx.guild.id, ctx.author) >= 100

    @commands.group(invoke_without_command=True)
    async def modlog(self, ctx):
        pass

    @modlog.command(name='hush')
    async def command_hush(self, ctx):
        if not self.is_admin(ctx): return await ctx.send("Invalid permissions.")
        if ctx.guild.id in self.hushed:
            return await ctx.send(':warning: modlog is already hushed')
            
        self.hushed[ctx.guild.id] = True
        await ctx.send(':white_check_mark: modlog has been hushed, do your dirty work in peace')

    @modlog.command(name='unhush')
    async def command_unhush(self, ctx):
        if not self.is_admin(ctx): return await ctx.send("Invalid permissions.")
        if ctx.guild.id not in self.hushed:
            return await ctx.send(':warning: modlog is not hushed')
            
        del self.hushed[ctx.guild.id]
        await ctx.send(':white_check_mark: modlog has been unhushed, shhhhh... nobody saw anything')

    @tasks.loop(seconds=120)
    async def cleanup_debounce(self):
        for obj in list(self.debounces):
            if obj.is_expired():
                obj.remove()

    @cleanup_debounce.before_loop
    async def before_cleanup(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel):
        await self.log_action(Actions.CHANNEL_CREATE, channel)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        await self.log_action(Actions.CHANNEL_DELETE, channel)

    @commands.Cog.listener()
    async def on_member_ban(self, guild, user):
        debounce = self.debounces.find('on_member_ban', guild_id=guild.id, user_id=user.id)
        if debounce: return
        
        # Wrapping event data to mimic standard event flow
        class MockEvent:
            def __init__(self, g, u):
                self.guild = g
                self.user = u
        await self.log_action(Actions.GUILD_BAN_ADD, MockEvent(guild, user))

    @commands.Cog.listener()
    async def on_member_unban(self, guild, user):
        debounce = self.debounces.find('on_member_unban', guild_id=guild.id, user_id=user.id)
        if debounce: return
        
        class MockEvent:
            def __init__(self, g, u):
                self.guild = g
                self.user = u
        await self.log_action(Actions.GUILD_BAN_REMOVE, MockEvent(guild, user))

    @commands.Cog.listener()
    async def on_member_join(self, member):
        created_delta = datetime.now(timezone.utc).replace(tzinfo=None) - member.created_at.replace(tzinfo=None)
        created = humanize.naturaltime(created_delta)
        
        core = self.bot.get_cog('CorePlugin')
        config = core.get_config(member.guild.id) if core else None
        threshold = config.plugins.modlog.new_member_threshold if config and hasattr(config.plugins, 'modlog') else 900
        
        new = threshold and created_delta.total_seconds() < threshold
        
        await self.log_action(Actions.GUILD_MEMBER_ADD, member, new=' :new:' if new else '', created=created)

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        debounce = self.debounces.find('on_member_remove', guild_id=member.guild.id, user_id=member.id)
        if debounce: return
        await self.log_action(Actions.GUILD_MEMBER_REMOVE, member)
    @commands.Cog.listener()
    async def on_guild_role_create(self, role):
        # We wrap in a mock event to fulfill the legacy log_action parameter expectations
        class MockEvent:
            def __init__(self, guild):
                self.guild = guild
        await self.log_action(Actions.GUILD_ROLE_CREATE, MockEvent(role.guild), role=role)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role):
        class MockEvent:
            def __init__(self, guild):
                self.guild = guild
        await self.log_action(Actions.GUILD_ROLE_DELETE, MockEvent(role.guild), pre_role=role)

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        # Global debounce check
        debounce = self.debounces.find('on_member_update', guild_id=after.guild.id, user_id=after.id)
        if debounce: return

        class MockEvent:
            def __init__(self, guild):
                self.guild = guild
        event_wrapper = MockEvent(after.guild)

        # Log nickname changes
        if before.nick != after.nick:
            if not before.nick:
                nick_debounce = self.debounces.find('on_member_update', guild_id=after.guild.id, user_id=after.id, nickname=after.nick)
                if not nick_debounce:
                    await self.log_action(Actions.ADD_NICK, event_wrapper, member=after, nickname=after.nick)
            elif not after.nick:
                await self.log_action(Actions.RMV_NICK, event_wrapper, member=after, nickname=before.nick)
            else:
                await self.log_action(Actions.CHANGE_NICK, event_wrapper, member=after, before=before.nick, after=after.nick)

        # Log role changes
        pre_roles = set(before.roles)
        post_roles = set(after.roles)
        
        if pre_roles != post_roles:
            added = post_roles - pre_roles
            removed = pre_roles - post_roles
            
            for role in added:
                role_debounce = self.debounces.find('on_member_update', guild_id=after.guild.id, user_id=after.id, role_id=role.id)
                if role_debounce: continue
                await self.log_action(Actions.GUILD_MEMBER_ROLES_ADD, event_wrapper, member=after, role=role)
                
            for role in removed:
                role_debounce = self.debounces.find('on_member_update', guild_id=after.guild.id, user_id=after.id, role_id=role.id)
                if role_debounce: continue
                await self.log_action(Actions.GUILD_MEMBER_ROLES_RMV, event_wrapper, member=after, role=role)

    @commands.Cog.listener()
    async def on_user_update(self, before, after):
        if before.name == after.name and before.discriminator == after.discriminator:
            return

        core = self.bot.get_cog('CorePlugin')
        if not core: return

        subscribed_guilds = defaultdict(list)
        
        # Iterate shared guilds
        for guild in after.mutual_guilds:
            config = core.get_config(guild.id)
            if not config or not getattr(config.plugins, 'modlog', None): continue
            
            ml_config = config.plugins.modlog
            if after.id in ml_config.ignored_users: continue
            
            if {Actions.CHANGE_USERNAME} & ml_config.subscribed:
                subscribed_guilds[Actions.CHANGE_USERNAME].append((guild, ml_config))

        if Actions.CHANGE_USERNAME in subscribed_guilds:
            for guild, ml_config in subscribed_guilds[Actions.CHANGE_USERNAME]:
                # Construct MockEvent for context
                class MockEvent:
                    def __init__(self, g): self.guild = g
                
                await self.log_action_raw(
                    Actions.CHANGE_USERNAME,
                    guild,
                    ml_config,
                    before=str(before),
                    after=str(after),
                    e=MockEvent(guild)
                )

    @commands.Cog.listener()
    async def on_message_edit(self, before, after):
        if after.author.id == self.bot.user.id or not after.guild:
            return

        core = self.bot.get_cog('CorePlugin')
        if not core: return
        config = core.get_config(after.guild.id)
        if not config or not getattr(config.plugins, 'modlog', None): return
        ml_config = config.plugins.modlog

        if after.author.id in ml_config.ignored_users or after.channel.id in ml_config.ignored_channels:
            return

        old_content = before.content
        if not old_content: # Not in cache, try DB
            def fetch_old():
                try:
                    return Message.get(Message.id == after.id).content
                except Message.DoesNotExist:
                    return None
            old_content = await asyncio.to_thread(fetch_old)
            if not old_content: return

        if old_content != after.clean_content:
            class MockEvent:
                def __init__(self, m):
                    self.guild = m.guild
                    self.author = m.author
                    self.channel_id = m.channel.id
                    
            await self.log_action(
                Actions.MESSAGE_EDIT,
                MockEvent(after),
                msg=after,
                before=filter_urls(old_content),
                after=filter_urls(after.clean_content)
            )

    @commands.Cog.listener()
    async def on_message_delete(self, message):
        if not message.guild or message.guild.id in self.hushed: return
        if message.author.id == self.bot.user.id: return
        
        debounce = self.debounces.find('on_message_delete', guild_id=message.guild.id, message_id=message.id)
        if debounce: return

        core = self.bot.get_cog('CorePlugin')
        if not core: return
        config = core.get_config(message.guild.id)
        if not config or not getattr(config.plugins, 'modlog', None): return
        ml_config = config.plugins.modlog

        if message.author.id in ml_config.ignored_users or message.channel.id in ml_config.ignored_channels:
            return

        # Use cached content if available, else DB fallback logic
        content = message.clean_content
        attachments = message.attachments

        if not content and not attachments:
            def fetch_msg():
                try: return Message.get(Message.id == message.id)
                except Message.DoesNotExist: return None
            db_msg = await asyncio.to_thread(fetch_msg)
            if not db_msg: return
            content = db_msg.content
            attachments = db_msg.attachments

        contents = filter_urls(content) if content else ''
        if len(contents) > 1750:
            contents = contents[:1750] + f'... ({len(contents) - 1750} more characters)'

        class MockEvent:
            def __init__(self, m):
                self.guild = m.guild
                
        attach_str = ''
        if attachments:
            urls = [a.url if hasattr(a, 'url') else a for a in attachments]
            attach_str = f'({", ".join(f"<{u}>" for u in urls)})'

        await self.log_action(
            Actions.MESSAGE_DELETE, 
            MockEvent(message),
            author=message.author,
            author_id=message.author.id,
            channel=message.channel,
            msg=contents,
            attachments=attach_str
        )

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload):
        guild = self.bot.get_guild(payload.guild_id)
        if not guild or guild.id in self.hushed: return
        channel = guild.get_channel(payload.channel_id)
        if not channel: return

        # Archive creation is a blocking Peewee/Requests op
        def generate_archive():
            return MessageArchive.create_from_message_ids(list(payload.message_ids))
            
        archive = await asyncio.to_thread(generate_archive)
        
        class MockEvent:
            def __init__(self, g): self.guild = g
            
        await self.log_action(
            Actions.MESSAGE_DELETE_BULK, 
            MockEvent(guild), 
            log=archive.url, 
            channel=channel, 
            count=len(payload.message_ids)
        )

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        class MockEvent:
            def __init__(self, g, m):
                self.guild = g
                self.member = m

        event = MockEvent(member.guild, member)

        if before.channel and after.channel:
            if before.channel.id != after.channel.id:
                await self.log_action(Actions.VOICE_CHANNEL_MOVE, event, before_channel=before.channel, after_channel=after.channel)
        elif before.channel and not after.channel:
            await self.log_action(Actions.VOICE_CHANNEL_LEAVE, event, channel=before.channel)
        elif not before.channel and after.channel:
            await self.log_action(Actions.VOICE_CHANNEL_JOIN, event, channel=after.channel)