import os
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
import logging
from flask import Flask, g, session
from holster.flask_ext import Holster
from sentry import ENV
from sentry.sql import init_db
from sentry.models.user import User
from sentry.types.guild import PluginsConfig
from yaml import safe_load  
raw_app = Flask(__name__)
raw_app.logger_name = raw_app.name
sentry_app = Holster(raw_app)
# -------------------------------------

logging.getLogger('peewee').setLevel(logging.DEBUG)

@sentry_app.app.before_first_request
def before_first_request():
    init_db(ENV)
    PluginsConfig.force_load_plugin_configs()
    with open('config.yaml', 'r') as f:
        data = safe_load(f) 
    
    web_config = data.get('web', {})
    sentry_app.app.config.update(web_config)
    
 
    sentry_app.app.secret_key = web_config.get('SECRET_KEY', 'sentry_default_secret_key_8675309')
    sentry_app.app.config['token'] = data.get('token')

@sentry_app.app.before_request
def check_auth():
    g.user = None
    if 'uid' in session:
        g.user = User.with_id(session['uid'])

@sentry_app.app.after_request
def save_auth(response):
    if g.user and 'uid' not in session:
        session['uid'] = g.user.id
    elif not g.user and 'uid' in session:
        del session['uid']
    return response

@sentry_app.app.context_processor
def inject_data():
    return dict(
        user=g.user,
    )