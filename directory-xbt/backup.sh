#!/bin/sh
# Copies the onion keys (the directory's identity) into one file.
#   ./backup.sh [destination folder]      default: your home folder
# Keep the file OFF this machine (USB stick, password manager...). Whoever has
# it can impersonate this directory; without it, a new machine gets a new
# address and every client has to be updated.
set -eu
cd "$(dirname "$0")"
HS=tor/data/hidden_service
[ -s "$HS/hs_ed25519_secret_key" ] || { echo "No keys in $HS (run ./init.sh first)" >&2; exit 1; }
DEST=${1:-$HOME}
OUT="$DEST/joinblaket-directory-onion-$(date +%Y%m%d).tar.gz"
umask 077
tar -czf "$OUT" -C tor/data hidden_service
echo "Saved $OUT"
echo "Address: $(cat "$HS/hostname"):5222"
echo "Restore on a new machine: clone the repo, then"
echo "  mkdir -p directory-xbt/tor/data && tar -xzf $(basename "$OUT") -C directory-xbt/tor/data"
echo "  chmod 700 directory-xbt/tor/data directory-xbt/tor/data/hidden_service && cd directory-xbt && ./init.sh"
