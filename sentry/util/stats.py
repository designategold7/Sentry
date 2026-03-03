import time
from contextlib import contextmanager
from datadog import statsd
def to_tags(obj=None, **kwargs):
    if obj: kwargs.update(obj)
    return [f'{k}:{v}' for k, v in kwargs.items()]
@contextmanager
def timed(metricname, tags=None):
    start = time.time()
    try: yield
    finally:
        if tags and isinstance(tags, dict): tags = to_tags(tags)
        statsd.timing(metricname, (time.time() - start) * 1000, tags=tags)