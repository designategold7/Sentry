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
            self.role_debounces[after.id] = time.time() + 60
            await after.edit(**to_update)

    def is_mod(self, ctx):
        core = self.bot.get_cog('CorePlugin')
        if not core: return False
        return core.get_level(ctx.guild.id, ctx.author) >= 50

    @commands.command()
    async def roles(self, ctx):
        if not self.is_mod(ctx): return await ctx.send("Invalid permissions.")
        buff = ''
        for role in ctx.guild.roles:
            role_text = discord.utils.escape_markdown(f'{role.id} - {role.name}\n')
            if len(role_text) + len(buff) > 1990:
                await ctx.send(f'```\n{buff}```')
                buff = ''
            buff += role_text
        if buff: await ctx.send(f'```\n{buff}```')

    @commands.group(invoke_without_command=True)
    async def backups(self, ctx): pass

    @backups.command(name='restore')
    async def backups_restore(self, ctx, user: discord.Member):
        if not self.is_mod(ctx): return await ctx.send("Invalid permissions.")
        await self.restore_user(ctx, user)
        await ctx.send(f":ok_hand: Attempted restoration for {user.name}")

    @backups.command(name='clear')
    async def backups_clear(self, ctx, user_id: int):
        if not self.is_mod(ctx): return await ctx.send("Invalid permissions.")
        def delete_backup():
            return bool(GuildMemberBackup.delete().where(
                (GuildMemberBackup.user_id == user_id) &
                (GuildMemberBackup.guild_id == ctx.guild.id)
            ).execute())
        deleted = await asyncio.to_thread(delete_backup)
        if deleted: await ctx.send(':ok_hand: I\'ve cleared the member backup for that user')
        else: await ctx.send('I couldn\'t find any member backups for that user')

    @commands.group(invoke_without_command=True)
    async def archive(self, ctx): pass

    @archive.command(name='here')
    async def archive_here(self, ctx, size: int = 50):
        await self._run_archive(ctx, size=size, mode='all', channel=ctx.channel)

    @archive.command(name='user')
    async def archive_user(self, ctx, user: discord.User, size: int = 50):
        await self._run_archive(ctx, size=size, mode='user', user=user)

    async def _run_archive(self, ctx, size=50, mode=None, user=None, channel=None):
        if not self.is_mod(ctx): return await ctx.send("Invalid permissions.")
        if 0 > size >= 15000: return await ctx.send('limit must be between 1-15000')
        def fetch_archive_ids():
            q = Message.select(Message.id).join(User).order_by(Message.id.desc()).limit(size)
            if mode in ('all', 'channel'):
                q = q.where((Message.channel_id == (channel.id if channel else ctx.channel.id)))
            else:
                user_id = user.id if user else ctx.author.id
                q = q.where((Message.author_id == user_id) & (Message.guild_id == ctx.guild.id))
            return MessageArchive.create_from_message_ids([i.id for i in q])
        archive = await asyncio.to_thread(fetch_archive_ids)
        await ctx.send('OK, archived {} messages at {}'.format(len(archive.message_ids), archive.url))

    @commands.group(invoke_without_command=True)
    async def clean(self, ctx): pass

    @clean.command(name='all')
    async def clean_all(self, ctx, size: int = 25):
        await self._run_clean(ctx, size=size, mode='all')

    async def _run_clean(self, ctx, size=25, mode='all', user=None):
        if not self.is_mod(ctx): return await ctx.send("Invalid permissions.")
        if ctx.channel.id in self.cleans: return await ctx.send('a clean is already running')
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        def fetch_messages():
            query = Message.select(Message.id).where(
                (Message.deleted >> False) & (Message.channel_id == ctx.channel.id) &
                (Message.timestamp > (now - timedelta(days=13)))
            ).join(User).order_by(Message.timestamp.desc()).limit(size)
            if mode == 'user': query = query.where((User.user_id == user.id))
            return [i[0] for i in query.tuples()]
        messages = await asyncio.to_thread(fetch_messages)
        async def run_clean():
            try:
                for i in range(0, len(messages), 100):
                    chunk = [discord.Object(id=m_id) for m_id in messages[i:i + 100]]
                    await ctx.channel.delete_messages(chunk)
                    await asyncio.sleep(1)
            finally:
                if ctx.channel.id in self.cleans: del self.cleans[ctx.channel.id]
        self.cleans[ctx.channel.id] = self.bot.loop.create_task(run_clean())

    @commands.command(name='stats')
    async def msgstats(self, ctx, user: discord.User):
        if not self.is_mod(ctx): return await ctx.send("Invalid permissions.")
        def run_queries():
            stats = list(Message.select(fn.Count('*'), fn.Sum(fn.char_length(Message.content))).where(Message.author_id == user.id).tuples())
            return stats
        stats = await asyncio.to_thread(run_queries)
        if not stats: return await ctx.send("No stats found.")
        embed = discord.Embed(description=f"Stats for {user.name}")
        embed.add_field(name='Total Messages', value=str(stats[0][0] or '0'))
        await ctx.send(embed=embed)

    @commands.command(name='emojistats')
    async def emojistats_custom(self, ctx, mode: str, sort: str):
        if not self.is_mod(ctx): return await ctx.send("Invalid permissions.")
        order = 'DESC' if sort == 'most' else 'ASC'
        def fetch_emoji_stats():
            q = CUSTOM_EMOJI_STATS_SERVER_SQL.format(order, guild=ctx.guild.id)
            return list(GuildEmoji.raw(q).tuples())
        q = await asyncio.to_thread(fetch_emoji_stats)
        tbl = MessageTable()
        tbl.set_header('Count', 'Name', 'ID')
        for eid, name, count in q: tbl.add(count, name, eid)
        await ctx.send(tbl.compile())

    @commands.command(name='unlock')
    async def unlock_role(self, ctx, role_id: int):
        if not self.is_mod(ctx): return await ctx.send("Invalid permissions.")
        self.unlocked_roles[role_id] = time.time() + 300
        await ctx.send('role is unlocked for 5 minutes')

async def setup(bot):
    await bot.add_cog(AdminPlugin(bot))