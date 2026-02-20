from sentry.models.migrations import Migrate
from sentry.models.message import StarboardEntry
@Migrate.only_if(Migrate.missing, StarboardEntry, 'blocked_stars')
def add_guild_columns(m):
    m.add_columns(
        StarboardEntry,
        StarboardEntry.blocked_stars,
        StarboardEntry.blocked
    )