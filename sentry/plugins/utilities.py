import io
import random
import requests
import humanize
import operator
import asyncio
import functools
from io import BytesIO
from PIL import Image
from peewee import fn
from datetime import datetime, timedelta, timezone
from collections import defaultdict

import discord
from discord.ext import commands

from sentry.util.input import parse_duration
from sentry.types.plugin import PluginConfig
from sentry.models.guild import GuildVoiceSession
from sentry.models.user import User, Infraction
from sentry.models.message import Message, Reminder
from sentry.util.images import get_dominant_colors_user, get_dominant_colors_guild
from sentry.constants import (
    STATUS_EMOJI, SNOOZE_EMOJI, GREEN_TICK_EMOJI, GREEN_TICK_EMOJI_ID,
    EMOJI_RE, USER_MENTION_RE, YEAR_IN_SEC, CDN_URL
)

def get_status_emoji(member):
    if not member:
        return STATUS_EMOJI[discord.Status.offline], 'Offline'
        
    activity = member.activity
    if activity and activity.type == discord.ActivityType.streaming:
        return STATUS_EMOJI['Streaming'], 'Streaming'
    elif member.status == discord.Status.online:
        return STATUS_EMOJI[discord.Status.online], 'Online'
    elif member.status == discord.Status.idle:
        return STATUS_EMOJI[discord.Status.idle], 'Idle'
    elif member.status == discord.Status.dnd:
        return STATUS_EMOJI[discord.Status.dnd], 'DND'
    elif member.status in (discord.Status.offline, discord.Status.invisible):
        return STATUS_EMOJI[discord.Status.offline], 'Offline'
        
    return STATUS_EMOJI[discord.Status.offline], 'Offline'

def get_emoji_url(emoji_str):
    return CDN_URL.format('-'.join(char.encode("unicode_escape").decode("utf-8")[2:].lstrip("0") for char in emoji_str))

class UtilitiesConfig(PluginConfig):
    pass

class UtilitiesPlugin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.recalculate_event = asyncio.Event()
        self.reminder_task = self.bot.loop.create_task(self.trigger_reminders_loop())
        self.bot.loop.call_later(10, self.queue_reminders)

    async def cog_unload(self):
        self.reminder_task.cancel()

    def queue_reminders(self):
        self.recalculate_event.set()

    @commands.command(name='coin')
    async def cmd_coin(self, ctx):
        await ctx.send(random.choice(['heads', 'tails']))

    @commands.command(name='number')
    async def cmd_random_number(self, ctx, end: int = 10, start: int = 0):
        if end > 9223372036854775807:
            return await ctx.send(':warning: ending number too big!')
        if end <= start:
            return await ctx.send(':warning: ending number must be larger than starting number!')
        await ctx.send(str(random.randint(start, end)))

    @commands.command(name='cat')
    async def cmd_cat(self, ctx):
        def fetch_cat():
            for _ in range(3):
                try:
                    r = requests.get('http://random.cat/meow', timeout=5)
                    r.raise_for_status()
                    url = r.json()['file']
                    if url.endswith(('.gif', '.jpg', '.png', '.jpeg')):
                        img_req = requests.get(url, timeout=5)
                        img_req.raise_for_status()
                        return img_req.content
                except Exception:
                    continue
            return None

        content = await asyncio.to_thread(fetch_cat)
        if not content:
            return await ctx.send('404 cat not found :(')
            
        file = discord.File(BytesIO(content), filename='cat.jpg')
        await ctx.send(file=file)

    @commands.command(name='emoji')
    async def cmd_emoji(self, ctx, emoji: str):
        if not EMOJI_RE.match(emoji):
            return await ctx.send(f'Unknown emoji: `{emoji}`')
            
        fields = []
        name, eid = EMOJI_RE.findall(emoji)[0]
        fields.append(f'**ID:** {eid}')
        fields.append(f'**Name:** {discord.utils.escape_markdown(name)}')
        
        # Searching all guilds the bot is in for the emoji
        guild = discord.utils.find(lambda g: discord.utils.get(g.emojis, id=int(eid)) is not None, self.bot.guilds)
        if guild:
            fields.append(f'**Guild:** {discord.utils.escape_markdown(guild.name)} ({guild.id})')
            
        url = f'https://cdn.discordapp.com/emojis/{eid}.png'
        
        def fetch_emoji_img():
            r = requests.get(url, timeout=5)
            r.raise_for_status()
            return r.content

        try:
            img_data = await asyncio.to_thread(fetch_emoji_img)
            file = discord.File(BytesIO(img_data), filename='emoji.png')
            await ctx.send('\n'.join(fields), file=file)
        except Exception:
            await ctx.send('\n'.join(fields) + '\n\n*(Failed to download emoji image)*')

    @commands.command(name='jumbo')
    async def cmd_jumbo(self, ctx, *, emojis: str):
        urls = []
        for emoji in emojis.split(' ')[:5]:
            if EMOJI_RE.match(emoji):
                _, eid = EMOJI_RE.findall(emoji)[0]
                urls.append(f'https://cdn.discordapp.com/emojis/{eid}.png')
            else:
                urls.append(get_emoji_url(emoji))

        def process_images(url_list):
            width, height, images = 0, 0, []
            for url in url_list:
                try:
                    r = requests.get(url, timeout=5)
                    r.raise_for_status()
                    img = Image.open(BytesIO(r.content)).convert("RGBA")
                    height = img.height if img.height > height else height
                    width += img.width + 10
                    images.append(img)
                except Exception:
                    continue
                    
            if not images:
                return None
                
            combined_image = Image.new('RGBA', (width, height))
            width_offset = 0
            for img in images:
                combined_image.paste(img, (width_offset, 0))
                width_offset += img.width + 10
                
            combined_bytes = BytesIO()
            combined_image.save(combined_bytes, 'png', quality=55)
            combined_bytes.seek(0)
            return combined_bytes

        combined = await asyncio.to_thread(process_images, urls)
        if not combined:
            return await ctx.send("Failed to parse those emojis.")
            
        file = discord.File(combined, filename='emoji.png')
        await ctx.send(file=file)

    @commands.command(name='seen')
    async def cmd_seen(self, ctx, user: discord.User):
        def fetch_last_seen():
            query = Message.select(Message.timestamp).where(Message.author_id == user.id).order_by(Message.timestamp.desc()).limit(1)
            try:
                return query.get().timestamp
            except Message.DoesNotExist:
                return None

        timestamp = await asyncio.to_thread(fetch_last_seen)
        if not timestamp:
            return await ctx.send(f"I've never seen {user}")
            
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        await ctx.send(f'I last saw {user} {humanize.naturaldelta(now - timestamp)} ago (at {timestamp})')

    @commands.command(name='search')
    async def cmd_search(self, ctx, *, query: str):
        def do_search():
            queries = []
            if query.isdigit():
                queries.append((User.user_id == int(query)))
                
            q = USER_MENTION_RE.findall(query)
            if len(q) and q[0].isdigit():
                queries.append((User.user_id == int(q[0])))
            else:
                queries.append((User.username ** f'%{query.replace("%", "")}%'))
                
            if '#' in query:
                try:
                    username, discrim = query.rsplit('#', 1)
                    if discrim.isdigit():
                        queries.append(((User.username == username) & (User.discriminator == int(discrim))))
                except ValueError:
                    pass
                    
            return list(User.select().where(functools.reduce(operator.or_, queries)).limit(26))

        users = await asyncio.to_thread(do_search)
        
        if len(users) == 0:
            return await ctx.send(f'No users found for query `{discord.utils.escape_markdown(query)}`')
            
        if len(users) == 1:
            member = ctx.guild.get_member(users[0].user_id) if ctx.guild else None
            discord_user = member or self.bot.get_user(users[0].user_id)
            if discord_user:
                return await self.cmd_info(ctx, discord_user)
                
        user_lines = [f'{str(u)} ({u.user_id})' for u in users[:25]]
        await ctx.send(f'Found the following users for your query: ```\n{"".join(user_lines)}\n```')

    @commands.command(name='server')
    async def cmd_server(self, ctx, guild_id: int = None):
        guild = self.bot.get_guild(guild_id) if guild_id else ctx.guild
        if not guild:
            return await ctx.send("Invalid server.")
            
        content = []
        content.append('**\u276F Server Information**')
        created_at = guild.created_at.replace(tzinfo=None)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        
        content.append(f'Created: {humanize.naturaldelta(now - created_at)} ago ({created_at.isoformat()})')
        content.append(f'Members: {guild.member_count}')
        content.append(f'Features: {", ".join(guild.features) or "none"}')
        
        content.append('\n**\u276F Counts**')
        text_count = len(guild.text_channels)
        voice_count = len(guild.voice_channels)
        content.append(f'Roles: {len(guild.roles)}')
        content.append(f'Text: {text_count}')
        content.append(f'Voice: {voice_count}')
        
        content.append('\n**\u276F Members**')
        status_counts = defaultdict(int)
        for member in guild.members:
            status_counts[member.status] += 1
            
        for status, count in sorted(status_counts.items(), key=lambda i: str(i[0]), reverse=True):
            status_emoji = STATUS_EMOJI.get(status, STATUS_EMOJI.get('offline'))
            content.append(f'<{status_emoji}> - {count}')
            
        embed = discord.Embed(description='\n'.join(content))
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
            
        # Color processing mapped to thread
        if guild.icon:
            try:
                color = await asyncio.to_thread(get_dominant_colors_guild, guild)
                embed.color = color
            except Exception:
                pass
                
        await ctx.send(embed=embed)

    @commands.command(name='info')
    async def cmd_info(self, ctx, user: discord.User):
        content = []
        content.append('**\u276F User Information**')
        content.append(f'ID: {user.id}')
        content.append(f'Profile: <@{user.id}>')
        
        member = ctx.guild.get_member(user.id) if ctx.guild else None
        
        emoji, status = get_status_emoji(member)
        content.append(f'Status: {status} <{emoji}>')
        
        if member and member.activity and member.activity.name:
            if member.activity.type == discord.ActivityType.streaming:
                content.append(f'Stream: [{member.activity.name}]({member.activity.url})')
            else:
                content.append(f'Game: {member.activity.name}')
                
        created_dt = user.created_at.replace(tzinfo=None)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        content.append(f'Created: {humanize.naturaldelta(now - created_dt)} ago ({created_dt.isoformat()})')
        
        if member:
            content.append('\n**\u276F Member Information**')
            if member.nick:
                content.append(f'Nickname: {member.nick}')
            if member.joined_at:
                joined_dt = member.joined_at.replace(tzinfo=None)
                content.append(f'Joined: {humanize.naturaldelta(now - joined_dt)} ago ({joined_dt.isoformat()})')
            if member.roles[1:]: # Skip @everyone
                content.append(f'Roles: {", ".join([r.name for r in member.roles[1:]])}')

        def run_db_queries():
            # Run all queries synchronously in the thread executor
            newest_msg = Message.select(Message.timestamp).where((Message.author_id == user.id) & (Message.guild_id == ctx.guild.id)).limit(1).order_by(Message.timestamp.desc()).first()
            oldest_msg = Message.select(Message.timestamp).where((Message.author_id == user.id) & (Message.guild_id == ctx.guild.id)).limit(1).order_by(Message.timestamp.asc()).first()
            
            infractions = list(Infraction.select(Infraction.guild_id, fn.COUNT('*')).where((Infraction.user_id == user.id)).group_by(Infraction.guild_id).tuples())
            voice = list(GuildVoiceSession.select(GuildVoiceSession.user_id, fn.COUNT('*'), fn.SUM(GuildVoiceSession.ended_at - GuildVoiceSession.started_at)).where((GuildVoiceSession.user_id == user.id) & (~(GuildVoiceSession.ended_at >> None))).group_by(GuildVoiceSession.user_id).tuples())
            
            return newest_msg, oldest_msg, infractions, voice

        newest_msg, oldest_msg, infractions, voice = await asyncio.to_thread(run_db_queries)

        if newest_msg and oldest_msg:
            content.append('\n **\u276F Activity**')
            content.append(f'Last Message: {humanize.naturaldelta(now - newest_msg.timestamp)} ago ({newest_msg.timestamp.isoformat()})')
            content.append(f'First Message: {humanize.naturaldelta(now - oldest_msg.timestamp)} ago ({oldest_msg.timestamp.isoformat()})')
            
        if infractions:
            total = sum(i[1] for i in infractions)
            content.append('\n**\u276F Infractions**')
            content.append(f'Total Infractions: {total}')
            content.append(f'Unique Servers: {len(infractions)}')
            
        if voice:
            content.append('\n**\u276F Voice**')
            content.append(f'Sessions: {voice[0][1]}')
            content.append(f'Time: {humanize.naturaldelta(voice[0][2])}')
            
        embed = discord.Embed(description='\n'.join(content))
        
        avatar_url = user.display_avatar.url
        embed.set_author(name=str(user), icon_url=avatar_url)
        embed.set_thumbnail(url=avatar_url)
        
        try:
            color = await asyncio.to_thread(get_dominant_colors_user, user, avatar_url)
            embed.color = color
        except Exception:
            pass

        await ctx.send(embed=embed)
    async def trigger_reminders_loop(self):
        await self.bot.wait_until_ready()
        
        while not self.bot.is_closed():
            self.recalculate_event.clear()
            
            def get_next_reminder():
                try:
                    return Reminder.select().order_by(Reminder.remind_at.asc()).limit(1).get()
                except Reminder.DoesNotExist:
                    return None
                    
            next_reminder = await asyncio.to_thread(get_next_reminder)
            
            if not next_reminder:
                # No reminders, sleep until woken up by a new addition
                await self.recalculate_event.wait()
                continue
                
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            
            if next_reminder.remind_at > now:
                sleep_seconds = (next_reminder.remind_at - now).total_seconds()
                try:
                    # Sleep until the next reminder, or until interrupted by a new one
                    await asyncio.wait_for(self.recalculate_event.wait(), timeout=sleep_seconds)
                    continue # Interrupted! Recalculate.
                except asyncio.TimeoutError:
                    pass # Timeout reached naturally, time to execute!

            # Time to process due reminders
            await self.trigger_reminders()

    async def trigger_reminders(self):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        
        def fetch_due_reminders():
            return list(Reminder.with_message_join().where((Reminder.remind_at < (now + timedelta(seconds=1)))))
            
        reminders = await asyncio.to_thread(fetch_due_reminders)
        
        # Fire off all due reminders concurrently
        tasks = [self.bot.loop.create_task(self.trigger_reminder(r)) for r in reminders]
        if tasks:
            await asyncio.gather(*tasks)
            
        # Re-queue to catch the next batch
        self.queue_reminders()

    async def trigger_reminder(self, reminder):
        message = reminder.message_id
        channel = self.bot.get_channel(message.channel_id)
        if not channel:
            await asyncio.to_thread(reminder.delete_instance)
            return
            
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        time_ago = humanize.naturaldelta(reminder.created_at - now)
        
        try:
            msg = await channel.send(f'<@{message.author_id}> you asked me at {reminder.created_at} ({time_ago} ago) to remind you about: {discord.utils.escape_markdown(reminder.content)}')
            await msg.add_reaction(SNOOZE_EMOJI)
            await msg.add_reaction(GREEN_TICK_EMOJI)
        except discord.HTTPException:
            await asyncio.to_thread(reminder.delete_instance)
            return

        def check(reaction, user):
            return user.id == message.author_id and reaction.message.id == msg.id and \
                   (str(reaction.emoji) == SNOOZE_EMOJI or (getattr(reaction.emoji, 'id', None) == GREEN_TICK_EMOJI_ID))

        try:
            reaction, user = await self.bot.wait_for('reaction_add', timeout=30.0, check=check)
        except asyncio.TimeoutError:
            await asyncio.to_thread(reminder.delete_instance)
            return
        finally:
            try:
                await msg.clear_reactions()
            except discord.HTTPException:
                pass

        if str(reaction.emoji) == SNOOZE_EMOJI:
            def snooze_db():
                reminder.remind_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=20)
                reminder.save()
            await asyncio.to_thread(snooze_db)
            await msg.edit(content='Ok, I\'ve snoozed that reminder for 20 minutes.')
            self.queue_reminders()
            return
            
        await asyncio.to_thread(reminder.delete_instance)

    @commands.group(invoke_without_command=True, aliases=['r'])
    async def remind(self, ctx):
        pass

    @remind.command(name='clear')
    async def cmd_remind_clear(self, ctx):
        count = await asyncio.to_thread(Reminder.delete_for_user, ctx.author.id)
        return await ctx.send(f':ok_hand: I cleared {count} reminders for you')

    @remind.command(name='add')
    async def cmd_remind_add(self, ctx, duration: str, *, content: str):
        # Fallback trigger logic to align with standard commands
        await self._add_reminder(ctx, duration, content)

    @commands.command(name='remind_standalone', aliases=['remind'])
    async def cmd_remind_standalone(self, ctx, duration: str, *, content: str):
        # Legacy route for !remind <duration> <content> instead of !r add
        await self._add_reminder(ctx, duration, content)

    async def _add_reminder(self, ctx, duration: str, content: str):
        count = await asyncio.to_thread(Reminder.count_for_user, ctx.author.id)
        if count > 30:
            return await ctx.send(':warning: you can only have 15 reminders going at once!')
            
        remind_at = parse_duration(duration)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        
        if remind_at > (now + timedelta(seconds=5 * YEAR_IN_SEC)):
            return await ctx.send(':warning: thats too far in the future, I\'ll forget!')
            
        def create_db_reminder():
            return Reminder.create(message_id=ctx.message.id, remind_at=remind_at, content=content)
            
        r = await asyncio.to_thread(create_db_reminder)
        
        # Trigger the event to interrupt the loop and recalculate sleep
        self.queue_reminders()
        
        await ctx.send(f':ok_hand: I\'ll remind you at {r.remind_at.isoformat()} ({humanize.naturaldelta(r.remind_at - now)})')

async def setup(bot):
    await bot.add_cog(UtilitiesPlugin(bot))