import time
import asyncio
import discord

class ModLogPump:
    def __init__(self, bot, channel, sleep_duration=5):
        self.bot = bot
        self.channel = channel
        self.sleep_duration = sleep_duration
        self._buffer = []
        self._have = asyncio.Event()
        self._lock = asyncio.Lock()
        self._quiescent_period = None
        self._task = self.bot.loop.create_task(self._emitter_loop())

    def __del__(self):
        if hasattr(self, '_task') and not self._task.done():
            self._task.cancel()

    async def _emitter_loop(self):
        while True:
            await self._have.wait()
            backoff = False

            try:
                await self._emit()
            except discord.HTTPException as e:
                # 429 Too Many Requests or 40004 Send Disabled
                if e.status == 429 or e.code == 40004:
                    backoff = True
            except Exception as e:
                print(f'Exception when executing ModLogPump._emit: {e}')

            if backoff:
                self._quiescent_period = time.time() + 60

            if self._quiescent_period:
                if self._quiescent_period < time.time():
                    self._quiescent_period = None
                else:
                    await asyncio.sleep(self.sleep_duration)

            async with self._lock:
                if not self._buffer:
                    self._have.clear()

    async def _emit(self):
        async with self._lock:
            msg = self._get_next_message()
            if not msg:
                return
                
        # Send outside the lock to prevent holding up the buffer during network IO
        await self.channel.send(msg)

    def _get_next_message(self):
        data = ''
        while self._buffer:
            payload = self._buffer.pop(0)
            # Discord max message size is 2000
            if len(data) + (len(payload) + 1) > 2000:
                self._buffer.insert(0, payload) # Push back if it overflows
                break
            if data:
                data += '\n'
            data += payload
        return data

    async def send(self, payload):
        async with self._lock:
            self._buffer.append(payload)
            self._have.set()