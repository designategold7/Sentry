import asyncio
from datetime import datetime, timedelta, timezone
import discord
from discord.ext import commands, tasks
from sentry.redis import rdb
from sentry.util.redis import RedisSet
from sentry.models.event import Event
from sentry.models.user import User
from sentry.models.channel import Channel
from sentry.models.message import Command, Message

# Utility replacement for disco's MessageTable
class MessageTable:
    def __init__(self, codeblock=True):
        self.headers = []
        self.rows = []
        self.codeblock = codeblock

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
            
        res = "\n".join(lines)
        if self.codeblock:
            return "```\n" + res + "\n```"
        return res

class InternalPlugin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.events = RedisSet(rdb, 'internal:tracked-events')
        self.session_id = None
        self.lock = asyncio.Lock()
        self.cache = []
        self.flush_cache.start()
        self.prune_old_events.start()

    async def cog_unload(self):
        self.flush_cache.cancel()
        self.prune_old_events.cancel()

    def is_admin(self, ctx):
        core = self.bot.get_cog('CorePlugin')
        if not core: return False
        return core.get_level(ctx.guild.id, ctx.author) >= 100

    @commands.group(invoke_without_command=True)
    async def commands(self, ctx):
        pass

    @commands.command(name='errors')
    async def on_commands_errors(self, ctx):
        if not self.is_admin(ctx): return await ctx.send("Invalid permissions.")
        
        def fetch_errors():
            q = Command.select().join(
                Message, on=(Command.message_id == Message.id)
            ).where(
                Command.success == 0
            ).order_by(Message.timestamp.desc()).limit(10)
            return list(q)

        errors = await asyncio.to_thread(fetch_errors)

        tbl = MessageTable()
        tbl.set_header('ID', 'Command', 'Error')

        for err in errors:
            tbl.add(err.message_id, f"{err.plugin}.{err.command}", err.traceback.split('\n')[-2])

        await ctx.send(tbl.compile())

    @commands.group(invoke_without_command=True)
    async def events(self, ctx):
        pass

    @events.command(name='add')
    async def on_events_add(self, ctx, name: str):
        if not self.is_admin(ctx): return await ctx.send("Invalid permissions.")
        
        await asyncio.to_thread(self.events.add, name)
        await ctx.send(f':ok_hand: added {name} to the list of tracked events')

    @events.command(name='remove')
    async def on_events_remove(self, ctx, name: str):
        if not self.is_admin(ctx): return await ctx.send("Invalid permissions.")
        
        await asyncio.to_thread(self.events.remove, name)
        await ctx.send(f':ok_hand: removed {name} from the list of tracked events')

    @tasks.loop(seconds=300)
    async def prune_old_events(self):
        def do_prune():
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            Event.delete().where(
                (Event.timestamp > now - timedelta(hours=24))
            ).execute()
            
        await asyncio.to_thread(do_prune)

    @prune_old_events.before_loop
    async def before_prune(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_ready(self):
        self.session_id = self.bot.ws.session_id if getattr(self.bot, 'ws', None) else "UNKNOWN_SESSION"
    @commands.Cog.listener()
    async def on_socket_response(self, msg):
        # Equivalent to catching OPCode.DISPATCH
        if msg.get('op') != 0:
            return
            
        event_name = msg.get('t')
        if not event_name:
            return

        tracked_events = await asyncio.to_thread(lambda: list(self.events))

        if event_name not in tracked_events:
            return

        async with self.lock:
            self.cache.append(msg)

    @tasks.loop(seconds=1)
    async def flush_cache(self):
        async with self.lock:
            if not self.cache:
                return
                
            # Pop all items from the current cache
            items = self.cache
            self.cache = []

        def execute_flush(batch):
            events_to_insert = [
                {
                    'event': item['t'],
                    'content': item['d'],
                    'session_id': self.session_id
                } for item in batch
            ]
            Event.insert_many(events_to_insert).execute()

        await asyncio.to_thread(execute_flush, items)

    @flush_cache.before_loop
    async def before_flush_cache(self):
        await self.bot.wait_until_ready()

async def setup(bot):
    await bot.add_cog(InternalPlugin(bot))