import asyncio
import peewee
from peewee import fn, JOIN
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands

from sentry.types.plugin import PluginConfig
from sentry.types import ChannelField, Field, SlottedModel, ListField, DictField
from sentry.models.user import StarboardBlock, User
from sentry.models.message import StarboardEntry, Message
from sentry.util.timing import Debounce
from sentry.constants import STAR_EMOJI, ERR_UNKNOWN_MESSAGE

def is_star_event(payload):
    return payload.emoji.name == STAR_EMOJI

class ChannelConfig(SlottedModel):
    # If specified, the only channels to allow stars from
    sources = ListField(ChannelField, default=[])
    # Channels to ignore
    ignored_channels = ListField(ChannelField, default=[])
    # Delete the star when the message is deleted
    clear_on_delete = Field(bool, default=True)
    # Min number of stars to post on the board
    min_stars = Field(int, default=1)
    min_stars_pin = Field(int, default=15)
    # The number which represents the "max" star level
    star_color_max = Field(int, default=15)
    # Prevent users from starring their own posts
    prevent_self_star = Field(bool, default=False)
    
    def get_color(self, count):
        ratio = min(count / float(self.star_color_max), 1.0)
        return (
            (255 << 16) +
            (int((194 * ratio) + (253 * (1 - ratio))) << 8) +
            int((12 * ratio) + (247 * (1 - ratio)))
        )

class StarboardConfig(PluginConfig):
    channels = DictField(ChannelField, ChannelConfig)
    
    def get_board(self, channel_id):
        # Starboards can't work recursively
        if channel_id in self.channels:
            return (None, None)
        for starboard, config in self.channels.items():
            if channel_id in config.ignored_channels:
                continue
            if config.sources and channel_id not in config.sources:
                continue
            return (starboard, config)
        return (None, None)


class StarboardPlugin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.updates = {}
        self.locks = {}

    def is_mod(self, ctx):
        core = self.bot.get_cog('CorePlugin')
        if not core: return False
        return core.get_level(ctx.guild.id, ctx.author) >= 50 

    def is_admin(self, ctx):
        core = self.bot.get_cog('CorePlugin')
        if not core: return False
        return core.get_level(ctx.guild.id, ctx.author) >= 100 

    def is_trusted(self, ctx):
        core = self.bot.get_cog('CorePlugin')
        if not core: return False
        return core.get_level(ctx.guild.id, ctx.author) >= 10

    @commands.group(invoke_without_command=True)
    async def stars(self, ctx):
        pass

    @stars.command(name='show')
    async def stars_show(self, ctx, mid: int):
        if not self.is_trusted(ctx): return await ctx.send("Invalid permissions.")
        
        def fetch_star():
            try:
                return StarboardEntry.select().join(Message).where(
                    (Message.guild_id == ctx.guild.id) &
                    (~(StarboardEntry.star_message_id >> None)) &
                    (
                        (Message.id == mid) |
                        (StarboardEntry.star_message_id == mid)
                    )
                ).get()
            except StarboardEntry.DoesNotExist:
                return None

        star = await asyncio.to_thread(fetch_star)
        if not star:
            return await ctx.send(':warning: no starboard message with that id')

        _, sb_config = ctx.base_config.plugins.starboard.get_board(star.message.channel_id)
        
        try:
            channel = self.bot.get_channel(star.message.channel_id)
            if not channel:
                channel = await self.bot.fetch_channel(star.message.channel_id)
            source_msg = await channel.fetch_message(star.message_id)
        except discord.HTTPException:
            return await ctx.send(':warning: no starboard message with that id')

        content, embed = self.get_embed(star, source_msg, sb_config)
        await ctx.send(content=content, embed=embed)

    @stars.command(name='stats')
    async def stars_stats(self, ctx, user: discord.User = None):
        if not self.is_mod(ctx): return await ctx.send("Invalid permissions.")
        
        if user:
            def fetch_user_stats():
                given_stars = list(StarboardEntry.select(
                    fn.COUNT('*'),
                ).join(Message).where(
                    (~ (StarboardEntry.star_message_id >> None)) &
                    (StarboardEntry.stars.contains(user.id)) &
                    (Message.guild_id == ctx.guild.id)
                ).tuples())[0][0]

                received_stars_posts, received_stars_total = list(StarboardEntry.select(
                    fn.COUNT('*'),
                    fn.SUM(fn.array_length(StarboardEntry.stars, 1)),
                ).join(Message).where(
                    (~ (StarboardEntry.star_message_id >> None)) &
                    (Message.author_id == user.id) &
                    (Message.guild_id == ctx.guild.id)
                ).tuples())[0]
                
                return given_stars, received_stars_posts, received_stars_total

            try:
                given_stars, received_stars_posts, received_stars_total = await asyncio.to_thread(fetch_user_stats)
            except Exception:
                return await ctx.send(':warning: failed to crunch the numbers on that user')

            embed = discord.Embed(color=0xffd700, title=user.name)
            if user.display_avatar:
                embed.set_thumbnail(url=user.display_avatar.url)
            embed.add_field(name='Total Stars Given', value=str(given_stars or 0), inline=True)
            embed.add_field(name='Total Posts w/ Stars', value=str(received_stars_posts or 0), inline=True)
            embed.add_field(name='Total Stars Received', value=str(received_stars_total or 0), inline=True)
            return await ctx.send(embed=embed)

        def fetch_global_stats():
            total_starred_posts, total_stars = list(StarboardEntry.select(
                fn.COUNT('*'),
                fn.SUM(fn.array_length(StarboardEntry.stars, 1)),
            ).join(Message).where(
                (~ (StarboardEntry.star_message_id >> None)) &
                (StarboardEntry.blocked == 0) &
                (Message.guild_id == ctx.guild.id)
            ).tuples())[0]

            top_users = list(StarboardEntry.select(fn.SUM(fn.array_length(StarboardEntry.stars, 1)), User.user_id).join(
                Message,
            ).join(
                User,
                on=(Message.author_id == User.user_id),
            ).where(
                (~ (StarboardEntry.star_message_id >> None)) &
                (fn.array_length(StarboardEntry.stars, 1) > 0) &
                (StarboardEntry.blocked == 0) &
                (Message.guild_id == ctx.guild.id)
            ).group_by(User).order_by(fn.SUM(fn.array_length(StarboardEntry.stars, 1)).desc()).limit(5).tuples())
            
            return total_starred_posts, total_stars, top_users

        total_starred_posts, total_stars, top_users = await asyncio.to_thread(fetch_global_stats)

        embed = discord.Embed(color=0xffd700, title='Star Stats')
        embed.add_field(name='Total Stars Given', value=str(total_stars or 0), inline=True)
        embed.add_field(name='Total Starred Posts', value=str(total_starred_posts or 0), inline=True)
        
        if top_users:
            embed.add_field(name='Top Star Receivers', value='\n'.join(
                '{}. <@{}> ({})'.format(idx + 1, row[1], row[0]) for idx, row in enumerate(top_users)
            ))
        await ctx.send(embed=embed)

    @stars.command(name='check')
    async def stars_update(self, ctx, mid: int):
        if not self.is_admin(ctx): return await ctx.send("Invalid permissions.")
        
        def fetch_entry():
            try:
                return StarboardEntry.select(StarboardEntry, Message).join(
                    Message
                ).where(
                    (Message.guild_id == ctx.guild.id) &
                    (StarboardEntry.message_id == mid)
                ).get()
            except StarboardEntry.DoesNotExist:
                return None

        entry = await asyncio.to_thread(fetch_entry)
        if not entry:
            return await ctx.send(':warning: no starboard entry exists with that message id')

        try:
            channel = self.bot.get_channel(entry.message.channel_id) or await self.bot.fetch_channel(entry.message.channel_id)
            msg = await channel.fetch_message(entry.message_id)
            
            target_reaction = discord.utils.get(msg.reactions, emoji=STAR_EMOJI)
            users = []
            if target_reaction:
                users = [u.id async for u in target_reaction.users()]

            def update_db():
                if set(users) != set(entry.stars):
                    StarboardEntry.update(
                        stars=users,
                        dirty=True
                    ).where(
                        (StarboardEntry.message_id == entry.message_id)
                    ).execute()
                else:
                    StarboardEntry.update(
                        dirty=True
                    ).where(
                        (StarboardEntry.message_id == mid)
                    ).execute()

            await asyncio.to_thread(update_db)
            self.queue_update(ctx.guild.id, ctx.base_config.plugins.starboard)
            await ctx.send('Forcing an update on message {}'.format(mid))
            
        except discord.HTTPException:
            await ctx.send(':warning: Could not fetch that message from Discord.')

    @stars.command(name='block')
    async def stars_block(self, ctx, user: discord.User):
        if not self.is_mod(ctx): return await ctx.send("Invalid permissions.")
        
        def block_user():
            _, created = StarboardBlock.get_or_create(
                guild_id=ctx.guild.id,
                user_id=user.id,
                defaults={'actor_id': ctx.author.id}
            )
            if created:
                StarboardEntry.block_user(user.id)
            return created

        created = await asyncio.to_thread(block_user)
        
        if not created:
            return await ctx.send('{} is already blocked from the starboard'.format(user))

        self.queue_update(ctx.guild.id, ctx.base_config.plugins.starboard)
        await ctx.send('Blocked {} from the starboard'.format(user))

    @stars.command(name='unblock')
    async def stars_unblock(self, ctx, user: discord.User):
        if not self.is_mod(ctx): return await ctx.send("Invalid permissions.")
        
        def unblock_user():
            count = StarboardBlock.delete().where(
                (StarboardBlock.guild_id == ctx.guild.id) &
                (StarboardBlock.user_id == user.id)
            ).execute()
            if count:
                StarboardEntry.unblock_user(user.id)
            return count

        count = await asyncio.to_thread(unblock_user)
        
        if not count:
            return await ctx.send('{} was not blocked from the starboard'.format(user))

        self.queue_update(ctx.guild.id, ctx.base_config.plugins.starboard)
        await ctx.send('Unblocked {} from the starboard'.format(user))
    @stars.command(name='unhide')
    async def stars_unhide(self, ctx, mid: int):
        if not self.is_mod(ctx): return await ctx.send("Invalid permissions.")
        
        def unhide_star():
            count = StarboardEntry.update(
                blocked=False,
                dirty=True,
            ).where(
                (StarboardEntry.message_id == mid) &
                (StarboardEntry.blocked == 1)
            ).execute()
            return count

        count = await asyncio.to_thread(unhide_star)
        if not count:
            return await ctx.send('No hidden starboard message with that ID')

        self.queue_update(ctx.guild.id, ctx.base_config.plugins.starboard)
        await ctx.send('Message {} has been unhidden from the starboard'.format(mid))

    @stars.command(name='hide')
    async def stars_hide(self, ctx, mid: int):
        if not self.is_mod(ctx): return await ctx.send("Invalid permissions.")
        
        def hide_star():
            count = StarboardEntry.update(
                blocked=True,
                dirty=True,
            ).where(
                (StarboardEntry.message_id == mid)
            ).execute()
            return count

        count = await asyncio.to_thread(hide_star)
        if not count:
            return await ctx.send('No starred message with that ID')

        self.queue_update(ctx.guild.id, ctx.base_config.plugins.starboard)
        await ctx.send('Message {} has been hidden from the starboard'.format(mid))

    @stars.command(name='update')
    async def force_update_stars(self, ctx):
        if not self.is_admin(ctx): return await ctx.send("Invalid permissions.")
        
        def fetch_dirty_stars():
            return list(StarboardEntry.select(StarboardEntry, Message).join(
                Message
            ).where(
                (Message.guild_id == ctx.guild.id) &
                (~ (StarboardEntry.star_message_id >> None))
            ).order_by(Message.timestamp.desc()).limit(100))

        stars = await asyncio.to_thread(fetch_dirty_stars)
        info_msg = await ctx.send('Updating starboard...')

        for star in stars:
            try:
                channel = self.bot.get_channel(star.message.channel_id) or await self.bot.fetch_channel(star.message.channel_id)
                msg = await channel.fetch_message(star.message_id)
                
                target_reaction = discord.utils.get(msg.reactions, emoji=STAR_EMOJI)
                users = [u.id async for u in target_reaction.users()] if target_reaction else []

                if set(users) != set(star.stars):
                    await asyncio.to_thread(
                        StarboardEntry.update(stars=users, dirty=True).where((StarboardEntry.message_id == star.message_id)).execute
                    )
            except discord.HTTPException:
                pass 

        self.queue_update(ctx.guild.id, ctx.base_config.plugins.starboard)
        await info_msg.delete()
        await ctx.send(':ballot_box_with_check: Starboard Updated!')

    @stars.command(name='lock')
    async def lock_stars(self, ctx):
        if not self.is_admin(ctx): return await ctx.send("Invalid permissions.")
        if ctx.guild.id in self.locks:
            return await ctx.send(':warning: starboard is already locked')
            
        self.locks[ctx.guild.id] = True
        await ctx.send(':white_check_mark: starboard has been locked')

    @stars.command(name='unlock')
    async def unlock_stars(self, ctx, block: bool = False):
        if not self.is_admin(ctx): return await ctx.send("Invalid permissions.")
        if ctx.guild.id not in self.locks:
            return await ctx.send(':warning: starboard is not locked')

        if block:
            def block_updates():
                StarboardEntry.update(dirty=False, blocked=True).join(Message).where(
                    (StarboardEntry.dirty == 1) &
                    (Message.guild_id == ctx.guild.id) &
                    (Message.timestamp > (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=32)))
                ).execute()
            await asyncio.to_thread(block_updates)

        del self.locks[ctx.guild.id]
        await ctx.send(':white_check_mark: starboard has been unlocked')

    def queue_update(self, guild_id, config):
        if guild_id in self.locks:
            return
            
        # Cancel the existing task to act as a debounce
        if guild_id in self.updates and not self.updates[guild_id].done():
            self.updates[guild_id].cancel()
            
        async def debounced_update():
            try:
                await asyncio.sleep(2) 
                await self.update_starboard(guild_id, config)
            except asyncio.CancelledError:
                pass
                
        self.updates[guild_id] = self.bot.loop.create_task(debounced_update())

    async def update_starboard(self, guild_id, config):
        def fetch_stars_to_update():
            return list(StarboardEntry.select().join(Message).where(
                (StarboardEntry.dirty == 1) &
                (Message.guild_id == guild_id) &
                (Message.timestamp > (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=32)))
            ))
            
        stars = await asyncio.to_thread(fetch_stars_to_update)

        for star in stars:
            sb_id, sb_config = config.get_board(star.message.channel_id)
            
            if not sb_id:
                await asyncio.to_thread(StarboardEntry.update(dirty=False).where(StarboardEntry.message_id == star.message_id).execute)
                continue

            if not star.stars:
                if not star.star_channel_id:
                    await asyncio.to_thread(StarboardEntry.update(dirty=False).where(StarboardEntry.message_id == star.message_id).execute)
                    continue
                await self.delete_star(star)
                continue

            try:
                channel = self.bot.get_channel(star.message.channel_id) or await self.bot.fetch_channel(star.message.channel_id)
                source_msg = await channel.fetch_message(star.message_id)
            except discord.HTTPException:
                await self.delete_star(star, update=True)
                continue

            if star.star_channel_id and (
                    star.star_channel_id != sb_id or
                    len(star.stars) < sb_config.min_stars) or star.blocked:
                await self.delete_star(star, update=True)

            if len(star.stars) < sb_config.min_stars or star.blocked:
                await asyncio.to_thread(StarboardEntry.update(dirty=False).where(StarboardEntry.message_id == star.message_id).execute)
                continue

            await self.post_star(star, source_msg, sb_id, sb_config)

    async def delete_star(self, star, update=True):
        if star.star_channel_id and star.star_message_id:
            try:
                channel = self.bot.get_channel(star.star_channel_id) or await self.bot.fetch_channel(star.star_channel_id)
                msg = await channel.fetch_message(star.star_message_id)
                await msg.delete()
            except discord.HTTPException:
                pass

        if update:
            def update_db():
                StarboardEntry.update(
                    dirty=False,
                    star_channel_id=None,
                    star_message_id=None,
                ).where(
                    (StarboardEntry.message_id == star.message_id)
                ).execute()
            await asyncio.to_thread(update_db)
            star.star_channel_id = None
            star.star_message_id = None

    async def post_star(self, star, source_msg, starboard_id, config):
        content, embed = self.get_embed(star, source_msg, config)
        
        starboard_channel = self.bot.get_channel(starboard_id) or await self.bot.fetch_channel(starboard_id)

        if not star.star_message_id:
            try:
                msg = await starboard_channel.send(content=content, embed=embed)
            except discord.HTTPException as e:
                print(f'Failed to post starboard message: {e}')
                return
        else:
            try:
                msg = await starboard_channel.fetch_message(star.star_message_id)
                await msg.edit(content=content, embed=embed)
            except discord.NotFound:
                # Equivalent to APIException code 10008 (ERR_UNKNOWN_MESSAGE)
                star.star_message_id = None
                star.star_channel_id = None
                return await self.post_star(star, source_msg, starboard_id, config)
            except discord.HTTPException as e:
                print(f'Failed to edit starboard message: {e}')
                return

        def update_db():
            StarboardEntry.update(
                dirty=False,
                star_channel_id=msg.channel.id,
                star_message_id=msg.id,
            ).where(
                (StarboardEntry.message_id == star.message_id)
            ).execute()
            
        await asyncio.to_thread(update_db)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        if payload.emoji.name != STAR_EMOJI:
            return

        def check_block_and_add():
            try:
                msg = Message.select(
                    Message,
                    StarboardBlock
                ).join(
                    StarboardBlock,
                    join_type=JOIN.LEFT_OUTER,
                    on=(
                        (
                            (Message.author_id == StarboardBlock.user_id) |
                            (StarboardBlock.user_id == payload.user_id)
                        ) &
                        (Message.guild_id == StarboardBlock.guild_id)
                    )
                ).where(
                    (Message.id == payload.message_id)
                ).get()
            except Message.DoesNotExist:
                return False, None

            if getattr(msg, 'starboardblock', None) and msg.starboardblock.user_id:
                return True, None 

            try:
                StarboardEntry.add_star(payload.message_id, payload.user_id)
                return False, msg
            except peewee.IntegrityError:
                return False, 'needs_fetch'

        blocked, result = await asyncio.to_thread(check_block_and_add)
        
        if blocked:
            await self.bot.http.remove_reaction(payload.channel_id, payload.message_id, STAR_EMOJI, payload.user_id)
            return

        if result == 'needs_fetch':
            try:
                channel = self.bot.get_channel(payload.channel_id) or await self.bot.fetch_channel(payload.channel_id)
                d_msg = await channel.fetch_message(payload.message_id)
                await asyncio.to_thread(Message.from_disco_message, d_msg) # Adapt your from_disco_message to d.py
                await asyncio.to_thread(StarboardEntry.add_star, payload.message_id, payload.user_id)
            except Exception:
                return

        if payload.guild_id:
            guild_config = self.bot.get_cog('CorePlugin').get_config(payload.guild_id)
            if guild_config and hasattr(guild_config.plugins, 'starboard'):
                sb_config = guild_config.plugins.starboard
                sb_id, board = sb_config.get_board(payload.channel_id)
                
                if board and board.prevent_self_star and result and result.author_id == payload.user_id:
                    await self.bot.http.remove_reaction(payload.channel_id, payload.message_id, STAR_EMOJI, payload.user_id)
                    return
                    
                self.queue_update(payload.guild_id, sb_config)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload):
        if payload.emoji.name != STAR_EMOJI:
            return
            
        await asyncio.to_thread(StarboardEntry.remove_star, payload.message_id, payload.user_id)
        
        if payload.guild_id:
            guild_config = self.bot.get_cog('CorePlugin').get_config(payload.guild_id)
            if guild_config and hasattr(guild_config.plugins, 'starboard'):
                self.queue_update(payload.guild_id, guild_config.plugins.starboard)

    @commands.Cog.listener()
    async def on_raw_reaction_clear(self, payload):
        def clear_stars():
            StarboardEntry.update(
                stars=[],
                blocked_stars=[],
                dirty=True
            ).where(
                (StarboardEntry.message_id == payload.message_id)
            ).execute()
        await asyncio.to_thread(clear_stars)
        
        if payload.guild_id:
            guild_config = self.bot.get_cog('CorePlugin').get_config(payload.guild_id)
            if guild_config and hasattr(guild_config.plugins, 'starboard'):
                self.queue_update(payload.guild_id, guild_config.plugins.starboard)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload):
        # We need the guild_id to fetch config, which might not be in the raw payload depending on cache
        guild_id = getattr(payload, 'guild_id', None)
        if not guild_id:
            return

        guild_config = self.bot.get_cog('CorePlugin').get_config(guild_id)
        if not guild_config or not hasattr(guild_config.plugins, 'starboard'):
            return

        sb_id, sb_config = guild_config.plugins.starboard.get_board(payload.channel_id)
        if not sb_id:
            return

        count = await asyncio.to_thread(
            StarboardEntry.update(dirty=True).where((StarboardEntry.message_id == payload.message_id)).execute
        )
        if count:
            self.queue_update(guild_id, guild_config.plugins.starboard)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload):
        guild_id = getattr(payload, 'guild_id', None)
        if not guild_id:
            return

        guild_config = self.bot.get_cog('CorePlugin').get_config(guild_id)
        if not guild_config or not hasattr(guild_config.plugins, 'starboard'):
            return

        sb_id, sb_config = guild_config.plugins.starboard.get_board(payload.channel_id)
        if not sb_id:
            return

        if sb_config.clear_on_delete:
            def fetch_and_delete():
                return list(StarboardEntry.delete().where(
                    (StarboardEntry.message_id == payload.message_id)
                ).returning(StarboardEntry).execute())
                
            stars = await asyncio.to_thread(fetch_and_delete)
            for star in stars:
                await self.delete_star(star, update=False)

    def get_embed(self, star, msg, config):
        stars = ':star:'
        if len(star.stars) > 1:
            if len(star.stars) >= config.star_color_max:
                stars = ':star2:'
            stars = stars + ' {}'.format(len(star.stars))
            
        content = '{} <#{}> ({})'.format(stars, msg.channel.id, msg.id)

        embed = discord.Embed()
        embed.description = msg.content

        if msg.attachments:
            attach = msg.attachments[0]
            if attach.url.lower().endswith(('png', 'jpeg', 'jpg', 'gif', 'webp')):
                embed.set_image(url=attach.url)
        elif msg.embeds:
            if msg.embeds[0].image:
                embed.set_image(url=msg.embeds[0].image.url)
            elif msg.embeds[0].thumbnail:
                embed.set_image(url=msg.embeds[0].thumbnail.url)

        embed.set_author(name=msg.author.display_name, icon_url=msg.author.display_avatar.url if msg.author.display_avatar else discord.Embed.Empty)
        embed.timestamp = msg.created_at
        embed.color = config.get_color(len(star.stars))
        
        return content, embed

async def setup(bot):
    await bot.add_cog(StarboardPlugin(bot))