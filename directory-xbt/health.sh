#!/bin/sh
# Health check of the JoinBlaket directory node.
#   ./health.sh          full check, including reaching our own onion through Tor (~1 min)
#   ./health.sh --quick  skip the Tor round trip
# Exit code 0 = healthy. Usable from cron or a monitoring tool.
set -u
cd "$(dirname "$0")"

ok=0
pass() { printf '  [OK]   %s\n' "$*"; }
fail() { printf '  [FAIL] %s\n' "$*"; ok=1; }

echo "JoinBlaket directory health check"

running=$(docker compose ps --status running --services 2>/dev/null)
for s in tor directory; do
    if echo "$running" | grep -qx "$s"; then pass "container $s is running"; else fail "container $s is not running (docker compose logs $s)"; fi
done

if docker compose exec -T directory jm-directory-ctl health >/dev/null 2>&1; then
    pass "directory answers its health check"
else
    fail "directory health check failed (docker compose logs directory)"
fi

stats=$(docker compose exec -T directory jm-directory-ctl status 2>/dev/null)
if [ -n "$stats" ]; then
    echo "$stats" | grep -E '^(Network|Uptime|Connected peers|Passive peers|Active peers)' | sed 's/^/         /'
fi

ONION=$(cat tor/data/hidden_service/hostname 2>/dev/null || true)
if [ -n "$ONION" ]; then
    pass "onion address: $ONION:5222"
else
    fail "no onion address yet (did you run ./init.sh?)"
fi

if [ "${1:-}" != "--quick" ] && [ -n "$ONION" ]; then
    echo "  ....   reaching $ONION:5222 through the Tor network (up to 2 minutes)..."
    # A bare TCP connection through Tor: it only succeeds if the onion is
    # published AND the directory accepted the connection behind it. The
    # directory drops it after 30 s without a handshake; that is expected.
    reach() {
        docker compose exec -T tor curl -sS --max-time 120 \
            --socks5-hostname 127.0.0.1:9050 "telnet://$ONION:5222" </dev/null >/dev/null 2>&1
    }
    # Second try after a pause: right after a restart Tor may still hold the
    # old descriptor of our own onion.
    if reach || { sleep 20; reach; }; then
        pass "onion reachable from the Tor network"
    else
        fail "onion not reachable through Tor yet (right after the first start this can take a few minutes; try again)"
    fi
fi

if [ "$ok" = 0 ]; then echo "HEALTHY"; else echo "PROBLEMS FOUND"; fi
exit "$ok"
