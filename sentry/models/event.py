from peewee import (
    BigIntegerField, CharField, DateTimeField, CompositeKey
)
from datetime import datetime, timedelta, timezone
from playhouse.postgres_ext import BinaryJSONField
from sentry.sql import BaseModel

@BaseModel.register
class Event(BaseModel):
    session = CharField()
    seq = BigIntegerField()
    timestamp = DateTimeField(default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    event = CharField()
    data = BinaryJSONField()

    class Meta:
        db_table = 'events'
        primary_key = CompositeKey('session', 'seq')
        indexes = (
            (('timestamp', ), False),
            (('event', ), False),
        )

    @classmethod
    def truncate(cls, hours=12):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        return cls.delete().where(
            (cls.timestamp < (now - timedelta(hours=hours)))
        ).execute()

    @classmethod
    def prepare(cls, session, event):
        return {
            'session': session,
            'seq': event['s'],
            'timestamp': datetime.now(timezone.utc).replace(tzinfo=None),
            'event': event['t'],
            'data': event['d'],
        }