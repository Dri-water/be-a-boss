#!/bin/sh
set -eu

# Codex stores SQLite state beside its credentials. SQLite locking is unreliable on
# a Windows/macOS Docker bind mount, so /root/.codex is a native named volume and
# the host login is copied in once as seed material.
mkdir -p /root/.codex
if [ ! -f /root/.codex/auth.json ] && [ -f /host-agent-auth/codex/auth.json ]; then
    install -m 600 /host-agent-auth/codex/auth.json /root/.codex/auth.json
fi
if [ ! -f /root/.codex/config.toml ] && [ -f /host-agent-auth/codex/config.toml ]; then
    install -m 600 /host-agent-auth/codex/config.toml /root/.codex/config.toml
fi

exec "$@"
