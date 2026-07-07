#!/usr/bin/env bash
set -euo pipefail

# Apply database migrations before starting the app. Postgres is guaranteed to
# be reachable because docker-compose gates startup on its healthcheck.
echo "Running database migrations..."
alembic upgrade head

echo "Starting application: $*"
exec "$@"
