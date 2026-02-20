from sentry.models.migrations import Migrate
from sentry.models.user import User 
@Migrate.only_if(Migrate.missing, User, 'admin')
def add_guild_columns(m):
    m.add_columns(User, User.admin)