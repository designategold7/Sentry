import time
import asyncio
from datadog import initialize, statsd

import discord
from discord.ext import commands

# Sentry internal imports
from sentry import ENV

def to_tags(obj):
    return ['{}:{}'.format(k, v) for k, v in obj.items()]

class StatsPlugin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        
        if ENV == 'docker':
            initialize(statsd_host='statsd', statsd_port=8125)
        else:
            initialize(statsd_host='localhost', statsd_port=8125)
            
        self.nonce = 0
        self.nonces = {}
        
        # Monkeypatch the discord.py HTTP client to inject nonces for latency tracking
        self.unhooked_send_message = self.bot.http.send_message
        self.bot.http.send_message = self.send_message_hook

    async def cog_unload(self):
        # Restore the original HTTP client method when the cog is unloaded
        self.bot.http.send_message = self.unhooked_send_message

    async def send_message_hook(self, channel_id, content, *args, **kwargs):
        self.nonce += 1
        
        # discord.py's send_message accepts a nonce kwarg
        kwargs['nonce'] = self.nonce
        self.nonces[self.nonce] = time.time()
        
        return await self.unhooked_send_message(channel_id, content, *args, **kwargs)

    @commands.Cog.listener()
    async def on_socket_response(self, msg):
        # msg is the raw dictionary from the Discord websocket
        event_name = msg.get('t')
        if not event_name:
            return
            
        metadata = {
            'event': event_name,
        }
        
        data = msg.get('d')
        if isinstance(data, dict):
            guild_id = data.get('guild_id')
            if guild_id:
                metadata['guild_id'] = guild_id
                
        # statsd uses UDP, making it safe to fire synchronously without blocking the event loop
        statsd.increment('gateway.events.received', tags=to_tags(metadata))

    @commands.Cog.listener()
    async def on_message(self, message):
        tags = {
            'channel_id': message.channel.id,
            'author_id': message.author.id,
        }
        if message.guild:
            tags['guild_id'] = message.guild.id
            
        if message.author.id == self.bot.user.id:
            # message.nonce can occasionally be cast as a string by Discord's API
            nonce = int(message.nonce) if message.nonce is not None and str(message.nonce).isdigit() else None
            
            if nonce in self.nonces:
                statsd.timing(
                    'latency.message_send',
                    time.time() - self.nonces[nonce],
                    tags=to_tags(tags)
                )
                del self.nonces[nonce]
                
        statsd.increment('guild.messages.create', tags=to_tags(tags))

    @commands.Cog.listener()
    async def on_message_edit(self, before, after):
        tags = {
            'channel_id': after.channel.id,
            'author_id': after.author.id,
        }
        if after.guild:
            tags['guild_id'] = after.guild.id
            
        statsd.increment('guild.messages.update', tags=to_tags(tags))

    @commands.Cog.listener()
    async def on_message_delete(self, message):
        tags = {
            'channel_id': message.channel.id,
        }
        statsd.increment('guild.messages.delete', tags=to_tags(tags))

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        tags = {
            'channel_id': payload.channel_id,
            'user_id': payload.user_id,
            'emoji_id': payload.emoji.id or '',
            'emoji_name': payload.emoji.name or '',
        }
        statsd.increment('guild.messages.reactions.add', tags=to_tags(tags))

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload):
        tags = {
            'channel_id': payload.channel_id,
            'user_id': payload.user_id,
            'emoji_id': payload.emoji.id or '',
            'emoji_name': payload.emoji.name or '',
        }
        statsd.increment('guild.messages.reactions.remove', tags=to_tags(tags))

# Entry point for loading the cog via discord.py's extension system
async def setup(bot):
    await bot.add_cog(StatsPlugin(bot))