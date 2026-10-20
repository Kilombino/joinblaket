#!/bin/bash
# JoinMarket-NG XBT container entrypoint (runs under tini -g as uid 1000).
# Starts Tor, checks the node is Bitcoin BLAKE2b, then the orderbook watcher
# and jmwalletd (which serves Jam). If any of them exits, the container exits
# and Docker's restart policy brings it back.
set -euo pipefail

log() { echo "[jm-xbt] $*"; }

# Short RPC variables (BITCOIN_RPC_URL/USER/PASS/COOKIE) -> settings variables.
[ -n "${BITCOIN_RPC_URL:-}" ] && export BITCOIN__RPC_URL="$BITCOIN_RPC_URL"
[ -n "${BITCOIN_RPC_USER:-}" ] && export BITCOIN__RPC_USER="$BITCOIN_RPC_USER"
[ -n "${BITCOIN_RPC_PASS:-}" ] && export BITCOIN__RPC_PASSWORD="$BITCOIN_RPC_PASS"
[ -n "${BITCOIN_RPC_PASSWORD:-}" ] && export BITCOIN__RPC_PASSWORD="$BITCOIN_RPC_PASSWORD"
[ -n "${BITCOIN_RPC_COOKIE:-}" ] && export BITCOIN__RPC_COOKIE_FILE="$BITCOIN_RPC_COOKIE"
unset BITCOIN_RPC_PASS BITCOIN_RPC_PASSWORD

if [ "$(id -u)" = 0 ]; then
    echo "[jm-xbt] refusing to run as root" >&2
    exit 1
fi

umask 077
mkdir -p /data/tor /data/obwatcher
chmod 700 /data /data/tor

log "starting Tor"
tor -f /etc/jm-xbt/torrc &

i=0
until python -c "import socket; socket.create_connection(('127.0.0.1', 28185), 1).close()" 2>/dev/null; do
    i=$((i + 1))
    if [ "$i" -ge 60 ]; then
        echo "[jm-xbt] Tor SOCKS port did not open" >&2
        exit 1
    fi
    sleep 1
done

log "checking the node (${BITCOIN__RPC_URL:-unset})"
rc=0
python /usr/local/lib/jm-xbt/preflight.py || rc=$?
if [ "$rc" -eq 2 ]; then
    echo "[jm-xbt] this node is not Bitcoin BLAKE2b (XBT) for this network; not starting." >&2
    sleep 300   # avoid a tight restart loop
    exit 2
elif [ "$rc" -ne 0 ]; then
    exit "$rc"
fi

log "starting orderbook watcher"
JOINMARKET_CONFIG_FILE=/data/config.toml JOINMARKET_DATA_DIR=/data/obwatcher \
    python -m orderbook_watcher.main &

log "starting jmwalletd on ${JMWALLETD_HOST}:28183"
jmwalletd &

rc=0
wait -n || rc=$?
log "a process exited (code $rc); stopping"
exit "$rc"
