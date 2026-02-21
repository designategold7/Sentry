import os
import psycogreen.gevent
psycogreen.gevent.patch_psycopg()

from peewee import DatabaseProxy, Model, Expression
from playhouse.postgres_ext import PostgresqlExtDatabase

REGISTERED_MODELS = []
database = DatabaseProxy()

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
    if env == 'docker':
        db_obj = PostgresqlExtDatabase(
            'sentry',
            host='db',
            user='sentry',  # Updated from 'postgres' to match Dockerfile
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
    
    # Ensure the database is actually reachable before model creation
    database.connect(reuse_if_open=True)
    
    for model in REGISTERED_MODELS:
        model.create_table(safe=True)

def reset_db():
    init_db(os.getenv('ENV', 'local'))
    for model in REGISTERED_MODELS:
        model.drop_table(safe=True)
        model.create_table(safe=True)