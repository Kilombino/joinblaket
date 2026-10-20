#!/usr/bin/env python3
"""Check this build's unified (0x21) signing against a Knots-BLAKE2b REGTEST node.

Funds a fresh P2WPKH key and a fresh fidelity-bond style P2WSH (CLTV freeze
script) from a regtest wallet, then spends both in one transaction signed by
jmwallet.wallet.signing with SIGHASH_UNIFIED|SIGHASH_ALL and asks the node
(testmempoolaccept) whether it is valid. Also checks that tampered
signatures (wrong spent amount committed, 0x21 byte swapped for 0x01) are
rejected. Refuses to run unless the node reports chain == "regtest".

Usage: regtest_unified_check.py http://cj:cj@127.0.0.1:18999 w
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.request
from hashlib import sha256
from urllib.parse import urlsplit

from bitcointx.core.key import CKey
from jmcore.bitcoin import (
    TxInput,
    TxOutput,
    create_p2wpkh_script_code,
    parse_transaction_bytes,
    pubkey_to_p2wpkh_script,
    scriptpubkey_to_address,
    serialize_transaction,
)
from jmcore.btc_script import mk_freeze_script
from jmwallet.wallet.signing import sign_p2wpkh_input, sign_p2wsh_input


class RPC:
    def __init__(self, url: str, wallet: str | None = None) -> None:
        u = urlsplit(url)
        self.base = f"{u.scheme}://{u.hostname}:{u.port}"
        self.auth = base64.b64encode(f"{u.username}:{u.password}".encode()).decode()
        self.wallet = wallet

    def __call__(self, method: str, *params: object, wallet: bool = False) -> object:
        url = self.base + (f"/wallet/{self.wallet}" if wallet and self.wallet else "")
        req = urllib.request.Request(
            url,
            data=json.dumps(
                {"jsonrpc": "1.0", "id": 1, "method": method, "params": params}
            ).encode(),
            headers={
                "Authorization": "Basic " + self.auth,
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            out = json.loads(r.read())
        if out.get("error"):
            raise RuntimeError(out["error"])
        return out["result"]


def main() -> int:
    rpc = RPC(sys.argv[1], sys.argv[2])
    info = rpc("getblockchaininfo")
    assert info["chain"] == "regtest", "refusing: not a regtest node"
    assert "difficulty_blake2b" in info, "not a BLAKE2b node / not post-activation"

    key = CKey(os.urandom(32))
    pub = bytes(key.pub)
    spk_wpkh = pubkey_to_p2wpkh_script(pub)
    locktime = info["mediantime"] - 86400  # already-expired timelock
    wscript = mk_freeze_script(pub.hex(), locktime)
    spk_wsh = b"\x00\x20" + sha256(wscript).digest()

    funded = []
    for spk, amount in ((spk_wpkh, 0.3), (spk_wsh, 0.2)):
        addr = scriptpubkey_to_address(spk, "regtest")
        txid = rpc("sendtoaddress", addr, amount, wallet=True)
        tx = rpc("getrawtransaction", txid, True)
        vout = next(o["n"] for o in tx["vout"] if o["scriptPubKey"]["hex"] == spk.hex())
        funded.append((txid, vout, round(amount * 1e8), spk))
    mine_to = rpc("getnewaddress", wallet=True)
    rpc("generatetoaddress", 1, mine_to)

    inputs = [TxInput.from_hex(t, v, sequence=0xFFFFFFFE) for t, v, _, _ in funded]
    dest = pubkey_to_p2wpkh_script(bytes(CKey(os.urandom(32)).pub))
    outputs = [TxOutput(value=sum(f[2] for f in funded) - 2000, script=dest)]
    tx = parse_transaction_bytes(serialize_transaction(2, inputs, outputs, locktime))
    spent = [(f[2], f[3]) for f in funded]

    sig0 = sign_p2wpkh_input(
        tx, 0, create_p2wpkh_script_code(pub), spent[0][0], key, spent_outputs=spent
    )
    sig1 = sign_p2wsh_input(tx, 1, wscript, spent[1][0], key, spent_outputs=spent)

    def accept(witnesses: list[list[bytes]]) -> dict:
        raw = serialize_transaction(2, inputs, outputs, locktime, witnesses).hex()
        return rpc("testmempoolaccept", [raw])[0]

    results = {}
    results["valid_0x21"] = accept([[sig0, pub], [sig1, wscript]])
    # Tampered: sign while committing to a wrong amount for the OTHER input.
    bad_spent = [spent[0], (spent[1][0] + 1, spent[1][1])]
    bad0 = sign_p2wpkh_input(
        tx, 0, create_p2wpkh_script_code(pub), spent[0][0], key, spent_outputs=bad_spent
    )
    results["wrong_other_amount"] = accept([[bad0, pub], [sig1, wscript]])
    # Same DER signature but hash type byte 0x01 (BIP-143 path) -> must fail.
    results["byte_0x01"] = accept([[sig0[:-1] + b"\x01", pub], [sig1, wscript]])
    print(
        json.dumps(
            {
                k: {"allowed": v["allowed"], "reject-reason": v.get("reject-reason")}
                for k, v in results.items()
            },
            indent=2,
        )
    )
    ok = (
        results["valid_0x21"]["allowed"]
        and not results["wrong_other_amount"]["allowed"]
        and not results["byte_0x01"]["allowed"]
    )
    if ok:
        raw = serialize_transaction(
            2, inputs, outputs, locktime, [[sig0, pub], [sig1, wscript]]
        )
        txid = rpc("sendrawtransaction", raw.hex())
        rpc("generatetoaddress", 1, mine_to)
        conf = rpc("getrawtransaction", txid, True)
        print("mined txid", txid, "confirmations", conf.get("confirmations"))
    print("RESULT", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
