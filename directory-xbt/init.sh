#!/bin/sh
# One-time setup of a JoinBlaket directory node.
#   1. starts Tor, which creates the onion address (the node's identity)
#   2. waits for the address and writes it to .env
#   3. starts the directory
# Running it again is safe: if .env already has the address it only makes
# sure everything is up.
set -eu
cd "$(dirname "$0")"

say() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "Docker is not installed (see README)."
docker compose version >/dev/null 2>&1 || die "'docker compose' is missing (package docker-compose-plugin, see README)."
docker info >/dev/null 2>&1 || die "Cannot talk to Docker. Add your user to the docker group (see README) or run with sudo."

# Tor runs as the person who owns this folder, not as root.
if [ "$(id -u)" = 0 ] && [ -n "${SUDO_UID:-}" ]; then
    HOST_UID=$SUDO_UID; HOST_GID=$SUDO_GID
else
    HOST_UID=$(id -u); HOST_GID=$(id -g)
fi

HS=tor/data/hidden_service
ID_VAR=DIRECTORY_SERVER__NICK_AUTH_DIRECTORY_ID

if [ -f .env ] && grep -q "^$ID_VAR=." .env; then
    say "Already initialised: $(grep "^$ID_VAR=" .env | cut -d= -f2)"
    docker compose up -d
    exit 0
fi

mkdir -p tor/data
if [ "$(id -u)" = 0 ]; then chown -R "$HOST_UID:$HOST_GID" tor/data; fi
chmod 700 tor/data

{
    echo "# Written by init.sh. The onion address is also in $HS/hostname."
    echo "HOST_UID=$HOST_UID"
    echo "HOST_GID=$HOST_GID"
} > .env

say "Downloading the images..."
docker compose pull

say "Starting Tor (the first start can take a minute or two)..."
docker compose up -d tor

i=0
while [ ! -s "$HS/hostname" ]; do
    i=$((i + 1))
    [ "$i" -le 180 ] || die "Tor did not create $HS/hostname in 3 minutes. Look at: docker compose logs tor"
    sleep 1
done

ONION=$(cat "$HS/hostname")
echo "$ONION" | grep -Eq '^[a-z2-7]{56}\.onion$' || die "Unexpected onion address: $ONION"

echo "$ID_VAR=$ONION:5222" >> .env

say "Starting the directory..."
docker compose up -d

say ""
say "Done. Your directory address is:"
say ""
say "    $ONION:5222"
say ""
say "Next steps:"
say "  1. ./health.sh             check that it works (wait a couple of minutes first)"
say "  2. ./backup.sh             save the onion keys somewhere safe"
say "  3. send us the address above"
