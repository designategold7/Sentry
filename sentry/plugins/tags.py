import asyncio

import discord
from discord.ext import commands
from sentry.types import Field
from sentry.types.plugin import PluginConfig
from sentry.models.tags import Tag
from sentry.models.user import User

class TagsConfig(PluginConfig):
    max_tag_length = Field(int)
    # Sentry command levels: 50 is MOD, 10 is TRUSTED
    min_level_remove_others = Field(int, default=50)

class TagsPlugin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def get_level(self, ctx):
        core = self.bot.get_cog('CorePlugin')
        if not core: return 0
        return core.get_level(ctx.guild.id, ctx.author)

    def is_trusted(self, ctx):
        return self.get_level(ctx) >= 10

    @commands.group(invoke_without_command=True, aliases=['tag'])
    async def tags(self, ctx, name: str = None):
        if not name:
            return
        await self.on_tags_show(ctx, name)

    @tags.command(name='create', aliases=['add'])
    async def on_tags_create(self, ctx, name: str, *, content: str):
        if not self.is_trusted(ctx): return await ctx.send("Invalid permissions.")
        
        name = discord.utils.escape_markdown(name)
        content = discord.utils.escape_markdown(content)

        config = getattr(ctx, 'base_config', None)
        max_len = config.plugins.tags.max_tag_length if config and hasattr(config.plugins, 'tags') else 2000

        if len(content) > max_len:
            return await ctx.send(f':warning: tag content is too long (max {max_len} characters)')

        def db_create_tag():
            return Tag.get_or_create(
                guild_id=ctx.guild.id,
                author_id=ctx.author.id,
                name=name,
                content=content
            )

        _, created = await asyncio.to_thread(db_create_tag)

        if not created:
            return await ctx.send(':warning: a tag by that name already exists')

        await ctx.send(f':ok_hand: ok, your tag named `{name}` has been created')

    @tags.command(name='show')
    async def on_tags_show(self, ctx, name: str):
        # We don't necessarily enforce TRUSTED to simply view a tag, but preserving Sentry logic
        if not self.is_trusted(ctx): return await ctx.send("Invalid permissions.")
        
        safe_name = discord.utils.escape_markdown(name)

        def fetch_and_update_tag():
            try:
                tag = Tag.select(Tag, User).join(
                    User, on=(User.user_id == Tag.author_id)
                ).where(
                    (Tag.guild_id == ctx.guild.id) &
                    (Tag.name == safe_name)
                ).get()
                
                Tag.update(times_used=Tag.times_used + 1).where(
                    (Tag.guild_id == tag.guild_id) &
                    (Tag.name == tag.name)
                ).execute()
                
                return tag
            except Tag.DoesNotExist:
                return None

        tag = await asyncio.to_thread(fetch_and_update_tag)
        
        if not tag:
            return await ctx.send(':warning: no tag by that name exists')

        await ctx.send(f':information_source: {tag.content}')

    @tags.command(name='remove', aliases=['del', 'rm'])
    async def on_tags_remove(self, ctx, name: str):
        if not self.is_trusted(ctx): return await ctx.send("Invalid permissions.")
        
        safe_name = discord.utils.escape_markdown(name)

        def fetch_tag_for_deletion():
            try:
                return Tag.select(Tag, User).join(
                    User, on=(User.user_id == Tag.author_id)
                ).where(
                    (Tag.guild_id == ctx.guild.id) &
                    (Tag.name == safe_name)
                ).get()
            except Tag.DoesNotExist:
                return None

        tag = await asyncio.to_thread(fetch_tag_for_deletion)
        if not tag:
            return await ctx.send(':warning: no tag by that name exists')

        config = getattr(ctx, 'base_config', None)
        min_level = config.plugins.tags.min_level_remove_others if config and hasattr(config.plugins, 'tags') else 50

        if tag.author_id != ctx.author.id:
            if self.get_level(ctx) < min_level:
                return await ctx.send(':warning: you do not have the required permissions to remove other users tags')

        await asyncio.to_thread(tag.delete_instance)
        await ctx.send(f':ok_hand: ok, deleted tag `{tag.name}`')

    @tags.command(name='info')
    async def on_tags_info(self, ctx, name: str):
        if not self.is_trusted(ctx): return await ctx.send("Invalid permissions.")
        
        safe_name = discord.utils.escape_markdown(name)

        def fetch_tag():
            try:
                return Tag.select(Tag, User).join(
                    User, on=(User.user_id == Tag.author_id).alias('author')
                ).where(
                    (Tag.guild_id == ctx.guild.id) &
                    (Tag.name == safe_name)
                ).get()
            except Tag.DoesNotExist:
                return None

        tag = await asyncio.to_thread(fetch_tag)
        if not tag:
            return await ctx.send(':warning: no tag by that name exists')

        embed = discord.Embed()
        embed.title = tag.name
        embed.description = tag.content
        embed.add_field(name='Author', value=str(tag.author), inline=True)
        embed.add_field(name='Times Used', value=str(tag.times_used), inline=True)
        
        # Datetimes from Peewee are usually naive UTC, replace for d.py compatibility
        embed.timestamp = tag.created_at.replace(tzinfo=timezone.utc) if tag.created_at else discord.Embed.Empty
        
        await ctx.send(embed=embed)

async def setup(bot):
    await bot.add_cog(TagsPlugin(bot))