import json
import uuid
import logging
import time
import os
import asyncio
from sentry.redis import rdb

log = logging.getLogger(__name__)
TASKS = {}

_client_instance = None

def get_client():
    """
    Returns a singleton discord.py client for REST-only operations.
    This avoids opening/closing aiohttp sessions redundantly.
    """
    global _client_instance
    if _client_instance is None:
        import discord
        from sentry.config import token
        
        # Instantiate without intent dependencies as this is a background worker
        _client_instance = discord.Client(intents=discord.Intents.default())
        # Pre-assign the token for HTTP routes
        _client_instance.http.token = token
        
    return _client_instance

def task(*args, **kwargs):
    """
    Register a new background task.
    """
    def deco(f):
        t = Task(f.__name__, f, *args, **kwargs)
        if f.__name__ in TASKS:
            raise Exception(f"Conflicting task name: {f.__name__}")
        TASKS[f.__name__] = t
        return t
    return deco

class Task(object):
    def __init__(self, name, method, max_concurrent=None, buffer_time=None, max_queue_size=25, global_lock=None):
        self.name = name
        self.method = method
        self.max_concurrent = max_concurrent
        self.max_queue_size = max_queue_size
        self.buffer_time = buffer_time
        self.global_lock = global_lock
        self.log = log

    async def __call__(self, *args, **kwargs):
        return await self.method(self, *args, **kwargs)

    def queue(self, *args, **kwargs):
        # Determine current queue size safely
        queue_size = rdb.llen(f'task_queue:{self.name}') or 0
        if self.max_queue_size and queue_size > self.max_queue_size:
            raise Exception(f"Queue for task {self.name} is full!")
            
        task_id = str(uuid.uuid4())
        rdb.rpush(f'task_queue:{self.name}', json.dumps({
            'id': task_id,
            'args': args,
            'kwargs': kwargs
        }))
        return task_id

class TaskRunner(object):
    def __init__(self, name, target_task):
        self.name = name
        self.task = target_task
        self.lock = asyncio.Semaphore(target_task.max_concurrent) if target_task.max_concurrent else None

    async def process(self, job):
        log.info('[%s] Running job %s...', job['id'], self.name)
        start = time.time()
        try:
            await self.task(*job['args'], **job['kwargs'])
            if self.task.buffer_time:
                await asyncio.sleep(self.task.buffer_time)
        except Exception:
            log.exception('[%s] Failed in %ss', job['id'], time.time() - start)
            
        log.info('[%s] Completed in %ss', job['id'], time.time() - start)

    async def run(self, job):
        lock = None
        if self.task.global_lock:
            lock_name = '{}:{}'.format(
                self.task.name,
                self.task.global_lock(*job['args'], **job['kwargs'])
            )
            # Redis locks are blocking network IO
            lock = await asyncio.to_thread(rdb.lock, lock_name)
            await asyncio.to_thread(lock.acquire)

        if self.lock:
            await self.lock.acquire()

        try:
            await self.process(job)
        finally:
            if lock:
                await asyncio.to_thread(lock.release)
            if self.lock:
                self.lock.release()

class TaskWorker(object):
    def __init__(self):
        self.load()
        self.queues = [f'task_queue:{i}' for i in TASKS.keys()]
        self.runners = {k: TaskRunner(k, v) for k, v in TASKS.items()}
        self.active = True

    def load(self):
        for f in os.listdir(os.path.dirname(os.path.abspath(__file__))):
            if f.endswith('.py') and not f.startswith('__'):
                __import__('sentry.tasks.' + f.rsplit('.')[0])

    async def run(self):
        log.info('Running TaskManager on %s queues...', len(self.queues))
        
        # Establish REST state for the singleton client
        client = get_client()
        await client.login(client.http.token)
        
        while self.active:
            # Wrap the blocking Redis pop in a thread with a 1-second timeout
            # to allow the asyncio event loop to breathe and handle shutdown signals.
            result = await asyncio.to_thread(rdb.blpop, self.queues, 1)
            
            if not result:
                continue
                
            chan, job_data = result
            # Handle byte decoding from Redis
            chan_str = chan.decode('utf-8') if isinstance(chan, bytes) else chan
            job_name = chan_str.split(':', 1)[1]
            job = json.loads(job_data)

            if job_name not in TASKS:
                log.error("Cannot handle task %s", job_name)
                continue

            # Schedule the task concurrently
            asyncio.create_task(self.runners[job_name].run(job))