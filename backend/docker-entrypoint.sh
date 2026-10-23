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

# Settings -> Backup & export -> Automatic backups can write to a folder on
# another disk (RECIPE_APP_BACKUP_DIR, mounted into the container). It may
# be a share holding other files, so only the folder itself and the app's
# own backups change owner, never what else is in it; and a share that
# refuses (some NAS mounts, read-only) doesn't stop the app from starting:
# Settings then shows why the backup failed.
if [ -n "$RECIPE_APP_BACKUP_DIR" ] && [ -d "$RECIPE_APP_BACKUP_DIR" ]; then
    b_uid="$(stat -c %u "$RECIPE_APP_BACKUP_DIR")"
    b_gid="$(stat -c %g "$RECIPE_APP_BACKUP_DIR")"
    if [ "$b_uid" != "$PUID" ] || [ "$b_gid" != "$PGID" ]; then
        if ! chown appuser:appuser "$RECIPE_APP_BACKUP_DIR"; then
            echo "Warning: couldn't change the owner of $RECIPE_APP_BACKUP_DIR; automatic backups may fail to write there." >&2
        fi
    fi
    # Exactly the names the app writes (scheduled_backup.NAME_RE, plus the
    # .partial of one being written), not anything that merely starts alike.
    find "$RECIPE_APP_BACKUP_DIR" -maxdepth 1 -type f -regextype posix-extended \
        -regex '.*/open-the-pantry-backup-[0-9]{8}-[0-9]{6}(-[0-9]+)?\.zip(\.partial)?' \
        ! -user "$PUID" -exec chown appuser:appuser {} + 2>/dev/null || true
fi

exec gosu appuser "$@"
