import functools
from flask import g, jsonify
from http.client import FORBIDDEN
def _authed(func):
    @functools.wraps(func)
    def deco(*args, **kwargs):
        if not getattr(g, 'user', None): return jsonify({'error': 'Authentication Required'}), FORBIDDEN
        return func(*args, **kwargs)
    return deco
def authed(func=None):
    if callable(func): return _authed(func)
    return functools.partial(_authed)