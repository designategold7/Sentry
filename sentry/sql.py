import os
import yaml
import time
from peewee import OperationalError, Model
from playhouse.postgres_ext import PostgresqlExtDatabase

database = PostgresqlExtDatabase(None)

class BaseModel(Model):
    REGISTERED_MODELS = {}
    @classmethod
    def register(cls, model_cls):
        cls.REGISTERED_MODELS[model_cls.__name__.lower()] = model_cls
        return model_cls
    class Meta:
        database = database

def init_db(env_name_unused):
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config_path = os.path.join(base_dir, 'config.yaml')
    
    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)
    
    # Direct mapping to user's 'database' root key
    db_config = config.get('database')
    if not db_config:
        raise KeyError(f"Critical: 'database' block missing from root of {config_path}")
    
    database.init(
        db_config['name'],  # User's YAML uses 'name' for the DB name
        host=db_config['host'],
        port=int(db_config.get('port', 5432)),
        user=db_config['user'],
        password=db_config.get('password', '')  # Safe fallback for missing password
    )
    
    retries = 0
    while retries < 15:
        try:
            database.connect(reuse_if_open=True)
            print("Postgres foundation synchronized with 'database' root key.")
            return
        except (OperationalError, Exception) as e:
            err_msg = str(e).lower()
            if 'starting up' in err_msg or 'connection refused' in err_msg:
                print(f"Postgres is booting (Attempt {retries+1}/15), waiting 3s...")
                time.sleep(3)
                retries += 1
            else:
                raise e
    raise Exception("Critical Failure: Database handshake timed out.")