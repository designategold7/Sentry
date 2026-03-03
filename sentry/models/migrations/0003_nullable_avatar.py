from sentry.models.migrations import Migrate
from sentry.models.user import User # FIXED: Was erroneously importing from models.guild

@Migrate.only_if(Migrate.non_nullable, User, 'avatar')
def alter_guild_columns(m):
    m.drop_not_nulls(User, User.avatar)