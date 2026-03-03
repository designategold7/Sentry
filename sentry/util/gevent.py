import asyncio
async def wait_many(*args, **kwargs):
    results = await asyncio.gather(*args, return_exceptions=True)
    if kwargs.get('track_exceptions', True):
        from sentry import raven_client
        for result in results:
            if isinstance(result, Exception): raven_client.captureException(exc_info=result)
    return results