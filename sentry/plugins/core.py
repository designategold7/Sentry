import os
import json
import gevent
import pprint
import signal
import inspect
import humanize
import functools
import contextlib
from datetime import datetime, timedelta
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
from sentry.models.guild import Guild, GuildBan
from sentry.models.message import Command
from sentry.models.notification import Notification
from sentry.plugins.modlog import Actions
from sentry.constants import (
    GREEN_TICK_EMOJI, RED_TICK_EMOJI, SENTRY_GUILD_ID, SENTRY_USER_ROLE_ID,
    SENTRY_CONTROL_CHANNEL
)
PY_CODE_BLOCK = '```py\n{}\n```'
BOT_INFO = '''Sentry is a moderation and utilitarian bot built for large Discord servers.'''
GUILDS_WAITING_SETUP_KEY = 'gws'
class CorePlugin(Plugin):
    def load(self, ctx):
        init_db(ENV)
        self.startup = ctx.get('startup', datetime.utcnow())
        self.guilds = ctx.get('guilds', {})
        self.emitter = Emitter(gevent.spawn)
        super(CorePlugin, self).load(ctx)
        self.bot.add_plugin = self.our_add_plugin
        if ENV != 'prod':
            self.spawn(self.wait_for_plugin_changes)
        self._wait_for_actions_greenlet = self.spawn(self.wait_for_actions)
    def spawn_wait_for_actions(self, *args, **kwargs):
        self._wait_for_actions_greenlet = self.spawn(self.wait_for_actions)
        self._wait_for_actions_greenlet.link_exception(self.spawn_wait_for_actions)
    def our_add_plugin(self, cls, *args, **kwargs):
        if getattr(cls, 'global_plugin', False):
            Bot.add_plugin(self.bot, cls, *args, **kwargs)