#!/bin/sh
set -e

# With no args: loop mode via SYNC_INTERVAL (default 900s), for `docker compose up`.
# With args (e.g. `docker compose run --rm sync --dry-run`): pass them straight
# through, giving one-shot behavior from the same image/compose file.
if [ "$#" -gt 0 ]; then
    exec termix-aws-sync "$@"
fi
exec termix-aws-sync --interval "${SYNC_INTERVAL:-900}"
