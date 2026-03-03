import threading
import json
import os
import redis
ENV = os.getenv('ENV', 'local')
if ENV == 'docker':
    rdb = redis.Redis(db=0, host='redis')
else:
    rdb = redis.Redis(db=11)
def emit(typ, **kwargs):
    kwargs['type'] = typ
    rdb.publish('actions', json.dumps(kwargs))
class RedisSet(object):
    def __init__(self, rdb, key_name):
        self.rdb = rdb
        self.key_name = key_name
        self.update_key_name = f'redis-set:{self.key_name}'
        raw_set = rdb.smembers(key_name)
        self._set = {item.decode('utf-8') if isinstance(item, bytes) else item for item in raw_set}
        self._lock = threading.Lock()
        self._ps = self.rdb.pubsub()
        self._ps.subscribe(self.update_key_name)
        self._thread = threading.Thread(target=self._listener, daemon=True)
        self._thread.start()
    def __contains__(self, other):
        with self._lock: return other in self._set
    def __iter__(self):
        with self._lock: return iter(list(self._set))
    def add(self, key):
        with self._lock:
            if key in self._set: return
            self.rdb.sadd(self.key_name, key)
            self._set.add(key)
            self.rdb.publish(self.update_key_name, f'A{key}')
    def remove(self, key):
        with self._lock:
            if key not in self._set: return
            self.rdb.srem(self.key_name, key)
            self._set.remove(key)
            self.rdb.publish(self.update_key_name, f'R{key}')
    def _listener(self):
        for item in self._ps.listen():
            if item['type'] != 'message': continue
            data_payload = item['data'].decode('utf-8') if isinstance(item['data'], bytes) else item['data']
            op, data = data_payload[0], data_payload[1:]
            with self._lock:
                if op == 'A' and data not in self._set: self._set.add(data)
                elif op == 'R' and data in self._set: self._set.remove(data)