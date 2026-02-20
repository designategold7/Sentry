from sentry.models.migrations import Migrate
from sentry.models.guild import Guild
@Migrate.only_if(Migrate.missing, Guild, 'premium_sub_id')
def add_channel_type_column(m):
    m.add_columns(Guild, Guild.premium_sub_id)