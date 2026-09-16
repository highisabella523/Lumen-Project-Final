#!/bin/sh
# Railway mounts the persistent volume at /data. Prepare only that dedicated
# application-state path, then run the relay as an unprivileged user.
set -eu

if [ "$(id -u)" = "0" ]; then
    mkdir -p /data
    chown -R lumen:lumen /data
    exec gosu lumen "$@"
fi

exec "$@"