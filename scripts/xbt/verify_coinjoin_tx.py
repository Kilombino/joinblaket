#!/usr/bin/env python3
"""Verify a (regtest) XBT CoinJoin: every input signed with 0x21, confirmed, equal outputs.

Usage: verify_coinjoin_tx.py http://user:pass@127.0.0.1:18999 <txid> <cj_amount_sats>
"""

from __future__ import annotations

import base64
import json
import sys
import urllib.request
from collections import Counter
from urllib.parse import urlsplit


def rpc(url: str, method: str, *params: object) -> object:
    u = urlsplit(url)
    req = urllib.request.Request(
        f"{u.scheme}://{u.hostname}:{u.port}",
        data=json.dumps(
            {"jsonrpc": "1.0", "id": 1, "method": method, "params": params}
        ).encode(),
        headers={
            "Authorization": "Basic "
            + base64.b64encode(f"{u.username}:{u.password}".encode()).decode()
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        out = json.loads(r.read())
    if out.get("error"):
        raise RuntimeError(out["error"])
    return out["result"]


def main() -> int:
    url, txid, cj_amount = sys.argv[1], sys.argv[2], int(sys.argv[3])
    assert rpc(url, "getblockchaininfo")["chain"] == "regtest", "refusing: not regtest"
    tx = rpc(url, "getrawtransaction", txid, True)
    ok = True
    print(
        f"txid {txid}  confirmations={tx.get('confirmations', 0)}  vsize={tx['vsize']}"
    )
    for i, vin in enumerate(tx["vin"]):
        wit = vin.get("txinwitness", [])
        sig = wit[0] if wit else ""
        last = sig[-2:]
        good = len(wit) == 2 and last == "21"
        ok &= good
        print(
            f"  in {i}: {vin['txid']}:{vin['vout']} witness_items={len(wit)} "
            f"sighash_byte=0x{last} {'OK' if good else 'BAD'}"
        )
    values = [round(o["value"] * 1e8) for o in tx["vout"]]
    for o in tx["vout"]:
        print(
            f"  out {o['n']}: {round(o['value'] * 1e8)} sats {o['scriptPubKey']['type']}"
        )
    equal = Counter(values)[cj_amount]
    print(f"  equal-amount outputs of {cj_amount}: {equal} (inputs: {len(tx['vin'])})")
    ok &= equal == len(tx["vin"]) or equal >= 2
    ok &= tx.get("confirmations", 0) >= 1
    print("RESULT", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
