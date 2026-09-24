#!/usr/bin/env bash
# Start a throwaway Postgres 16 for the backend tests (port 55432, trust auth, local only).
# Usage: scripts/dev_test_db.sh   then   export TEST_DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:55432/catalystedge_test
set -euo pipefail
PGBIN=${PGBIN:-/usr/lib/postgresql/16/bin}
DIR=${PGDATA_DIR:-/var/tmp/cepg}
RUN_AS=${PG_RUN_AS:-postgres}
if pg_isready -h 127.0.0.1 -p 55432 >/dev/null 2>&1; then echo "already running"; exit 0; fi
mkdir -p "$DIR" && chown "$RUN_AS" "$DIR"
if [ ! -f "$DIR/data/PG_VERSION" ]; then
  su "$RUN_AS" -c "$PGBIN/initdb -D $DIR/data -U postgres --auth=trust >/dev/null"
fi
su "$RUN_AS" -c "$PGBIN/pg_ctl -D $DIR/data -o '-p 55432 -k $DIR -c listen_addresses=127.0.0.1' -l $DIR/log start >/dev/null"
for _ in $(seq 1 20); do pg_isready -h 127.0.0.1 -p 55432 >/dev/null 2>&1 && break; sleep 0.5; done
psql -h 127.0.0.1 -p 55432 -U postgres -tc "SELECT 1 FROM pg_database WHERE datname='catalystedge_test'" | grep -q 1 \
  || psql -h 127.0.0.1 -p 55432 -U postgres -qc "CREATE DATABASE catalystedge_test"
echo "ready: postgresql+psycopg://postgres@127.0.0.1:55432/catalystedge_test"
