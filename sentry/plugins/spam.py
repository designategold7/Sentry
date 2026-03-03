import re
import time
import asyncio
import operator
from functools import reduce
from datetime import datetime, timedelta, timezone
from collections import defaultdict

import discord
from discord.ext import commands

from holster.enum import Enum
from sentry.redis import rdb
from sentry.plugins.modlog import Actions
from sentry.plugins.censor import URL_RE
from sentry.util.leakybucket import LeakyBucket
from sentry.util.stats import timed
from sentry.types.plugin import PluginConfig
from sentry.types import SlottedModel, DictField, Field
from sentry.models.user import Infraction
from sentry.models.message import Message, EMOJI_RE

UPPER_RE = re.compile('[A-Z]')

PunishmentType = Enum(
    'NONE',
    'MUTE',
    'KICK',
    'TEMPBAN',
    'BAN',
    'TEMPMUTE'
)

class CheckConfig(SlottedModel):
    count = Field(int)
    interval = Field(int)
    meta = Field(dict, default=None)
    punishment = Field(PunishmentType, default=None)
    punishment_duration = Field(int, default=None)

class SubConfig(SlottedModel):
    max_messages = Field(CheckConfig, default=None)
    max_mentions = Field(CheckConfig, default=None)
    max_links = Field(CheckConfig, default=None)
    max_upper_case = Field(CheckConfig, default=None)
    max_emojis = Field(CheckConfig, default=None)
    max_newlines = Field(CheckConfig, default=None)
    max_attachments = Field(CheckConfig, default=None)
    max_duplicates = Field(CheckConfig, default=None)
    punishment = Field(PunishmentType, default=PunishmentType.NONE)
    punishment_duration = Field(int, default=300)
    clean = Field(bool, default=False)
    clean_count = Field(int, default=100)
    clean_duration = Field(int, default=900)
    _cached_max_messages_bucket = Field(str, private=True)
    _cached_max_mentions_bucket = Field(str, private=True)
    _cached_max_links_bucket = Field(str, private=True)
    _cached_max_upper_case_bucket = Field(str, private=True)
    _cached_max_emojis_bucket = Field(str, private=True)
    _cached_max_newlines_bucket = Field(str, private=True)
    _cached_max_attachments_bucket = Field(str, private=True)

    def validate(self):
        if self.clean_duration < 0 or self.clean_duration > 86400:
            raise Exception('Invalid value for `clean_duration` must be between 0 and 86400')
        if self.clean_count < 0 or self.clean_count > 1000:
            raise Exception('Invalid value for `clean_count` must be between 0 and 1000')

    def get_bucket(self, attr, guild_id):
        obj = getattr(self, attr)
        if not obj or not obj.count or not obj.interval:
            return (None, None)
        bucket = getattr(self, '_cached_{}_bucket'.format(attr), None)
        if not bucket:
            bucket = LeakyBucket(rdb, 'spam:{}:{}:{}'.format(attr, guild_id, '{}'), obj.count, obj.interval * 1000)
            setattr(self, '_cached_{}_bucket'.format(attr), bucket)
        return obj, bucket

class SpamConfig(PluginConfig):
    roles = DictField(str, SubConfig)
    levels = DictField(int, SubConfig)

    def compute_relevant_rules(self, member, level):
        if self.roles:
            if '*' in self.roles:
                yield self.roles['*']
            for role in member.roles:
                if str(role.id) in self.roles:
                    yield self.roles[str(role.id)]
                if role.name in self.roles:
                    yield self.roles[role.name]
        if self.levels:
            for lvl in self.levels.keys():
                if level <= lvl:
                    yield self.levels[lvl]

class Violation(Exception):
    def __init__(self, rule, check, message, member, label, msg, **info):
        self.rule = rule
        self.check = check
        self.message = message
        self.member = member
        self.label = label
        self.msg = msg
        self.info = info

class SpamPlugin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.guild_locks = {}

    def _get_lock(self, guild_id):
        if guild_id not in self.guild_locks:
            self.guild_locks[guild_id] = asyncio.Lock()
        return self.guild_locks[guild_id]

    async def violate(self, violation):
        key = 'lv:{e.guild.id}:{e.author.id}'.format(e=violation.message)
        
        def redis_check_and_set():
            last_violated = int(rdb.get(key) or 0)
            rdb.setex(key, int(time.time()), 60)
            return last_violated

        last_violated = await asyncio.to_thread(redis_check_and_set)
        
        if not last_violated > time.time() - 10:
            modlog = self.bot.get_cog('ModLogPlugin')
            if modlog:
                await modlog.log_action_ext(
                    Actions.SPAM_DEBUG,
                    violation.message.guild.id,
                    v=violation
                )
                
            punishment = violation.check.punishment or violation.rule.punishment
            punishment_duration = violation.check.punishment_duration or violation.rule.punishment_duration
            
            # Assuming Infraction methods are updated to async in the next migration step
            if punishment == PunishmentType.MUTE:
                await Infraction.mute(
                    self,
                    violation.message,
                    violation.member,
                    'Spam Detected')
            elif punishment == PunishmentType.TEMPMUTE:
                await Infraction.tempmute(
                    self,
                    violation.message,
                    violation.member,
                    'Spam Detected',
                    datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=punishment_duration))
            elif punishment == PunishmentType.KICK:
                await Infraction.kick(
                    self,
                    violation.message,
                    violation.member,
                    'Spam Detected')
            elif punishment == PunishmentType.TEMPBAN:
                await Infraction.tempban(
                    self,
                    violation.message,
                    violation.member,
                    'Spam Detected',
                    datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=punishment_duration))
            elif punishment == PunishmentType.BAN:
                await Infraction.ban(
                    self,
                    violation.message,
                    violation.member,
                    'Spam Detected',
                    violation.message.guild)
                    
            if punishment != PunishmentType.NONE and violation.rule.clean:
                def fetch_msgs_to_clean():
                    return list(Message.select(
                        Message.id,
                        Message.channel_id
                    ).where(
                        (Message.guild_id == violation.message.guild.id) &
                        (Message.author_id == violation.member.id) &
                        (Message.timestamp > (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=violation.rule.clean_duration)))
                    ).limit(violation.rule.clean_count).tuples())

                msgs = await asyncio.to_thread(fetch_msgs_to_clean)
                channels = defaultdict(list)
                
                for mid, chan in msgs:
                    channels[chan].append(discord.Object(id=mid))
                    
                for channel_id, messages in channels.items():
                    channel = self.bot.get_channel(channel_id)
                    if not channel:
                        continue
                    try:
                        for i in range(0, len(messages), 100):
                            await channel.delete_messages(messages[i:i+100])
                    except discord.HTTPException:
                        pass

    async def check_duplicate_messages(self, message, member, rule):
        def fetch_duplicate_data():
            q = [
                (Message.guild_id == message.guild.id),
                (Message.timestamp > (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=rule.max_duplicates.interval)))
            ]
            if not rule.max_duplicates.meta or not rule.max_duplicates.meta.get('global'):
                q.append((Message.author_id == member.id))
                
            return list(Message.select(
                Message.id,
                Message.content,
            ).where(reduce(operator.and_, q)).order_by(
                Message.timestamp.desc()
            ).limit(50).tuples())

        msgs = await asyncio.to_thread(fetch_duplicate_data)
        
        dupes = defaultdict(int)
        for mid, content in msgs:
            if content:
                dupes[content] += 1
                
        dupes_count = [v for k, v in dupes.items() if v > rule.max_duplicates.count]
        if dupes_count:
            raise Violation(
                rule,
                rule.max_duplicates,
                message,
                member,
                'MAX_DUPLICATES',
                'Too Many Duplicated Messages ({} / {})'.format(sum(dupes_count), len(dupes_count))
            )

    async def check_message_simple(self, message, member, rule):
        async def check_bucket(name, base_text, func):
            check, bucket = rule.get_bucket(name, message.guild.id)
            if not bucket:
                return
                
            val = func(message) if callable(func) else func
            
            def do_check():
                return bucket.check(message.author.id, val), bucket.count(message.author.id), bucket.size(message.author.id)

            passed, current_count, max_size = await asyncio.to_thread(do_check)
            
            if not passed:
                raise Violation(rule, check, message, member,
                    name.upper(),
                    f"{base_text} ({current_count} / {max_size}s)")

        await check_bucket('max_messages', 'Too Many Messages', 1)
        await check_bucket('max_mentions', 'Too Many Mentions', lambda m: len(m.mentions))
        await check_bucket('max_links', 'Too Many Links', lambda m: len(URL_RE.findall(m.content)))
        await check_bucket('max_upper_case', 'Too Many Capitals', lambda m: len(UPPER_RE.findall(m.content)))
        await check_bucket('max_emojis', 'Too Many Emojis', lambda m: len(EMOJI_RE.findall(m.content)))
        await check_bucket('max_newlines', 'Too Many Newlines', lambda m: m.content.count('\n') + m.content.count('\r'))
        await check_bucket('max_attachments', 'Too Many Attachments', lambda m: len(m.attachments))
        
        if rule.max_duplicates and rule.max_duplicates.interval and rule.max_duplicates.count:
            await self.check_duplicate_messages(message, member, rule)

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or message.webhook_id:
            return
            
        if not message.guild:
            return

        async with self._get_lock(message.guild.id):
            tags = {'guild_id': message.guild.id, 'channel_id': message.channel.id}
            with timed('sentry.plugin.spam.duration', tags=tags):
                try:
                    member = message.author
                    if not isinstance(member, discord.Member):
                        return
                        
                    core = self.bot.get_cog('CorePlugin')
                    if not core: return
                    
                    guild_config = core.get_config(message.guild.id)
                    if not guild_config or not hasattr(guild_config.plugins, 'spam'):
                        return
                        
                    spam_config = guild_config.plugins.spam
                    level = int(core.get_level(message.guild.id, member))
                    
                    for rule in spam_config.compute_relevant_rules(member, level):
                        await self.check_message_simple(message, member, rule)
                except Violation as v:
                    await self.violate(v)

async def setup(bot):
    await bot.add_cog(SpamPlugin(bot))