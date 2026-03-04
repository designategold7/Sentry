#!/bin/bash
set -e
psql -v ON_ERROR_STOP=1 --username "sentry" -d sentry -c "CREATE EXTENSION IF NOT EXISTS hstore;"
psql -v ON_ERROR_STOP=1 --username "sentry" -d sentry -c "CREATE EXTENSION IF NOT EXISTS pg_trgm;"