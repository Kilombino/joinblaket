"""XBT taker protocol vectors (docs/xbt-taker-vectors.json).

1. Regenerating the vectors with scripts/xbt/taker_vectors.py gives exactly the
   committed JSON (so a Kotlin port checked against the file is checked against
   the Python code as it is now).
2. Each vector verifies with the real implementation, reading only the JSON
   (independent of the generator's internals).
"""

from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
VECTORS = ROOT / "docs" / "xbt-taker-vectors.json"
GENERATOR = ROOT / "scripts" / "xbt" / "taker_vectors.py"


def _load_generator():
    spec = importlib.util.spec_from_file_location("xbt_taker_vectors", GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def vectors() -> dict:
    return json.loads(VECTORS.read_text(encoding="utf-8"))


def test_regenerated_vectors_match_committed_file() -> None:
    gen = _load_generator()
    assert gen.dumps(gen.build()) == VECTORS.read_text(encoding="utf-8")


def test_nick_and_privmsg_signature(vectors: dict) -> None:
    from jmcore.crypto import NickIdentity, nick_from_pubkey_hex, verify_signed_privmsg

    v = vectors["nick"]
    ident = NickIdentity(5, bytes.fromhex(v["private_key"]))
    assert ident.public_key_hex == v["pubkey"]
    assert ident.nick == v["nick"] == nick_from_pubkey_hex(v["pubkey"])
    assert ident.sign_message(v["signed_data"], v["hostid"]) == v["signed_suffix"]
    from_nick, _to, rest = v["privmsg_line"].split("!", 2)
    ok, command, data = verify_signed_privmsg(from_nick, rest, v["hostid"])
    assert (ok, command, data) == (True, "fill", v["signed_data"])
    # Signing over a different hostid must not verify.
    assert not verify_signed_privmsg(from_nick, rest, "other-host")[0]


def test_handshake_and_nick_auth(vectors: dict) -> None:
    from jmcore.nick_auth import NickAuthProof, verify_nick_auth_proof

    v = vectors["handshake"]
    envelope = json.loads(v["handshake_envelope"])
    assert envelope["type"] == 793 and envelope["line"] == v["handshake_line"]
    hs = json.loads(v["handshake_line"])
    assert hs["network"] == "xbt" and hs["proto-ver"] == 5 and hs["directory"] is False
    assert hs["features"]["nick_auth"] is True
    proof = NickAuthProof.from_payload(v["nick_auth_proof"])
    assert verify_nick_auth_proof(
        proof, v["challenge"], v["directory_id"], v["handshake_line"], v["nick"], 5
    )
    # The proof is bound to the exact handshake line and directory identity.
    assert not verify_nick_auth_proof(
        proof,
        v["challenge"],
        v["directory_id"],
        v["handshake_line"] + " ",
        v["nick"],
        5,
    )
    assert not verify_nick_auth_proof(
        proof, v["challenge"], "other.onion:5222", v["handshake_line"], v["nick"], 5
    )
    assert v["authenticated"] is True


def test_nacl_encryption(vectors: dict) -> None:
    from nacl import public

    from jmcore.encryption import create_encryption_box, decode_decrypt, init_pubkey

    v = vectors["encryption"]
    taker = public.PrivateKey(bytes.fromhex(v["taker_private"]))
    maker = public.PrivateKey(bytes.fromhex(v["maker_private"]))
    assert bytes(taker.public_key).hex() == v["taker_public"]
    assert bytes(maker.public_key).hex() == v["maker_public"]
    taker_box = create_encryption_box(taker, init_pubkey(v["maker_public"]))
    maker_box = create_encryption_box(maker, init_pubkey(v["taker_public"]))
    assert (
        taker_box.shared_key().hex() == v["shared_key"] == maker_box.shared_key().hex()
    )
    assert decode_decrypt(v["wire_b64"], maker_box).decode() == v["plaintext"]
    assert (
        decode_decrypt(v["reply_wire_b64"], taker_box).decode() == v["reply_plaintext"]
    )
    assert base64.b64decode(v["wire_b64"])[:24].hex() == v["nonce"]


def test_podle(vectors: dict) -> None:
    from jmcore.podle import generate_nums_point, verify_podle, verify_podle_binding

    v = vectors["podle"]
    for index, point in v["nums_points"].items():
        assert bytes(generate_nums_point(int(index))).hex() == point
    for proof in v["proofs"]:
        ok, err = verify_podle(
            bytes.fromhex(proof["P"]),
            bytes.fromhex(proof["P2"]),
            bytes.fromhex(proof["s"]),
            bytes.fromhex(proof["e"]),
            bytes.fromhex(proof["commitment"]),
            index_range=range(3),
        )
        assert ok, err
        assert proof["commitment_wire"] == "P" + proof["commitment"]
    p0 = bytes.fromhex(v["proofs"][0]["P"])
    assert verify_podle_binding(p0, v["p2wpkh_scriptpubkey"])[0]
    assert v["revelation_extended"].split("|")[0].count(":") == 3
    assert v["revelation_legacy"].split("|")[0] == v["utxo"]


def test_fees(vectors: dict) -> None:
    from jmcore.bitcoin import calculate_relative_fee, calculate_sweep_amount
    from jmcore.fee_policy import estimate_p2wpkh_vsize

    v = vectors["fees"]
    for case in v["relative_fee_cases"]:
        assert calculate_relative_fee(case["amount"], case["cjfee"]) == case["fee"]
    for shape in v["shapes"]:
        assert (
            estimate_p2wpkh_vsize(shape["inputs"], shape["outputs"])
            == shape["vsize_estimate"]
        )
    s = v["sweep"]
    assert calculate_sweep_amount(s["available"], s["relative_fees"]) == s["cj_amount"]


def test_transaction_and_unified_sighash(vectors: dict) -> None:
    from jmcore.bitcoin import create_p2wpkh_script_code, pubkey_to_p2wpkh_script
    from jmwallet.wallet.signing import (
        compute_sighash_unified,
        deserialize_transaction,
        verify_p2wpkh_signature,
    )
    from maker.tx_verification import verify_unsigned_transaction
    from jmcore.models import NetworkType, OfferType
    from taker.tx_builder import CoinJoinTxBuilder

    v = vectors["transaction"]
    unsigned = bytes.fromhex(v["unsigned_tx"])
    tx = deserialize_transaction(unsigned)
    assert tx.version == 2 and tx.locktime == v["locktime"]
    assert all(i.sequence == 0xFFFFFFFE for i in tx.inputs)
    spent = [(i["value"], bytes.fromhex(i["scriptpubkey"])) for i in v["inputs"]]
    for i in v["inputs"]:
        pk = bytes.fromhex(i["pubkey"])
        assert pubkey_to_p2wpkh_script(pk).hex() == i["scriptpubkey"]
        script_code = create_p2wpkh_script_code(pk)
        assert (
            compute_sighash_unified(tx, i["index"], script_code, spent).hex()
            == (i["unified_sighash"])
        )
        sig = bytes.fromhex(i["signature"])
        assert sig[-1] == 0x21
        assert verify_p2wpkh_signature(
            tx, i["index"], script_code, i["value"], sig, pk, spent
        )
        assert not verify_p2wpkh_signature(
            tx, i["index"], script_code, i["value"], sig[:-1] + b"\x01", pk, spent
        )
        # The unified sighash commits to every input's amount.
        wrong = list(spent)
        other = (i["index"] + 1) % len(spent)
        wrong[other] = (wrong[other][0] + 1, wrong[other][1])
        assert not verify_p2wpkh_signature(
            tx, i["index"], script_code, i["value"], sig, pk, wrong
        )
    signed = bytes.fromhex(v["signed_tx"])
    assert CoinJoinTxBuilder("mainnet").get_txid(signed) == v["txid"]
    assert CoinJoinTxBuilder("mainnet").get_txid(unsigned) == v["txid"]

    # Each maker would accept the unsigned transaction (maker-side check).
    offers = {o["nick"]: o for o in v["offers"]}
    for nick, data in v["makers"].items():
        our_utxos = {
            (u["txid"], u["vout"]): SimpleNamespace(value=u["value"])
            for u in data["utxos"]
        }
        ok, err = verify_unsigned_transaction(
            tx_hex=unsigned.hex(),
            our_utxos=our_utxos,  # type: ignore[arg-type]
            cj_address=data["cj_addr"],
            change_address=data["change_addr"],
            amount=v["cj_amount"],
            cjfee=offers[nick]["cjfee"],
            txfee=offers[nick]["txfee"],
            offer_type=OfferType(offers[nick]["ordertype"]),
            network=NetworkType.MAINNET,
        )
        assert ok, err

    # Amounts: taker pays cj_amount + maker fees + tx_fee; outputs + fee = inputs.
    total_in = sum(i["value"] for i in v["inputs"])
    total_out = sum(o["value"] for o in v["outputs"])
    maker_txfees = sum(o["txfee"] for o in v["offers"])
    assert total_in - total_out == v["tx_fee"] + maker_txfees


def test_offer_and_fidelity_bond(vectors: dict) -> None:
    from jmcore.crypto import verify_fidelity_bond_proof, verify_signed_privmsg
    from jmcore.directory_client import parse_fidelity_bond_proof

    v = vectors["offer"]
    ok, data, err = verify_fidelity_bond_proof(
        v["proof_b64"], v["maker_nick"], v["taker_nick"]
    )
    assert ok, err
    assert (
        data is not None
        and data["cert_expiry"] == v["verify_fidelity_bond_proof"]["cert_expiry_height"]
    )
    assert not verify_fidelity_bond_proof(
        v["proof_b64"], v["maker_nick"], v["maker_nick"]
    )[0]
    parsed = parse_fidelity_bond_proof(v["proof_b64"], v["maker_nick"], v["taker_nick"])
    assert parsed is not None
    assert parsed["p2wsh_script"] == v["bond_p2wsh_scriptpubkey"]
    assert parsed["redeem_script"] == v["freeze_script"]
    from_nick, _to, rest = v["privmsg_line"].split("!", 2)
    assert verify_signed_privmsg(from_nick, rest, "onion-network")[0]


def test_session_frames(vectors: dict) -> None:
    from nacl import public

    from jmcore.crypto import verify_signed_privmsg
    from jmcore.encryption import create_encryption_box, decode_decrypt, init_pubkey

    enc = vectors["encryption"]
    v = vectors["session"]
    taker_box = create_encryption_box(
        public.PrivateKey(bytes.fromhex(enc["taker_private"])),
        init_pubkey(enc["maker_public"]),
    )
    maker_box = create_encryption_box(
        public.PrivateKey(bytes.fromhex(enc["maker_private"])),
        init_pubkey(enc["taker_public"]),
    )
    frames = [
        ("fill", v["fill"]["frame"]),
        ("pubkey", v["pubkey"]["frame"]),
        ("auth", v["auth"]["frame"]),
        ("ioauth", v["ioauth"]["frame"]),
        ("tx", v["tx"]["frame"]),
        ("push", v["push"]["frame"]),
        ("error", v["error"]["frame"]),
    ] + [("sig", s["line"]) for s in v["sig"]]
    for command, frame in frames:
        envelope = json.loads(frame)
        assert envelope["type"] == 685
        from_nick, _to, rest = envelope["line"].split("!", 2)
        ok, cmd, _data = verify_signed_privmsg(from_nick, rest, "onion-network")
        assert ok and cmd == command
    assert (
        decode_decrypt(v["auth"]["ciphertext"], maker_box).decode()
        == v["auth"]["plaintext"]
    )
    assert (
        decode_decrypt(v["ioauth"]["ciphertext"], taker_box).decode()
        == v["ioauth"]["plaintext"]
    )
    assert (
        decode_decrypt(v["tx"]["ciphertext"], maker_box).decode()
        == v["tx"]["plaintext"]
    )
    for s in v["sig"]:
        assert decode_decrypt(s["ciphertext"], taker_box).decode() == s["plaintext"]
    assert v["ioauth"]["btc_sig_valid"] is True
