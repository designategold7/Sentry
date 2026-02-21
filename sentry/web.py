import os; os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
import logging
from flask import Flask, g, session
from holster.flask_ext import Holster
from sentry import ENV
from sentry.sql import init_db
from sentry.models.user import User
from sentry.types.guild import PluginsConfig
from yaml import load
sentry_app = Holster(Flask(__name__))
logging.getLogger('peewee').setLevel(logging.DEBUG)
@sentry_app.app.before_first_request
def before_first_request():
    init_db(ENV)
    PluginsConfig.force_load_plugin_configs()
    with open('config.yaml', 'r') as f:
        data = load(f)
    sentry_app.app.config.update(data['web'])
    sentry_app.app.secret_key = data['web']['SECRET_KEY']
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