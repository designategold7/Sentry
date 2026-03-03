#!/usr/bin/env python
import flask
if not hasattr(flask.Flask, 'before_first_request'):
    def before_first_request(self, f):
        self.before_request(f)
        return f
    flask.Flask.before_first_request = before_first_request
import os
import click
import logging
import asyncio
import yaml
import discord
from discord.ext import commands
from werkzeug.serving import run_simple
from sentry import ENV
from sentry.web import sentry_app
from sentry.sql import init_db
@click.group()
def cli(): logging.getLogger().setLevel(logging.INFO)
@cli.command()
@click.option('--reloader/--no-reloader', '-r', default=False)
def serve(reloader):
    init_db(ENV)
    run_simple('0.0.0.0', 8686, sentry_app, use_reloader=reloader, use_debugger=True)
@cli.command()
@click.option('--env', '-e', default='local')
def bot(env):
    init_db(env)
    with open('config.yaml', 'r') as f: config = yaml.safe_load(f)
    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True
    intents.presences = True
    client = commands.Bot(command_prefix='!', intents=intents)
    async def main():
        async with client:
            for f in os.listdir('sentry/plugins'):
                if f.endswith('.py') and not f.startswith('__'): await client.load_extension(f'sentry.plugins.{f[:-3]}')
            await client.start(config['token'])
    asyncio.run(main())
@cli.command()
def workers():
    from sentry.tasks import TaskWorker
    logging.getLogger('peewee').setLevel(logging.INFO)
    init_db(ENV)
    asyncio.run(TaskWorker().run())
@cli.command('add-global-admin')
@click.argument('user-id')
def add_global_admin(user_id):
    from sentry.redis import rdb
    from sentry.models.user import User
    init_db(ENV)
    rdb.sadd('global_admins', user_id)
    User.update(admin=True).where(User.user_id == user_id).execute()
    print(f'Ok, added {user_id} as a global admin')
if __name__ == '__main__': cli()