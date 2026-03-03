import asyncio
import discord

from . import task, get_client
from sentry.models.message import Message

@task(max_concurrent=1, max_queue_size=10, global_lock=lambda guild_id: guild_id)
async def backfill_guild(task, guild_id):
    client = get_client()
    
    try:
        guild = await client.fetch_guild(guild_id)
        channels = await guild.fetch_channels()
    except discord.HTTPException as e:
        task.log.error(f"Failed to fetch guild {guild_id}: {e}")
        return

    for channel in channels:
        if isinstance(channel, discord.TextChannel):
            await asyncio.to_thread(backfill_channel.queue, channel.id)

@task(max_concurrent=6, max_queue_size=500, global_lock=lambda channel_id: channel_id)
async def backfill_channel(task, channel_id):
    client = get_client()
    
    try:
        channel = await client.fetch_channel(channel_id)
    except discord.NotFound:
        task.log.warning(f"Channel {channel_id} not found.")
        return
    except discord.Forbidden:
        task.log.warning(f"Missing permissions to fetch channel {channel_id}.")
        return
    except discord.HTTPException as e:
        task.log.error(f"Failed to fetch channel {channel_id}: {e}")
        return

    scanned = 0
    inserted = 0
    chunk = []
    
    try:
        async for msg in channel.history(limit=None, oldest_first=True):
            chunk.append(msg)
            
            # Flush to DB in batches of 100 to prevent memory bloat
            if len(chunk) >= 100:
                scanned += len(chunk)
                inserted += await asyncio.to_thread(
                    lambda c: len(Message.from_disco_message_many(c, safe=True)), chunk
                )
                chunk = []
                
                # Yield to the event loop momentarily
                await asyncio.sleep(0.1) 

        # Flush any remaining messages in the final chunk
        if chunk:
            scanned += len(chunk)
            inserted += await asyncio.to_thread(
                lambda c: len(Message.from_disco_message_many(c, safe=True)), chunk
            )
            
    except discord.Forbidden:
        task.log.warning(f"Missing permissions to read history for channel {channel_id}.")
    except discord.HTTPException as e:
        task.log.error(f"HTTPException while reading history for {channel_id}: {e}")

    task.log.info(f'Completed backfill on channel {channel_id}, {scanned} scanned and {inserted} inserted')