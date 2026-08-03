#!/bin/sh
set -eu

# CODEX_HOME is the host's live Codex directory, including its rotating OAuth
# credential. SQLite alone lives on the native named volume via CODEX_SQLITE_HOME.
# Before this split, the volume held both; merge its non-SQLite session artifacts
# into the shared home once so existing bot conversations remain resumable.
mkdir -p /root/.codex /root/.codex-sqlite
marker=/root/.codex-sqlite/.beaboss-home-migrated
if [ ! -f "$marker" ]; then
    for directory in sessions archived_sessions shell_snapshots; do
        source_dir="/root/.codex-sqlite/$directory"
        if [ -d "$source_dir" ]; then
            mkdir -p "/root/.codex/$directory"
            cp -an "$source_dir/." "/root/.codex/$directory/"
        fi
    done
    touch "$marker"
fi

exec "$@"
