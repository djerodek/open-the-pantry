#!/bin/sh
set -e

# Optional: match the container's internal user to a specific host UID/GID
# (e.g. if ./data is already owned by a non-1000 user on the host). Falls
# back to the built-in appuser (1000:1000) if unset -- this is the common
# case and needs no configuration.
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

if [ "$PUID" != "1000" ] || [ "$PGID" != "1000" ]; then
    groupmod -o -g "$PGID" appuser
    usermod -o -u "$PUID" appuser
fi

# /app/data is normally a bind-mounted host directory (./data in
# docker-compose.yml). Its ownership comes from the host side at runtime,
# not from anything baked into the image -- a Dockerfile-time chown alone
# wouldn't apply once the volume is mounted over it. Fixing ownership here,
# at container start, handles a fresh bind mount (created root:root by the
# Docker daemon on first run) without requiring the user to manually chown
# anything on the host first.
#
# Skipped once ownership already matches -- a full recursive chown on every
# restart gets slow once ./data has accumulated a lot of uploaded images.
current_uid="$(stat -c %u /app/data)"
current_gid="$(stat -c %g /app/data)"
if [ "$current_uid" != "$PUID" ] || [ "$current_gid" != "$PGID" ]; then
    chown -R appuser:appuser /app/data
fi

exec gosu appuser "$@"
