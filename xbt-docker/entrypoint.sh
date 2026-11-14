#!/bin/bash
# JoinMarket-NG XBT container entrypoint (runs under tini -g).
# Starts JoinBlaket's RPC filter, Tor, checks the node is Bitcoin BLAKE2b, then
# the orderbook watcher and jmwalletd (which serves Jam). If any of them exits,
# the container exits and Docker's restart policy brings it back.
#
# RPC filter (xbt6): the node's RPC credentials go to rpc_filter.py only, which
# forwards to the node just the RPCs JoinBlaket uses, on JoinBlaket's own
# wallets. Everything that talks to the network (jmwalletd, Jam, maker, taker,
# orderbook watcher) gets the filter's own random credentials instead.
#   * Started as root (the image default): the filter runs as user jmrpc, the
#     only one that can read the node credentials (/run/jm-xbt-rpc/node.cookie),
#     and everything else runs as uid 1000 (jm), which cannot read them nor
#     look into the filter's process.
#   * Started as uid 1000 (--user 1000): the filter still runs and still
#     filters, but as the same user, so a compromised jmwalletd could read the
#     credentials. A warning says so.
#   * JM_XBT_RPC_FILTER=external: the filter runs elsewhere (StartOS and
#     Umbrel run it as its own container); the BITCOIN_RPC_* given here are
#     already the filter's, not the node's.
set -euo pipefail

log() { echo "[jm-xbt] $*"; }

FILTER_PORT=28187

# Short RPC variables (BITCOIN_RPC_URL/USER/PASS/COOKIE) -> settings variables.
[ -n "${BITCOIN_RPC_URL:-}" ] && export BITCOIN__RPC_URL="$BITCOIN_RPC_URL"
[ -n "${BITCOIN_RPC_USER:-}" ] && export BITCOIN__RPC_USER="$BITCOIN_RPC_USER"
[ -n "${BITCOIN_RPC_PASS:-}" ] && export BITCOIN__RPC_PASSWORD="$BITCOIN_RPC_PASS"
[ -n "${BITCOIN_RPC_PASSWORD:-}" ] && export BITCOIN__RPC_PASSWORD="$BITCOIN_RPC_PASSWORD"
[ -n "${BITCOIN_RPC_COOKIE:-}" ] && export BITCOIN__RPC_COOKIE_FILE="$BITCOIN_RPC_COOKIE"
unset BITCOIN_RPC_URL BITCOIN_RPC_USER BITCOIN_RPC_PASS BITCOIN_RPC_PASSWORD BITCOIN_RPC_COOKIE

# Start the filter with the node credentials now in BITCOIN__RPC_*, then point
# BITCOIN__RPC_* at the filter. $1 = user to run it as ("" = this one), $2 = dir.
start_rpc_filter() {
    local as_user=$1 dir=$2 node_url=${BITCOIN__RPC_URL:-}
    if [ -z "$node_url" ]; then
        log "no node RPC URL given (BITCOIN_RPC_URL); the RPC filter is not started"
        return 0
    fi
    case "$node_url" in
        http://*) ;;
        *) echo "[jm-xbt] BITCOIN_RPC_URL must be http://host:port" >&2; exit 1 ;;
    esac
    local node_user=${BITCOIN__RPC_USER:-} node_pass=${BITCOIN__RPC_PASSWORD:-}
    local cookie_src=${BITCOIN__RPC_COOKIE_FILE:-}
    mkdir -p "$dir"
    # node credentials: a user:password file only the filter's user can read
    if [ -n "$node_user" ] && [ -n "$node_pass" ]; then
        (umask 077 && printf '%s:%s\n' "$node_user" "$node_pass" > "$dir/node.cookie")
    elif [ -n "$cookie_src" ]; then
        install -m 0400 "$cookie_src" "$dir/node.cookie"
    else
        echo "[jm-xbt] no node RPC credentials (BITCOIN_RPC_USER/PASS or BITCOIN_RPC_COOKIE)" >&2
        exit 1
    fi
    # the filter's own credentials, for JoinBlaket
    (umask 077 && python -c 'import secrets; print("jmrpcfilter:" + secrets.token_hex(32))' > "$dir/client.cookie")
    local run=()
    if [ -n "$as_user" ]; then
        chown root:root "$dir"
        chmod 0755 "$dir"
        chown "$as_user:$as_user" "$dir/node.cookie"
        chmod 0400 "$dir/node.cookie"
        chown "$as_user:jm" "$dir/client.cookie"
        chmod 0440 "$dir/client.cookie"
        run=(setpriv --reuid="$as_user" --regid="$as_user" --clear-groups --no-new-privs
             --bounding-set=-all --inh-caps=-all)
        if [ -z "$node_user" ] && [ -n "$cookie_src" ]; then
            # The node writes a new cookie when it restarts: keep the copy current.
            ( while sleep 10; do
                  if [ -r "$cookie_src" ] && ! cmp -s "$cookie_src" "$dir/node.cookie"; then
                      install -o "$as_user" -g "$as_user" -m 0400 "$cookie_src" "$dir/node.cookie.new" &&
                          mv -f "$dir/node.cookie.new" "$dir/node.cookie"
                  fi
              done ) &
        fi
    else
        chmod 0700 "$dir"
    fi
    "${run[@]}" env -i PATH="$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
        JM_RPC_FILTER_LISTEN="127.0.0.1:$FILTER_PORT" \
        JM_RPC_FILTER_NODE_URL="$node_url" \
        JM_RPC_FILTER_NODE_COOKIE="$dir/node.cookie" \
        JM_RPC_FILTER_CLIENT_COOKIE="$dir/client.cookie" \
        ${JM_RPC_FILTER_MODE:+JM_RPC_FILTER_MODE="$JM_RPC_FILTER_MODE"} \
        python /usr/local/lib/jm-xbt/rpc_filter.py &
    export JM_XBT_RPC_FILTER_PID=$!
    unset BITCOIN__RPC_USER BITCOIN__RPC_PASSWORD
    export BITCOIN__RPC_URL="http://127.0.0.1:$FILTER_PORT"
    export BITCOIN__RPC_COOKIE_FILE="$dir/client.cookie"
    export JM_XBT_RPC_FILTER=started
    local i=0
    until python -c "import socket; socket.create_connection(('127.0.0.1', $FILTER_PORT), 1).close()" 2>/dev/null; do
        i=$((i + 1))
        if [ "$i" -ge 30 ] || [ ! -e "/proc/$JM_XBT_RPC_FILTER_PID" ]; then
            echo "[jm-xbt] the RPC filter did not start" >&2
            exit 1
        fi
        sleep 1
    done
    log "RPC filter up on 127.0.0.1:$FILTER_PORT${as_user:+ as user $as_user}, node $node_url"
}

if [ "$(id -u)" = 0 ]; then
    if [ "${JM_XBT_RPC_FILTER:-}" != external ]; then
        start_rpc_filter jmrpc /run/jm-xbt-rpc
    fi
    # Everything else as uid 1000, without root's leftovers.
    exec setpriv --reuid=jm --regid=jm --init-groups --no-new-privs \
        --bounding-set=-all --inh-caps=-all "$0" "$@"
fi

case "${JM_XBT_RPC_FILTER:-}" in
    started | external) ;;
    *)
        log "WARNING: not started as root, so the RPC filter runs as this same user;"
        log "WARNING: it still limits the RPCs, but cannot hide the node credentials from jmwalletd."
        start_rpc_filter "" "$(mktemp -d /tmp/jm-xbt-rpc.XXXXXX)"
        ;;
esac

# External filter that writes its credentials to a shared file (Umbrel): wait for it.
if [ "${JM_XBT_RPC_FILTER:-}" = external ] && [ -n "${BITCOIN__RPC_COOKIE_FILE:-}" ]; then
    i=0
    until [ -s "$BITCOIN__RPC_COOKIE_FILE" ]; do
        i=$((i + 1))
        if [ "$i" -ge 120 ]; then
            echo "[jm-xbt] no RPC filter credentials in $BITCOIN__RPC_COOKIE_FILE" >&2
            exit 1
        fi
        sleep 1
    done
fi

# Telegram notices for the maker (who takes an offer, CoinJoin done, directory lost...).
# From TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, or from /data/telegram.env with those two
# lines (StartOS writes it from an action; on Umbrel it is edited in the app's data folder).
# Any NOTIFICATIONS__URLS already set (any Apprise URL) wins.
if [ -f /data/telegram.env ]; then
    TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-$(sed -n 's/^TELEGRAM_BOT_TOKEN=//p' /data/telegram.env | tr -d '"[:space:]')}"
    TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-$(sed -n 's/^TELEGRAM_CHAT_ID=//p' /data/telegram.env | tr -d '"[:space:]')}"
fi
if [ -z "${NOTIFICATIONS__URLS:-}" ] && [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
    export NOTIFICATIONS__ENABLED=true
    export NOTIFICATIONS__URLS="[\"tgram://${TELEGRAM_BOT_TOKEN}/${TELEGRAM_CHAT_ID}\"]"
    export NOTIFICATIONS__TITLE_PREFIX="${NOTIFICATIONS__TITLE_PREFIX:-JoinBlaket}"
    log "Telegram notices on (chat ${TELEGRAM_CHAT_ID})"
fi
unset TELEGRAM_BOT_TOKEN

if [ "$(id -u)" = 0 ]; then
    echo "[jm-xbt] refusing to run as root" >&2
    exit 1
fi

# The filter is a sibling process (another user when started as root): if it
# stops, stop the container too (fail closed, and Docker restarts everything).
watch_rpc_filter() {
    local pid=$1
    while [ -e "/proc/$pid" ] && ! grep -qE '^[0-9]+ \(.*\) Z' "/proc/$pid/stat" 2>/dev/null; do
        sleep 5
    done
    echo "[jm-xbt] the RPC filter stopped" >&2
    return 1
}

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

if [ -n "${JM_XBT_RPC_FILTER_PID:-}" ]; then
    watch_rpc_filter "$JM_XBT_RPC_FILTER_PID" &
fi

rc=0
wait -n || rc=$?
log "a process exited (code $rc); stopping"
exit "$rc"
