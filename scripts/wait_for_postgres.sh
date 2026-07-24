#!/usr/bin/env sh
set -eu

: "${PGHOST:?PGHOST is required}"
: "${PGUSER:?PGUSER is required}"
: "${PGDATABASE:?PGDATABASE is required}"

attempts="${POSTGRES_WAIT_ATTEMPTS:-60}"
while [ "$attempts" -gt 0 ]; do
  if pg_isready -q; then
    exit 0
  fi
  attempts=$((attempts - 1))
  sleep 1
done

echo "PostgreSQL did not become ready" >&2
exit 1
