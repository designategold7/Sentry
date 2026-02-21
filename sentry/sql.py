import os
import psycogreen.gevent
psycogreen.gevent.patch_psycopg()

from peewee import DatabaseProxy, Model, Expression, SQL
from playhouse.postgres_ext import PostgresqlExtDatabase

REGISTERED_MODELS = []

# Create a database proxy we can setup post-init (Updated for Peewee 3.x)
database = DatabaseProxy()

# Define the custom Postgres case-insensitive regex operator
def pg_regex_i(lhs, rhs):
    return Expression(lhs, 'IRGX', rhs)

class BaseModel(Model):
    class Meta:
        database = database

    @staticmethod
    def register(cls):
        REGISTERED_MODELS.append(cls)
        return cls

def init_db(env):
    # Initialize the modern Postgres connection
    if env == 'docker':
        db_obj = PostgresqlExtDatabase(
            'sentry',
            host='db',
            user='postgres',
            port=int(os.getenv('PG_PORT', 5432)),
            autorollback=True
        )
    else:
        db_obj = PostgresqlExtDatabase(
            'sentry',
            user='sentry',
            port=int(os.getenv('PG_PORT', 5432)),
            autorollback=True
        )
        
    database.initialize(db_obj)

    # Register the custom regex operator in the Peewee 3.x connection context
    database.connection_context()
    
    for model in REGISTERED_MODELS:
        model.create_table(safe=True)

        if hasattr(model, 'SQL'):
            database.execute_sql(model.SQL)

def reset_db():
    init_db(os.getenv('ENV', 'local'))

    for model in REGISTERED_MODELS:
        model.drop_table(safe=True)
        model.create_table(safe=True)