import io
import time
import asyncio
import psycopg2
import markovify
import pygal
import cairosvg
from datetime import datetime, timezone
import discord
from discord.ext import commands, tasks
from sentry.sql import database
from sentry.models.user import User
from sentry.models.guild import GuildEmoji, GuildVoiceSession
from sentry.models.channel import Channel
from sentry.models.message import Message, Reaction
from sentry.util.input import parse_duration
from sentry.tasks.backfill import backfill_channel, backfill_guild

#
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

class SQLPlugin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # In d.py cogs, we store state locally or attach to bot
        self.models = getattr(self.bot, 'markov_models', {})
        self.bot.markov_models = self.models
        self.backfills = {}
        self.user_updates = asyncio.LifoQueue(maxsize=4096)
        
        self.update_users_task.start()

    async def cog_unload(self):
        self.update_users_task.cancel()

    @tasks.loop(seconds=15)
    async def update_users_task(self):
        already_updated = set()
        while True:
            if len(already_updated) > 10000:
                return
            try:
                user_id, data = self.user_updates.get_nowait()
            except asyncio.QueueEmpty:
                return
                
            if user_id in already_updated:
                continue
            already_updated.add(user_id)
            
            def update_db():
                try:
                    User.update(**data).where(User.user_id == user_id).execute()
                except Exception as e:
                    print(f'Failed to update user {user_id}: {e}')
                    
            await asyncio.to_thread(update_db)

    @update_users_task.before_loop
    async def before_update_users(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        def update_db():
            # Adjust GuildVoiceSession.create_or_update for d.py VoiceState objects
            GuildVoiceSession.create_or_update(before, after, member)
        await asyncio.to_thread(update_db)

    @commands.Cog.listener()
    async def on_presence_update(self, before, after):
        updates = {}
        if before.avatar != after.avatar:
            updates['avatar'] = after.avatar.key if after.avatar else None
        if before.name != after.name:
            updates['username'] = after.name
        if before.discriminator != after.discriminator:
            updates['discriminator'] = int(after.discriminator)
            
        if not updates:
            return
            
        try:
            self.user_updates.put_nowait((after.id, updates))
        except asyncio.QueueFull:
            pass

    @commands.Cog.listener()
    async def on_message(self, message):
        if not message.guild:
            return
        await asyncio.to_thread(Message.from_disco_message, message) # Needs Sentry model adaptation for d.py message

    @commands.Cog.listener()
    async def on_message_edit(self, before, after):
        if not after.guild:
            return
        await asyncio.to_thread(Message.from_disco_message_update, after)

    @commands.Cog.listener()
    async def on_message_delete(self, message):
        def update_db():
            Message.update(deleted=True).where(Message.id == message.id).execute()
        await asyncio.to_thread(update_db)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload):
        def update_db():
            Message.update(deleted=True).where((Message.id << list(payload.message_ids))).execute()
        await asyncio.to_thread(update_db)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        await asyncio.to_thread(Reaction.from_disco_reaction, payload) # Adapt models to payload

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload):
        def update_db():
            Reaction.delete().where(
                (Reaction.message_id == payload.message_id) &
                (Reaction.user_id == payload.user_id) &
                (Reaction.emoji_id == (payload.emoji.id or None)) &
                (Reaction.emoji_name == (payload.emoji.name or None))
            ).execute()
        await asyncio.to_thread(update_db)

    @commands.Cog.listener()
    async def on_raw_reaction_clear(self, payload):
        def update_db():
            Reaction.delete().where((Reaction.message_id == payload.message_id)).execute()
        await asyncio.to_thread(update_db)

    @commands.Cog.listener()
    async def on_guild_emojis_update(self, guild, before, after):
        def update_db():
            ids = []
            for emoji in after:
                GuildEmoji.from_disco_guild_emoji(emoji, guild.id)
                ids.append(emoji.id)
                
            GuildEmoji.update(deleted=True).where(
                (GuildEmoji.guild_id == guild.id) &
                (~(GuildEmoji.emoji_id << ids))
            ).execute()
        await asyncio.to_thread(update_db)

    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        def update_db():
            for channel in guild.channels:
                Channel.from_disco_channel(channel)
            for emoji in guild.emojis:
                GuildEmoji.from_disco_guild_emoji(emoji, guild_id=guild.id)
        await asyncio.to_thread(update_db)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild):
        def update_db():
            Channel.update(deleted=True).where(
                Channel.guild_id == guild.id
            ).execute()
        await asyncio.to_thread(update_db)

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel):
        await asyncio.to_thread(Channel.from_disco_channel, channel)

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before, after):
        await asyncio.to_thread(Channel.from_disco_channel, after)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        def update_db():
            Channel.update(deleted=True).where(Channel.channel_id == channel.id).execute()
        await asyncio.to_thread(update_db)

    # Permission helper 
    def is_admin(self, ctx):
        core = self.bot.get_cog('CorePlugin')
        if not core: return False
        return core.get_level(ctx.guild.id, ctx.author) >= 100

    @commands.command()
    async def sql(self, ctx, *, codeblock: str):
        # Global bot admin/owner check
        if ctx.author.id != self.bot.owner_id and not self.is_admin(ctx):
            return await ctx.send("Invalid permissions.")
            
        # Strip codeblocks
        query = codeblock.strip('` ')
        if query.startswith('sql\n'):
            query = query[4:]

        def execute_query():
            conn = database.obj.get_conn()
            try:
                tbl = MessageTable(codeblock=False)
                with conn.cursor() as cur:
                    start = time.time()
                    # Safe formatting placeholder context
                    cur.execute(query.format(e=ctx))
                    dur = time.time() - start
                    
                    if cur.description:
                        tbl.set_header(*[desc[0] for desc in cur.description])
                        for row in cur.fetchall():
                            tbl.add(*row)
                            
                    result = tbl.compile()
                    return True, result, dur
            except psycopg2.Error as e:
                return False, e.pgerror, 0
            finally:
                conn.commit() # Ensure transactions are closed

        success, result, dur = await asyncio.to_thread(execute_query)
        
        if not success:
            return await ctx.send(f'```\n{result}\n```')

        if len(result) > 1900:
            fp = io.BytesIO(result.encode('utf-8'))
            return await ctx.send(
                f'_took {int(dur * 1000)}ms_', 
                file=discord.File(fp, 'result.txt')
            )
            
        await ctx.send(f'```\n{result}\n```\n_took {int(dur * 1000)}ms_\n')
        @commands.group(invoke_without_command=True)
    async def markov(self, ctx):
        pass

    @markov.command(name='init')
    async def markov_init(self, ctx, entity_id: int):
        if not self.is_admin(ctx): return await ctx.send("Invalid permissions.")
        
        target = self.bot.get_user(entity_id) or self.bot.get_channel(entity_id)
        if not target:
            return await ctx.send(':warning: Unknown user or channel ID.')

        msg = await ctx.send(':timer: Fetching messages and building model... this may take a while.')

        def build_model():
            if isinstance(target, discord.User):
                q = Message.select(Message.content).where(Message.author_id == target.id).limit(500000)
            else:
                q = Message.select(Message.content).where(Message.channel_id == target.id).limit(500000)
                
            text = [m.content for m in q if m.content]
            if not text:
                return None, 0
                
            model = markovify.NewlineText('\n'.join(text))
            return model, len(text)

        model, count = await asyncio.to_thread(build_model)
        
        if not model:
            return await msg.edit(content=':warning: Not enough data to build model.')

        self.models[target.id] = model
        await msg.edit(content=f':ok_hand: created markov model for {target} using {count} messages')

    @markov.command(name='one')
    async def markov_one(self, ctx, entity_id: int):
        if not self.is_admin(ctx): return await ctx.send("Invalid permissions.")
        if entity_id not in self.models:
            return await ctx.send(f':warning: no model created yet for {entity_id}')

        def make_sentence():
            return self.models[entity_id].make_sentence(max_overlap_ratio=1, max_overlap_total=500)
            
        sentence = await asyncio.to_thread(make_sentence)
        if not sentence:
            return await ctx.send(':warning: not enough data :(')
            
        target = self.bot.get_user(entity_id) or self.bot.get_channel(entity_id) or entity_id
        await ctx.send(f'{target}: {sentence}')

    @markov.command(name='many')
    async def markov_many(self, ctx, entity_id: int, count: int = 5):
        if not self.is_admin(ctx): return await ctx.send("Invalid permissions.")
        if entity_id not in self.models:
            return await ctx.send(f':warning: no model created yet for {entity_id}')

        def make_sentences():
            sentences = []
            for _ in range(count):
                s = self.models[entity_id].make_sentence(max_overlap_total=500)
                if s: sentences.append(s)
            return sentences

        sentences = await asyncio.to_thread(make_sentences)
        if not sentences:
            return await ctx.send(':warning: not enough data :(')

        target = self.bot.get_user(entity_id) or self.bot.get_channel(entity_id) or entity_id
        for sentence in sentences:
            await ctx.send(f'{target}: {sentence}')

    @markov.command(name='list')
    async def markov_list(self, ctx):
        if not self.is_admin(ctx): return await ctx.send("Invalid permissions.")
        if not self.models:
            return await ctx.send('`No models currently loaded.`')
        await ctx.send('`{}`'.format(', '.join(map(str, self.models.keys()))))

    @markov.command(name='delete')
    async def markov_delete(self, ctx, oid: int):
        if not self.is_admin(ctx): return await ctx.send("Invalid permissions.")
        if oid not in self.models:
            return await ctx.send(':warning: no model with that ID')
            
        del self.models[oid]
        await ctx.send(':ok_hand: deleted model')

    @markov.command(name='clear')
    async def markov_clear(self, ctx):
        if not self.is_admin(ctx): return await ctx.send("Invalid permissions.")
        self.models.clear()
        await ctx.send(':ok_hand: cleared models')

    @commands.group(invoke_without_command=True)
    async def backfill(self, ctx):
        pass

    @backfill.command(name='message')
    async def backfill_message(self, ctx, channel_id: int, message_id: int):
        if not self.is_admin(ctx): return await ctx.send("Invalid permissions.")
        
        channel = self.bot.get_channel(channel_id)
        if not channel:
            return await ctx.send(':warning: Unknown channel.')
            
        try:
            msg = await channel.fetch_message(message_id)
            await asyncio.to_thread(Message.from_disco_message, msg)
            await ctx.send(':ok_hand: backfilled')
        except discord.HTTPException:
            await ctx.send(':warning: Failed to fetch message.')

    @backfill.command(name='reactions')
    async def backfill_reactions(self, ctx, message_id: int):
        if not self.is_admin(ctx): return await ctx.send("Invalid permissions.")
        
        def get_msg_channel():
            try:
                return Message.get(id=message_id).channel_id
            except Message.DoesNotExist:
                return None
                
        channel_id = await asyncio.to_thread(get_msg_channel)
        if not channel_id:
            return await ctx.send(':warning: no message found in database')
            
        channel = self.bot.get_channel(channel_id)
        if not channel:
            return await ctx.send(':warning: unknown channel')
            
        try:
            msg = await channel.fetch_message(message_id)
            for reaction in msg.reactions:
                users = [u.id async for u in reaction.users()]
                await asyncio.to_thread(Reaction.from_disco_reactors, msg.id, reaction, users)
            await ctx.send(':ok_hand: backfilled reactions')
        except discord.HTTPException:
            await ctx.send(':warning: Failed to fetch message.')

    @backfill.command(name='channel')
    async def backfill_channel_cmd(self, ctx, channel: discord.TextChannel = None):
        if not self.is_admin(ctx): return await ctx.send("Invalid permissions.")
        channel = channel or ctx.channel
        
        # Assuming backfill_channel.queue uses Celery/Redis which is network IO
        await asyncio.to_thread(backfill_channel.queue, channel.id)
        await ctx.send(':ok_hand: enqueued channel to be backfilled')

    @backfill.command(name='guild')
    async def backfill_guild_cmd(self, ctx, concurrency: int = 1):
        if not self.is_admin(ctx): return await ctx.send("Invalid permissions.")
        
        await asyncio.to_thread(backfill_guild.queue, ctx.guild.id)
        await ctx.send(':ok_hand: enqueued guild to be backfilled')

    @commands.group(invoke_without_command=True)
    async def recover(self, ctx):
        pass

    @recover.command(name='global')
    async def recover_global(self, ctx, duration: str, pool: int = 4):
        await self._run_recover(ctx, duration, pool, mode='global')

    @recover.command(name='here')
    async def recover_here(self, ctx, duration: str, pool: int = 4):
        await self._run_recover(ctx, duration, pool, mode='here')

    async def _run_recover(self, ctx, duration, pool_size, mode):
        if not self.is_admin(ctx): return await ctx.send("Invalid permissions.")
        
        if mode == 'global':
            channels = list(self.bot.get_all_channels())
            channels = [c for c in channels if isinstance(c, discord.TextChannel)]
        else:
            channels = ctx.guild.text_channels
            
        start_at = parse_duration(duration, negative=True)
        total = len(channels)
        msg = await ctx.send(f'Recovery Status: 0/{total}')
        
        recovered_count = 0
        completed_channels = 0
        semaphore = asyncio.Semaphore(pool_size)

        async def recover_channel(channel):
            nonlocal recovered_count, completed_channels
            async with semaphore:
                try:
                    chunk = []
                    # Fetching from oldest to newest requires after=start_at and oldest_first=True
                    async for message in channel.history(after=start_at, oldest_first=True, limit=None):
                        chunk.append(message)
                        if len(chunk) >= 100:
                            recovered_count += await asyncio.to_thread(
                                lambda c: len(Message.from_disco_message_many(c, safe=True)), chunk
                            )
                            chunk = []
                            await asyncio.sleep(0.1) # Yield to event loop
                            
                    if chunk:
                        recovered_count += await asyncio.to_thread(
                            lambda c: len(Message.from_disco_message_many(c, safe=True)), chunk
                        )
                except discord.HTTPException:
                    pass
                finally:
                    completed_channels += 1

        async def updater():
            last = completed_channels
            while completed_channels < total:
                await asyncio.sleep(5)
                if last != completed_channels:
                    last = completed_channels
                    try:
                        await msg.edit(content=f'Recovery Status: {completed_channels}/{total}')
                    except discord.HTTPException:
                        pass

        updater_task = self.bot.loop.create_task(updater())
        
        # Run all channel recoveries with semaphore limits
        tasks = [self.bot.loop.create_task(recover_channel(c)) for c in channels]
        await asyncio.gather(*tasks)
        
        updater_task.cancel()
        await msg.edit(content=f'RECOVERY COMPLETED ({recovered_count} total messages)')

    @commands.group(invoke_without_command=True)
    async def words(self, ctx):
        pass

    @words.command(name='usage')
    async def words_usage(self, ctx, word: str, unit: str = 'days', amount: int = 7):
        if not self.is_admin(ctx): return await ctx.send("Invalid permissions.")
        
        sql_query = '''
            SELECT date, coalesce(count, 0) AS count
            FROM
                generate_series(
                    NOW() - interval %s,
                    NOW(),
                    %s
                ) AS date
            LEFT OUTER JOIN (
                SELECT date_trunc(%s, timestamp) AS dt, count(*) AS count
                FROM messages
                WHERE
                    timestamp >= (NOW() - interval %s) AND
                    timestamp < (NOW()) AND
                    guild_id=%s AND
                    (SELECT count(*) FROM regexp_matches(content, %s)) >= 1
                GROUP BY dt
            ) results
            ON (date_trunc(%s, date) = results.dt);
        '''
        
        msg = await ctx.send(':alarm_clock: One moment pls...')
        
        def run_db_and_chart():
            start = time.time()
            tuples = list(Message.raw(
                sql_query,
                f'{amount} {unit}',
                f'1 {unit}',
                unit,
                f'{amount} {unit}',
                ctx.guild.id,
                r'\s?{}\s?'.format(word),
                unit
            ).tuples())
            sql_duration = time.time() - start
            
            start = time.time()
            chart = pygal.Line()
            chart.title = f'Usage of {word} Over {amount} {unit}'
            
            if unit == 'days':
                chart.x_labels = [i[0].strftime('%a %d') for i in tuples]
            elif unit == 'minutes':
                chart.x_labels = [i[0].strftime('%X') for i in tuples]
            else:
                chart.x_labels = [i[0].strftime('%x %X') for i in tuples]
                
            chart.add(word, [i[1] for i in tuples])
            
            pngdata = cairosvg.svg2png(
                bytestring=chart.render(),
                dpi=72)
            chart_duration = time.time() - start
            
            return pngdata, sql_duration, chart_duration

        try:
            pngdata, sql_duration, chart_duration = await asyncio.to_thread(run_db_and_chart)
            
            file = discord.File(io.BytesIO(pngdata), filename='chart.png')
            await ctx.send(
                f'_SQL: {int(sql_duration * 1000)}ms_ - _Chart: {int(chart_duration * 1000)}ms_',
                file=file
            )
            await msg.delete()
        except Exception as e:
            await msg.edit(content=f':warning: Failed to generate chart: {e}')

    @words.command(name='top')
    async def words_top(self, ctx, target_id: int):
        if not self.is_admin(ctx): return await ctx.send("Invalid permissions.")
        
        # Determine target type
        if ctx.guild.get_member(target_id) or self.bot.get_user(target_id):
            q_col = 'author_id'
        elif ctx.guild.get_channel(target_id):
            q_col = 'channel_id'
        elif self.bot.get_guild(target_id):
            q_col = 'guild_id'
        else:
            return await ctx.send("Unknown target ID.")
            
        sql_query = """
            SELECT word, count(*)
            FROM (
                SELECT regexp_split_to_table(content, '\s') as word
                FROM messages
                WHERE {}=%s
                LIMIT 3000000
            ) t
            GROUP BY word
            ORDER BY 2 DESC
            LIMIT 30
        """.format(q_col)

        def fetch_top_words():
            t = MessageTable()
            t.set_header('Word', 'Count')
            for word, count in Message.raw(sql_query, target_id).tuples():
                if '```' in word:
                    continue
                t.add(word, count)
            return t.compile()

        result = await asyncio.to_thread(fetch_top_words)
        await ctx.send(result)

async def setup(bot):
    await bot.add_cog(SQLPlugin(bot))