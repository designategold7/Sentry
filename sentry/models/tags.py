from peewee import (
    BigIntegerField, TextField, DateTimeField, CompositeKey, IntegerField
)
from datetime import datetime, timezone
from sentry.sql import BaseModel

@BaseModel.register
class Tag(BaseModel):
    guild_id = BigIntegerField()
    author_id = BigIntegerField()
    name = TextField()
    content = TextField()
    times_used = IntegerField(default=0)
    created_at = DateTimeField(default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))

    class Meta:
        db_table = 'tags'
        primary_key = CompositeKey('guild_id', 'name')