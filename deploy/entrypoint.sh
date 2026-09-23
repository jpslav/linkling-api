#!/bin/sh
# Starts as root only long enough to make the bind-mounted /data usable by the service's own
# user, then drops to it for good (ADR-0015 ii). On Linux, a host directory Docker creates
# for a bind mount is owned by root (INFERRED from Docker's documentation), which a non-root
# service cannot write into.
set -eu

uid=10001

if [ "$(id -u)" = 0 ]; then
    # Only /data itself and the database's own files are touched -- including one put back
    # by hand during a restore. A deployer's other files in the directory are left alone.
    find /data -maxdepth 1 \( -path /data -o -name 'linkling.db*' \) ! -user "$uid" \
        -exec chown "$uid:$uid" {} +
    exec setpriv --reuid="$uid" --regid="$uid" --clear-groups "$@"
fi

exec "$@"
