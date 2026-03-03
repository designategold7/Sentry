import os
import logging
import subprocess
from raven import Client

# Replaced disco's proprietary LOG_FORMAT with a standard Python logging format
LOG_FORMAT = '[%(levelname)s] %(asctime)s - %(name)s:%(lineno)d - %(message)s'

ENV = os.getenv('ENV', 'local')
DSN = os.getenv('DSN')

try:
    # Decode bytes to string for modern Python 3 compatibility
    REV = subprocess.check_output(['git', 'rev-parse', 'HEAD']).strip().decode('utf-8')
except Exception:
    REV = 'unknown'
    
VERSION = '1.3.0'

raven_client = Client(
    DSN,
    ignore_exceptions=[
        'KeyboardInterrupt',
    ],
    release=REV,
    environment=ENV,
    # REMOVED: GeventedHTTPTransport. Reverting to default threaded transport for asyncio compatibility.
)

# Log things to file
file_handler = logging.FileHandler('sentry.log')
log = logging.getLogger()
file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
log.addHandler(file_handler)