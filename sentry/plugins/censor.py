import re
import json
import asyncio
from urllib import parse as urlparse
from functools import cached_property

import discord
from discord.ext import commands

from holster.enum import Enum
from sentry.redis import rdb
from sentry.util.stats import timed
from sentry.util.zalgo import ZALGO_RE
from sentry.types import SlottedModel, Field, ListField, DictField, ChannelField, snowflake, lower
from sentry.types.plugin import PluginConfig
from sentry.models.message import Message
from sentry.plugins.modlog import Actions
from sentry.constants import INVITE_LINK_RE, URL_RE

CensorReason = Enum(
    'INVITE',
    'DOMAIN',
    'WORD',
    'ZALGO',
)

class CensorSubConfig(SlottedModel):
    filter_zalgo = Field(bool, default=True)
    filter_invites = Field(bool, default=True)
    invites_guild_whitelist = ListField(snowflake, default=[])
    invites_whitelist = ListField(lower, default=[])
    invites_blacklist = ListField(lower, default=[])
    filter_domains = Field(bool, default=True)
    domains_whitelist = ListField(lower, default=[])
    domains_blacklist = ListField(lower, default=[])
    blocked_words = ListField(lower, default=[])
    blocked_tokens = ListField(lower, default=[])

    @cached_property
    def blocked_re(self):
        return re.compile('({})'.format('|'.join(
            self.blocked_words +
            [r'\b{}\b'.format(w) for w in self.blocked_tokens]
        )), re.I)

class CensorConfig(PluginConfig):
    levels = DictField(int, CensorSubConfig)

class Censorship(Exception):
    def __init__(self, reason, message, ctx):
        self.reason = reason
        self.message = message
        self.ctx = ctx

class CensorPlugin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def on_censor(self, message, censorship):
        try:
            await message.delete()
        except discord.HTTPException:
            pass

        modlog = self.bot.get_cog('ModLogPlugin')
        if modlog:
            await modlog.log_action_ext(
                Actions.CENSORED,
                message.guild.id,
                member=message.author,
                c=censorship,
                msg=message
            )

    @commands.Cog.listener()
    async def on_message(self, message):
        await self.process_message(message)

    @commands.Cog.listener()
    async def on_message_edit(self, before, after):
        await self.process_message(after)

    async def process_message(self, message):
        if message.author.bot or not message.guild:
            return

        core = self.bot.get_cog('CorePlugin')
        if not core: return

        guild_config = core.get_config(message.guild.id)
        if not guild_config or not hasattr(guild_config.plugins, 'censor'):
            return

        user_level = int(core.get_level(message.guild.id, message.author))
        censor_config = guild_config.plugins.censor

        # Find the highest applicable config level
        applicable_configs = [c for l, c in censor_config.levels.items() if user_level <= l]
        if not applicable_configs:
            return
            
        # Take the most restrictive/highest level applying to user
        config = sorted(applicable_configs, key=lambda x: list(censor_config.levels.values()).index(x))[0]

        tags = {'guild_id': message.guild.id, 'channel_id': message.channel.id}
        with timed('sentry.plugin.censor.duration', tags=tags):
            try:
                if config.filter_zalgo:
                    self.filter_zalgo(message, config)
                if config.filter_invites:
                    await self.filter_invites(message, config)
                if config.filter_domains:
                    self.filter_domains(message, config)
                if config.blocked_tokens or config.blocked_words:
                    self.filter_blocked_words(message, config)
            except Censorship as c:
                await self.on_censor(message, c)

    def filter_zalgo(self, message, config):
        zalgo = ZALGO_RE.search(message.content)
        if zalgo:
            raise Censorship(CensorReason.ZALGO, message, ctx={
                'position': zalgo.span()
            })

    async def filter_invites(self, message, config):
        invites = INVITE_LINK_RE.findall(message.content)
        for invite in invites:
            def fetch_invite():
                # Checking invites requires API hit, offload to thread or use async fetch
                try:
                    import requests
                    res = requests.get(f'https://discord.com/api/v9/invites/{invite}?with_counts=true')
                    if res.status_code == 200:
                        return res.json()
                except Exception:
                    pass
                return None

            invite_data = await asyncio.to_thread(fetch_invite)
            invite_info = invite_data.get('guild', {}) if invite_data else {}

            if invite_info and int(invite_info.get('id', 0)) == message.guild.id:
                continue

            if invite_info and int(invite_info.get('id', 0)) in config.invites_guild_whitelist:
                continue

            if (config.invites_whitelist or not config.invites_blacklist) \
                    and invite.lower() not in config.invites_whitelist:
                raise Censorship(CensorReason.INVITE, message, ctx={
                    'hit': 'whitelist',
                    'invite': invite,
                    'guild': invite_info,
                })
            elif config.invites_blacklist and invite.lower() in config.invites_blacklist:
                raise Censorship(CensorReason.INVITE, message, ctx={
                    'hit': 'blacklist',
                    'invite': invite,
                    'guild': invite_info,
                })

    def filter_domains(self, message, config):
        urls = URL_RE.findall(INVITE_LINK_RE.sub('', message.content))
        for url in urls:
            try:
                parsed = urlparse.urlparse(url)
            except Exception:
                continue

            if (config.domains_whitelist or not config.domains_blacklist) \
                    and parsed.netloc.lower() not in config.domains_whitelist:
                raise Censorship(CensorReason.DOMAIN, message, ctx={
                    'hit': 'whitelist',
                    'url': url,
                    'domain': parsed.netloc,
                })
            elif config.domains_blacklist and parsed.netloc.lower() in config.domains_blacklist:
                raise Censorship(CensorReason.DOMAIN, message, ctx={
                    'hit': 'blacklist',
                    'url': url,
                    'domain': parsed.netloc
                })

    def filter_blocked_words(self, message, config):
        blocked_words = config.blocked_re.findall(message.content)
        if blocked_words:
            raise Censorship(CensorReason.WORD, message, ctx={
                'words': blocked_words,
            })

async def setup(bot):
    await bot.add_cog(CensorPlugin(bot))