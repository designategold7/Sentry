import json
import emoji
import asyncio
import requests
from collections import defaultdict
from html import unescape

import discord
from discord.ext import commands, tasks

from holster.enum import Enum
from sentry.redis import rdb
from sentry.models.guild import Guild
from sentry.types.plugin import PluginConfig
from sentry.types import SlottedModel, DictField, Field, ChannelField

FormatMode = Enum(
    'PLAIN',
    'PRETTY'
)

class SubRedditConfig(SlottedModel):
    channel = Field(ChannelField)
    mode = Field(FormatMode, default=FormatMode.PRETTY)
    nsfw = Field(bool, default=False)
    text_length = Field(int, default=256)
    include_stats = Field(bool, default=False)

class RedditConfig(PluginConfig):
    subs = DictField(str, SubRedditConfig)

    def validate(self):
        if len(self.subs) > 3:
            raise Exception('Cannot have more than 3 subreddits configured')

class RedditPlugin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.check_subreddits.start()

    async def cog_unload(self):
        self.check_subreddits.cancel()

    @tasks.loop(seconds=30)
    async def check_subreddits(self):
        def fetch_subs():
            return list(Guild.select(
                Guild.guild_id,
                Guild.config['plugins']['reddit']
            ).where(
                ~(Guild.config['plugins']['reddit'] >> None)
            ).tuples())

        subs_raw = await asyncio.to_thread(fetch_subs)
        
        subs = defaultdict(list)
        for gid, config in subs_raw:
            if isinstance(config, str):
                config = json.loads(config)
            
            # Defensive check against bad config states
            if 'subs' not in config:
                continue
                
            for sub, sub_config in config['subs'].items():
                subs[sub.lower()].append((gid, SubRedditConfig(sub_config)))

        for sub, configs in subs.items():
            try:
                await self.check_subreddit(sub, configs)
            except Exception as e:
                print(f"Error checking subreddit {sub}: {e}")

    @check_subreddits.before_loop
    async def before_check_subreddits(self):
        await self.bot.wait_until_ready()

    async def check_subreddit(self, sub, configs):
        def fetch_reddit():
            r = requests.get(
                f'https://www.reddit.com/r/{sub}/new.json?limit=15',
                headers={'User-Agent': 'discord:Sentry:v0.0.1'}
            )
            r.raise_for_status()
            return list(reversed(list(map(lambda i: i['data'], r.json()['data']['children']))))

        try:
            data = await asyncio.to_thread(fetch_reddit)
        except Exception as e:
            print(f"Failed to fetch Reddit data for {sub}: {e}")
            return

        for gid, config in configs:
            guild = self.bot.get_guild(gid)
            if not guild:
                continue

            channel = guild.get_channel(config.channel)
            if not channel:
                continue

            def get_last_id():
                return float(rdb.get(f'rdt:lpid:{channel.id}:{sub}') or 0)

            last = await asyncio.to_thread(get_last_id)
            item_count, high_time = 0, last

            for item in data:
                if item['created_utc'] > last:
                    try:
                        await self.send_post(config, channel, item)
                    except Exception as e:
                        print(f'Failed to post reddit content from {item}: {e}')
                        
                    item_count += 1
                    
                    if item['created_utc'] > high_time:
                        await asyncio.to_thread(rdb.set, f'rdt:lpid:{channel.id}:{sub}', item['created_utc'])
                        high_time = item['created_utc']

    async def send_post(self, config, channel, item):
        if not config.nsfw and item['over_18']:
            return

        if config.mode == FormatMode.PLAIN:
            content = f"**{item['title']}**\n<{item['url']}>"
            await channel.send(content)
            return

        embed = discord.Embed()
        embed.title = item['title'][:256]
        embed.url = f"https://reddit.com{item['permalink']}"
        embed.color = 0xFF5700 # Reddit Orange
        
        embed.set_author(name=f"Posted by u/{item['author']}", url=f"https://reddit.com/u/{item['author']}")

        if item.get('selftext'):
            text = unescape(item['selftext'])
            if len(text) > config.text_length:
                text = text[:config.text_length] + '...'
            embed.description = text

        if config.include_stats:
            embed.add_field(name='Score', value=str(item['score']), inline=True)
            embed.add_field(name='Comments', value=str(item['num_comments']), inline=True)

        if item.get('post_hint') == 'image' and item.get('url'):
            embed.set_image(url=item['url'])
        elif item.get('thumbnail') and item['thumbnail'] not in ('self', 'default', 'nsfw', 'spoiler'):
            embed.set_thumbnail(url=item['thumbnail'])

        await channel.send(embed=embed)

async def setup(bot):
    await bot.add_cog(RedditPlugin(bot))