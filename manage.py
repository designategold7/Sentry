#!/usr/bin/env python
from gevent import monkey; monkey.patch_all()
import os
import copy
import click
import signal
import logging
import gevent
import subprocess
from werkzeug.serving import run_simple
from sentry import ENV
from sentry.web import sentry_app
from sentry.sql import init_db
class BotSupervisor(object):
    def __init__(self, env={}):
        self.proc = None
        self.env = env
        self.bind_signals()
        self.start()
    def bind_signals(self):
        signal.signal(signal.SIGUSR1, self.handle_sigusr1)
    def handle_sigusr1(self, signum, frame):
        print('SIGUSR1 - RESTARTING')
        gevent.spawn(self.restart)
    def start(self):
        env = copy.deepcopy(os.environ)
        env.update(self.env)
        self.proc = subprocess.Popen(['python', '-m', 'disco.cli', '--config', 'config.yaml'], env=env)
    def stop(self):
        self.proc.terminate()
    def restart(self):
        try:
            self.stop()
        except:
            pass
        self.start()
    def run_forever(self):
        while True:
            self.proc.wait()
            gevent.sleep(5)
@click.group()
def cli():
    logging.getLogger().setLevel(logging.INFO)
@cli.command()
@click.option('--reloader/--no-reloader', '-r', default=False)
def serve(reloader):
    init_db(ENV)
    run_simple('0.0.0.0', 8686, sentry_app, use_reloader=reloader, use_debugger=True)
@cli.command()
@click.option('--env', '-e', default='local')
def bot(env):
    init_db(env)
    BotSupervisor(env={'ENV': env}).run_forever()
@cli.command()
def workers():
    from sentry.tasks.worker import TaskWorker
    logging.getLogger('peewee').setLevel(logging.INFO)
    init_db(ENV)
    TaskWorker().run()
@cli.command('add-global-admin')
@click.argument('user-id')
def add_global_admin(user_id):
    from sentry.redis import rdb
    from sentry.models.user import User
    init_db(ENV)
    rdb.sadd('global_admins', user_id)
    User.update(admin=True).where(User.user_id == user_id).execute()
    print('Ok, added {} as a global admin'.format(user_id))
if __name__ == '__main__':
    cli()