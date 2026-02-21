#!/bin/bash
set -e

psql -v ON_ERROR_STOP=1 --username "postgres" -d sentry -c "CREATE EXTENSION hstore;"
psql -v ON_ERROR_STOP=1 --username "postgres" -d sentry -c "CREATE EXTENSION pg_trgm;"