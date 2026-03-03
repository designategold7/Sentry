import time
import asyncio
from datetime import datetime, timezone
class Eventual(object):
    def __init__(self, function):
        self.function = function
        self._next_execution_time = None
        self._waiter_task = None
        self._mutex = asyncio.Lock()
    async def _execute(self):
        async with self._mutex:
            if self._waiter_task:
                self._waiter_task.cancel()
                self._waiter_task = None
            if asyncio.iscoroutinefunction(self.function): await self.function()
            else: self.function()
            self._next_execution_time = None
    async def _waiter(self):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        wait_duration = (self._next_execution_time - now).total_seconds()
        if wait_duration > 0: await asyncio.sleep(wait_duration)
        asyncio.create_task(self._execute())
    async def set_next_schedule(self, date):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if date < now:
            asyncio.create_task(self._execute())
            return
        if not self._next_execution_time or date < self._next_execution_time:
            async with self._mutex:
                if self._waiter_task: self._waiter_task.cancel()
                self._next_execution_time = date
                self._waiter_task = asyncio.create_task(self._waiter())
class Debounce(object):
    def __init__(self, func, default, hardlimit, **kwargs):
        self.func = func
        self.default = default
        self.hardlimit = hardlimit
        self.kwargs = kwargs
        self._start = time.time()
        self._lock = asyncio.Lock()
        self._t = asyncio.create_task(self.wait())
    def active(self):
        return self._t is not None
    async def wait(self):
        await asyncio.sleep(self.default)
        async with self._lock:
            if asyncio.iscoroutinefunction(self.func): await self.func(**self.kwargs)
            else: self.func(**self.kwargs)
            self._t = None
    async def touch(self):
        if self._t:
            async with self._lock:
                if self._t:
                    self._t.cancel()
                    self._t = None
        else:
            self._start = time.time()
        if time.time() - self._start > self.hardlimit:
            if asyncio.iscoroutinefunction(self.func): asyncio.create_task(self.func(**self.kwargs))
            else: asyncio.to_thread(self.func, **self.kwargs)
            return
        self._t = asyncio.create_task(self.wait())