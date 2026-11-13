#!/usr/bin/env python3
"""Deterministic test vectors for the JoinBlaket (XBT) taker protocol.

Every value below is produced by the real Python implementation (jmcore,
jmwallet, taker, maker): nothing is re-implemented here except the fixed
inputs. Randomness that the protocol normally draws is pinned explicitly:

* private keys / NaCl keys: SHA-256 of a fixed label;
* NaCl nonces: fixed 24-byte values passed to ``Box.encrypt`` (``encrypt_encode``
  draws a random nonce; the wire layout ``nonce || ciphertext`` is identical);
* input/output shuffle of ``build_coinjoin_tx``: ``taker.tx_builder.secure_random``
  is replaced by an identity shuffle while building, then restored;
* the fidelity-bond certificate key (``maker.fidelity._generate_certificate_private_key``)
  is pinned the same way;
* ``compute_tx_locktime`` gets a scripted RNG.

ECDSA signatures are deterministic in libsecp256k1 (RFC 6979, no low-R
grinding is requested by the code), and the PoDLE nonce is derived
deterministically by ``jmcore.podle`` itself.

Usage (from the repository root):

    PP=$(ls -d */src | paste -sd:)
    PYTHONPATH=$PP python scripts/xbt/taker_vectors.py > docs/xbt-taker-vectors.json

``tests/test_xbt_taker_vectors.py`` regenerates the vectors, compares them
with the committed JSON and re-verifies each one with the real code.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sys
from contextlib import contextmanager
from typing import Any

from bitcointx.core.key import CKey
from nacl import public

import jmwallet.wallet.signing as signing_mod
import maker.fidelity as fidelity_mod
import taker.tx_builder as tx_builder_mod
from jmcore import bitcoin as jb
from jmcore.bond_calc import calculate_timelocked_fidelity_bond_value
from jmcore.btc_script import derive_bond_address, mk_freeze_script
from jmcore.crypto import (
    NickIdentity,
    bitcoin_message_hash,
    bitcoin_message_hash_bytes,
    ecdsa_sign,
    ecdsa_verify,
    nick_from_pubkey_hex,
    verify_fidelity_bond_proof,
    verify_signed_privmsg,
)
from jmcore.directory_client import DirectoryClient
from jmcore.encryption import create_encryption_box, decode_decrypt, init_pubkey
from jmcore.fee_policy import estimate_p2wpkh_vsize
from jmcore.fee_quantization import (
    QUANT_ABS,
    QUANT_REL,
    quantize_abs_up,
    quantize_rel_up,
)
from jmcore.models import Offer, OfferType
from jmcore.network import ONION_HOSTID
from jmcore.nick_auth import (
    NickAuthMode,
    NickAuthProof,
    build_nick_auth_signed_message,
    handshake_line_sha256,
)
from jmcore.podle import (
    _podle_nonce_candidates,
    generate_nums_point,
    generate_podle,
    point_mult,
    scalar_mult_g,
    verify_podle,
    verify_podle_binding,
)
from jmcore.protocol import (
    FEATURE_NICK_AUTH,
    FeatureSet,
    MessageType,
    UTXOMetadata,
    create_handshake_response,
    format_utxo_list,
    parse_utxo_list,
)
from jmcore.transaction_policy import compute_tx_locktime
from jmwallet.wallet.signing import (
    SIGHASH_UNIFIED_ALL,
    compute_sighash_segwit,
    compute_sighash_unified,
    deserialize_transaction,
    sign_p2wpkh_input,
    verify_p2wpkh_signature,
)
from taker.coinjoin_session import CoinJoinSession
from taker.orderbook import (
    calculate_cj_fee,
    calculate_cj_fee_plan,
    is_fee_within_limits,
    is_quantized_cj_fee,
)
from taker.config import MaxCjFee
from taker.podle import ExtendedPoDLECommitment
from taker.tx_builder import CoinJoinTxBuilder, build_coinjoin_tx

VERSION = 1
NETWORK = "mainnet"  # XBT mainnet: bc1 addresses, wire network "xbt"
DIRECTORY = "f3tfzgk5ilwfapx427yx5ckeh7ptuvskfxnfnicfo747jyq5mgzdtvad.onion"
DIRECTORY_PORT = 5222


def h(label: str) -> bytes:
    """Fixed 32 bytes for a label (keys, txids, nonces)."""
    return hashlib.sha256(f"xbt-taker-vectors/{label}".encode()).digest()


def key(label: str) -> CKey:
    return CKey(h(label))


def pub(label: str) -> bytes:
    return bytes(key(label).pub)


def p2wpkh_spk(label: str) -> bytes:
    return jb.pubkey_to_p2wpkh_script(pub(label))


def addr(label: str) -> str:
    return jb.pubkey_to_p2wpkh_address(pub(label), NETWORK)


def nonce24(label: str) -> bytes:
    return h(label)[:24]


class _IdentityRandom:
    """Stand-in for secure_random in the tx builder: keeps insertion order."""

    @staticmethod
    def shuffle(seq: list[Any]) -> None:
        return None


class _ScriptedRng:
    def __init__(self, values: list[int]) -> None:
        self.values = list(values)

    def randint(self, start: int, end: int) -> int:
        value = self.values.pop(0)
        assert start <= value <= end
        return value


@contextmanager
def _patched(obj: Any, name: str, value: Any):
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, old)


class _FakeConnection:
    """Records what DirectoryClient sends and replays scripted directory lines."""

    def __init__(self, replies: list[bytes]) -> None:
        self.sent: list[str] = []
        self.replies = list(replies)

    async def send(self, data: bytes) -> None:
        self.sent.append(data.decode("utf-8"))

    async def receive(self) -> bytes:
        return self.replies.pop(0)

    def is_connected(self) -> bool:
        return True

    async def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# 1. Nick and message signing
# ---------------------------------------------------------------------------


def nick_vectors() -> dict[str, Any]:
    priv = h("taker-nick")
    ident = NickIdentity(5, priv)
    pub_hex = ident.public_key_hex
    data = "0 30000000 " + "11" * 32 + " P" + "22" * 32
    signed = ident.sign_message(data, ONION_HOSTID)
    sig_b64 = signed.rsplit(" ", 1)[1]
    maker = NickIdentity(5, h("maker1-nick"))
    line = f"{ident.nick}!{maker.nick}!fill {signed}"
    ok, command, parsed_data = verify_signed_privmsg(
        ident.nick, line.split("!", 2)[2], ONION_HOSTID
    )
    return {
        "_comment": (
            "nick = 'J5' + base58(sha256(ascii_hex(compressed_pubkey))[:10]) right-padded with 'O' "
            "to 14 chars. Privmsg signature: DER ECDSA (RFC6979, no low-R grinding) over "
            "bitcoin_message_hash(data + 'onion-network'), base64; appended as "
            "'<data> <pubkey_hex> <sig_b64>'. The command is NOT signed."
        ),
        "private_key": priv.hex(),
        "pubkey": pub_hex,
        "pubkey_hex_ascii_sha256": hashlib.sha256(pub_hex.encode()).hexdigest(),
        "nick": ident.nick,
        "nick_from_pubkey": nick_from_pubkey_hex(pub_hex),
        "maker_nick": maker.nick,
        "signed_data": data,
        "hostid": ONION_HOSTID,
        "message_hash": bitcoin_message_hash(data + ONION_HOSTID).hex(),
        "signature_b64": sig_b64,
        "signed_suffix": signed,
        "privmsg_line": line,
        "verify_signed_privmsg": {"ok": ok, "command": command, "data": parsed_data},
    }


# ---------------------------------------------------------------------------
# 2. Handshake + JMP-0005 nick authentication (driven through DirectoryClient)
# ---------------------------------------------------------------------------


def handshake_vectors() -> dict[str, Any]:
    ident = NickIdentity(5, h("taker-nick"))
    directory_id = f"{DIRECTORY}:{DIRECTORY_PORT}"
    challenge = h("nick-auth-challenge").hex()
    dir_features = FeatureSet(features={"peerlist_features", "ping", FEATURE_NICK_AUTH})
    dn_response = create_handshake_response(
        nick="J5DirectoryNickOO",
        network=NETWORK,
        accepted=True,
        motd="JoinMarket Directory Server",
        features=dir_features,
    )
    replies = [
        json.dumps(
            {"type": MessageType.DN_HANDSHAKE.value, "line": json.dumps(dn_response)}
        ).encode(),
        json.dumps(
            {
                "type": MessageType.NICK_AUTH_CHALLENGE.value,
                "line": json.dumps(
                    {"challenge": challenge, "directory-id": directory_id},
                    separators=(",", ":"),
                ),
            }
        ).encode(),
        json.dumps(
            {
                "type": MessageType.NICK_AUTH_RESULT.value,
                "line": '{"code":"ok","verified":true}',
            }
        ).encode(),
    ]
    client = DirectoryClient(
        host=DIRECTORY,
        port=DIRECTORY_PORT,
        network=NETWORK,
        nick_identity=ident,
        neutrino_compat=True,  # taker with a full-node backend advertises it
        nick_auth_mode=NickAuthMode.REQUIRE_VERIFIED,
        nick_auth_directory_id=directory_id,
    )
    conn = _FakeConnection(replies)
    client.connection = conn  # type: ignore[assignment]
    asyncio.run(client._handshake())
    handshake_envelope = conn.sent[0]
    handshake_line = json.loads(handshake_envelope)["line"]
    proof_envelope = conn.sent[1]
    proof_line = json.loads(proof_envelope)["line"]
    proof = NickAuthProof.parse(proof_line)
    signed_message = build_nick_auth_signed_message(
        challenge,
        directory_id,
        handshake_line_sha256(handshake_line),
        ident.nick,
        ident.public_key_hex,
    )
    return {
        "_comment": (
            "Taker -> directory. Each frame is one JSON object + '\\r\\n'. 'line' of the "
            "HANDSHAKE (793) is itself a JSON string; nick-auth hashes exactly that string "
            "(UTF-8) with SHA-256. Signed message = ascii('nick-auth|<challenge>|<directory-id>|"
            "<handshake-sha256>|<nick>|<pubkey>') + b'nick-auth-v1', hashed with the Bitcoin "
            "signed-message hash, signed by the nick key; signature = canonical base64 DER, low-S."
        ),
        "nick": ident.nick,
        "directory_id": directory_id,
        "handshake_envelope": handshake_envelope,
        "handshake_line": handshake_line,
        "handshake_line_sha256": handshake_line_sha256(handshake_line),
        "directory_reply_dn_handshake": replies[0].decode(),
        "directory_reply_challenge": replies[1].decode(),
        "nick_auth_signed_message_hex": signed_message.hex(),
        "nick_auth_message_hash": bitcoin_message_hash_bytes(signed_message).hex(),
        "nick_auth_proof_envelope": proof_envelope,
        "nick_auth_proof": proof.to_payload(),
        "directory_reply_result": replies[2].decode(),
        "authenticated": client.directory_nick_authenticated,
        "challenge": challenge,
        "orderbook_request_envelope": json.dumps(
            {"type": MessageType.PUBMSG.value, "line": f"{ident.nick}!PUBLIC!orderbook"}
        ),
    }


# ---------------------------------------------------------------------------
# 3. NaCl encryption (taker <-> maker)
# ---------------------------------------------------------------------------


def encryption_vectors() -> dict[str, Any]:
    taker_sk = public.PrivateKey(h("taker-nacl"))
    maker_sk = public.PrivateKey(h("maker1-nacl"))
    taker_pk_hex = bytes(taker_sk.public_key).hex()
    maker_pk_hex = bytes(maker_sk.public_key).hex()
    taker_box = create_encryption_box(taker_sk, init_pubkey(maker_pk_hex))
    maker_box = create_encryption_box(maker_sk, init_pubkey(taker_pk_hex))
    plaintext = "hello maker, this is the taker"
    nonce = nonce24("nonce-sample")
    wire = base64.b64encode(
        bytes(taker_box.encrypt(plaintext.encode(), nonce))
    ).decode()
    decrypted = decode_decrypt(wire, maker_box).decode()
    nonce2 = nonce24("nonce-sample-reply")
    reply_plain = "and this is the maker"
    reply_wire = base64.b64encode(
        bytes(maker_box.encrypt(reply_plain.encode(), nonce2))
    ).decode()
    return {
        "_comment": (
            "NaCl crypto_box (X25519 + HSalsa20 key, XSalsa20-Poly1305). NaCl private keys are "
            "raw 32-byte X25519 scalars (clamped by libsodium). Wire = base64(nonce24 || MAC16 || "
            "ciphertext); nonce is random per message in production. One taker keypair per "
            "CoinJoin round, shared by all makers (!fill carries its public key hex)."
        ),
        "taker_private": h("taker-nacl").hex(),
        "taker_public": taker_pk_hex,
        "maker_private": h("maker1-nacl").hex(),
        "maker_public": maker_pk_hex,
        "shared_key": taker_box.shared_key().hex(),
        "shared_key_maker_side": maker_box.shared_key().hex(),
        "plaintext": plaintext,
        "nonce": nonce.hex(),
        "wire_b64": wire,
        "decrypted_by_maker": decrypted,
        "reply_plaintext": reply_plain,
        "reply_nonce": nonce2.hex(),
        "reply_wire_b64": reply_wire,
    }


# ---------------------------------------------------------------------------
# 4. PoDLE
# ---------------------------------------------------------------------------


def podle_vectors() -> dict[str, Any]:
    priv = h("taker-utxo-key")
    txid = h("txid/taker-utxo")[::-1].hex()
    utxo = f"{txid}:1"
    nums = {str(i): bytes(generate_nums_point(i)).hex() for i in range(10)}
    proofs = []
    for index in range(3):
        c = generate_podle(priv, utxo, index)
        k = next(_podle_nonce_candidates(priv, utxo, index, c.p, c.p2))
        kg = bytes(scalar_mult_g(k))
        kj = bytes(point_mult(k, generate_nums_point(index)))
        ok, err = verify_podle(
            c.p, c.p2, c.sig, c.e, c.commitment, index_range=range(3)
        )
        proofs.append(
            {
                "index": index,
                "J": nums[str(index)],
                "P": c.p.hex(),
                "P2": c.p2.hex(),
                "commitment": c.commitment.hex(),
                "commitment_wire": c.to_commitment_str(),
                "nonce_k": f"{k:064x}",
                "KG": kg.hex(),
                "KJ": kj.hex(),
                "e": c.e.hex(),
                "s": c.sig.hex(),
                "verify": ok,
                "verify_error": err,
            }
        )
    c0 = generate_podle(priv, utxo, 0)
    spk = jb.pubkey_to_p2wpkh_script(c0.p).hex()
    ext = ExtendedPoDLECommitment(commitment=c0, scriptpubkey=spk, blockheight=961650)
    rev_legacy = ext.to_revelation(extended=False)
    rev_ext = ext.to_revelation(extended=True)
    fmt = lambda r: "|".join([r["utxo"], r["P"], r["P2"], r["sig"], r["e"]])  # noqa: E731
    return {
        "_comment": (
            "J_i = first valid point 02||sha256(G_ser || i || ctr) trying G compressed then G "
            "uncompressed, ctr 0..255. P = kG, P2 = kJ, commitment = sha256(P2) (wire: 'P'+hex). "
            "e = sha256(KG||KJ||P||P2) (all compressed), s = k_nonce + e*key mod n. The nonce is "
            "RFC6979-HMAC-SHA256(key, sha256('JoinMarket/PoDLE/nonce/v1' || be32(len(utxo)) || utxo "
            "|| u8(index) || P || P2)); any nonce verifies, this one makes vectors reproducible. "
            "Makers accept indices 0..taker_utxo_retries-1 (default 3)."
        ),
        "private_key": priv.hex(),
        "utxo": utxo,
        "nums_points": nums,
        "proofs": proofs,
        "p2wpkh_scriptpubkey": spk,
        "binding_ok": verify_podle_binding(c0.p, spk)[0],
        "revelation_legacy": fmt(rev_legacy),
        "revelation_extended": fmt(rev_ext),
    }


# ---------------------------------------------------------------------------
# 5. Fees
# ---------------------------------------------------------------------------


def _offer(
    nick: str, oid: int, ordertype: OfferType, cjfee: str | int, txfee: int = 0
) -> Offer:
    return Offer(
        counterparty=nick,
        oid=oid,
        ordertype=ordertype,
        minsize=27300,
        maxsize=10_000_000_000,
        txfee=txfee,
        cjfee=cjfee,
    )


def fee_vectors() -> dict[str, Any]:
    rel_cases = []
    for amount, rate in [
        (30_000_000, "0.0002"),
        (9_994_243, "0.000022"),
        (12_345, "0.0002"),  # 2.469 -> 2
        (12_500, "0.0002"),  # 2.5 -> 2 (half even)
        (17_500, "0.0002"),  # 3.5 -> 4 (half even)
        (100_000_000, "0.001"),
        (50_000_000, "1E-5"),
    ]:
        rel_cases.append(
            {
                "amount": amount,
                "cjfee": rate,
                "fee": jb.calculate_relative_fee(amount, rate),
            }
        )
    offers = [
        _offer("J5makerRelativeOO", 0, OfferType.SW0_RELATIVE, "0.0002", 0),
        _offer("J5makerAbsoluteOO", 1, OfferType.SW0_ABSOLUTE, "500", 150),
        _offer("J5makerOffGridOOO", 0, OfferType.SW0_RELATIVE, "0.00025", 0),
    ]
    cj_amount = 30_000_000
    per_offer = []
    for o in offers:
        per_offer.append(
            {
                "nick": o.counterparty,
                "ordertype": o.ordertype.value,
                "cjfee": str(o.cjfee),
                "txfee": o.txfee,
                "fee": calculate_cj_fee(o, cj_amount),
                "fee_rounded_up": (
                    calculate_cj_fee(o, cj_amount, True)
                    if (
                        quantize_rel_up(o.cjfee) is not None
                        or o.ordertype == OfferType.SW0_ABSOLUTE
                    )
                    else None
                ),
                "quantized": is_quantized_cj_fee(o),
                "within_default_limits": is_fee_within_limits(o, cj_amount, MaxCjFee()),
            }
        )
    plan = calculate_cj_fee_plan(offers[:2], cj_amount)
    plan_eq = calculate_cj_fee_plan(offers[:2], cj_amount, equalize_cj_fees=True)

    session = CoinJoinSession()
    session._fee_rate = 1.0
    session._randomized_fee_rate = 1.137
    shapes = []
    for n_in, n_out in [(2, 4), (3, 5), (5, 9)]:
        shapes.append(
            {
                "inputs": n_in,
                "outputs": n_out,
                "vsize_estimate": estimate_p2wpkh_vsize(n_in, n_out),
                "fee_at_randomized_rate": session._estimate_tx_fee(n_in, n_out),
                "fee_at_base_rate": session._estimate_tx_fee(
                    n_in, n_out, use_base_rate=True
                ),
            }
        )
    sweep = {
        "available": 50_000_000,
        "relative_fees": ["0.0002", "0.00005"],
        "cj_amount": jb.calculate_sweep_amount(50_000_000, ["0.0002", "0.00005"]),
    }
    return {
        "_comment": (
            "Relative fee = int((Decimal(cjfee) * Decimal(amount)).quantize(Decimal(1))) i.e. "
            "ROUND_HALF_EVEN; absolute fee = int(cjfee). Taker change = sum(taker inputs) - "
            "cj_amount - sum(maker fees) - tx_fee (no output if <= 2730). Maker change = "
            "sum(maker inputs) - cj_amount - offer.txfee + cjfee (maker dropped if < 27300). "
            "vsize estimate = 68*in + 31*out + 11; tx_fee = ceil(vsize * randomized_rate)."
        ),
        "relative_fee_cases": rel_cases,
        "cj_amount": cj_amount,
        "offers": per_offer,
        "fee_plan": plan,
        "fee_plan_equalized": plan_eq,
        "quant_rel_grid": [str(q) for q in QUANT_REL],
        "quant_abs_grid": list(QUANT_ABS),
        "quantize_abs_up_150": quantize_abs_up(150),
        "default_max_cj_fee": {
            "abs_fee": MaxCjFee().abs_fee,
            "rel_fee": MaxCjFee().rel_fee,
        },
        "fee_rate_base": 1.0,
        "fee_rate_randomized": 1.137,
        "shapes": shapes,
        "sweep": sweep,
    }


# ---------------------------------------------------------------------------
# 6/7. Unsigned transaction, unified sighash 0x21, signatures, final tx
# ---------------------------------------------------------------------------


def _capture_unified_preimage(
    tx: Any, index: int, script_code: bytes, spent: list
) -> tuple[bytes, bytes]:
    captured: list[bytes] = []
    real = signing_mod._tagged_hash

    def spy(tag: str, msg: bytes) -> bytes:
        captured.append(msg)
        return real(tag, msg)

    with _patched(signing_mod, "_tagged_hash", spy):
        digest = compute_sighash_unified(tx, index, script_code, spent)
    return captured[0], digest


def transaction_vectors() -> dict[str, Any]:
    cj_amount = 30_000_000
    taker_utxos = [
        {
            "txid": h("txid/taker-utxo")[::-1].hex(),
            "vout": 1,
            "value": 45_000_000,
            "scriptpubkey": p2wpkh_spk("taker-utxo-key").hex(),
            "_key": "taker-utxo-key",
        },
    ]
    maker1_utxos = [
        {
            "txid": h("txid/maker1-a")[::-1].hex(),
            "vout": 0,
            "value": 20_000_000,
            "scriptpubkey": p2wpkh_spk("maker1-utxo-a").hex(),
            "_key": "maker1-utxo-a",
        },
        {
            "txid": h("txid/maker1-b")[::-1].hex(),
            "vout": 3,
            "value": 15_000_000,
            "scriptpubkey": p2wpkh_spk("maker1-utxo-b").hex(),
            "_key": "maker1-utxo-b",
        },
    ]
    maker2_utxos = [
        {
            "txid": h("txid/maker2-a")[::-1].hex(),
            "vout": 2,
            "value": 31_000_000,
            "scriptpubkey": p2wpkh_spk("maker2-utxo-a").hex(),
            "_key": "maker2-utxo-a",
        },
    ]
    offer1 = _offer("J5maker1OOOOOOOO", 0, OfferType.SW0_RELATIVE, "0.0002", 0)
    offer2 = _offer("J5maker2OOOOOOOO", 0, OfferType.SW0_ABSOLUTE, "500", 150)
    plan = calculate_cj_fee_plan([offer1, offer2], cj_amount)

    session = CoinJoinSession()
    session._fee_rate = 1.0
    session._randomized_fee_rate = 1.137
    n_in = len(taker_utxos) + len(maker1_utxos) + len(maker2_utxos)
    n_out = 1 + 2 + 1 + 2
    tx_fee = session._estimate_tx_fee(n_in, n_out)

    locktime_cases = {
        "height_961700_rng_[3]": compute_tx_locktime(961700, _ScriptedRng([3])),
        "height_961700_rng_[0,37]": compute_tx_locktime(961700, _ScriptedRng([0, 37])),
        "height_0": compute_tx_locktime(0, _ScriptedRng([])),
    }
    locktime = locktime_cases["height_961700_rng_[3]"]

    def strip(us: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{k: v for k, v in u.items() if not k.startswith("_")} for u in us]

    maker_data = {
        offer1.counterparty: {
            "utxos": strip(maker1_utxos),
            "cj_addr": addr("maker1-cj"),
            "change_addr": addr("maker1-change"),
            "cjfee": plan[offer1.counterparty],
            "txfee": offer1.txfee,
        },
        offer2.counterparty: {
            "utxos": strip(maker2_utxos),
            "cj_addr": addr("maker2-cj"),
            "change_addr": addr("maker2-change"),
            "cjfee": plan[offer2.counterparty],
            "txfee": offer2.txfee,
        },
    }
    with _patched(tx_builder_mod, "secure_random", _IdentityRandom()):
        unsigned, metadata = build_coinjoin_tx(
            taker_utxos=strip(taker_utxos),
            taker_cj_address=addr("taker-cj"),
            taker_change_address=addr("taker-change"),
            taker_total_input=sum(u["value"] for u in taker_utxos),
            maker_data=maker_data,
            cj_amount=cj_amount,
            tx_fee=tx_fee,
            network=NETWORK,
            locktime=locktime,
        )
    tx = deserialize_transaction(unsigned)
    all_utxos = taker_utxos + maker1_utxos + maker2_utxos
    by_outpoint = {(u["txid"], u["vout"]): u for u in all_utxos}
    spent = []
    for inp in tx.inputs:
        u = by_outpoint[(inp.txid, inp.vout)]
        spent.append((u["value"], bytes.fromhex(u["scriptpubkey"])))

    inputs_out = []
    signatures: dict[str, list[dict[str, Any]]] = {}
    for idx, (inp, owner) in enumerate(
        zip(tx.inputs, metadata["input_owners"], strict=True)
    ):
        u = by_outpoint[(inp.txid, inp.vout)]
        k = key(u["_key"])
        pk = bytes(k.pub)
        script_code = jb.create_p2wpkh_script_code(pk)
        preimage, digest = _capture_unified_preimage(tx, idx, script_code, spent)
        sig = sign_p2wpkh_input(
            tx, idx, script_code, u["value"], k, SIGHASH_UNIFIED_ALL, spent
        )
        bip143 = compute_sighash_segwit(tx, idx, script_code, u["value"], 0x01)
        sig_01 = sig[:-1] + b"\x01"
        inputs_out.append(
            {
                "index": idx,
                "owner": owner,
                "txid": inp.txid,
                "vout": inp.vout,
                "value": u["value"],
                "scriptpubkey": u["scriptpubkey"],
                "private_key": h(u["_key"]).hex(),
                "pubkey": pk.hex(),
                "script_code": script_code.hex(),
                "unified_preimage": preimage.hex(),
                "unified_sighash": digest.hex(),
                "bip143_sighash_0x01_for_contrast": bip143.hex(),
                "signature": sig.hex(),
                "witness": [sig.hex(), pk.hex()],
                "verify_0x21": verify_p2wpkh_signature(
                    tx, idx, script_code, u["value"], sig, pk, spent
                ),
                "verify_same_der_with_0x01_byte": verify_p2wpkh_signature(
                    tx, idx, script_code, u["value"], sig_01, pk, spent
                ),
                "sig_wire_b64": base64.b64encode(
                    bytes([len(sig)]) + sig + bytes([len(pk)]) + pk
                ).decode(),
            }
        )
        signatures.setdefault(owner, []).append(
            {"txid": inp.txid, "vout": inp.vout, "witness": [sig.hex(), pk.hex()]}
        )
    builder = CoinJoinTxBuilder(NETWORK)
    signed = builder.add_signatures(unsigned, signatures, metadata)

    return {
        "_comment": (
            "build_coinjoin_tx with the shuffle disabled (production shuffles inputs and outputs "
            "with a CSPRNG; order here = taker inputs, maker inputs by nick insertion order; "
            "outputs = taker cj, maker cj..., taker change, maker change...). version 2, no "
            "witness in the unsigned serialization, nSequence 0xfffffffe when nLockTime != 0 "
            "(0xffffffff when 0). Unified preimage = 0x00 epoch || hashtype || version LE4 || "
            "locktime LE5 || sha256(prevouts) || sha256(amounts LE8) || sha256(compactsize||spk "
            "for each spent output) || sha256(sequences LE4) || sha256(outputs) || 0x01 script "
            "type || input index LE4 || compactsize||scriptCode; sighash = TaggedHash"
            "('UnifiedSighash', preimage). Signature = DER || 0x21. Witness = [sig, pubkey]."
        ),
        "network": NETWORK,
        "cj_amount": cj_amount,
        "fee_rate_randomized": 1.137,
        "tx_fee": tx_fee,
        "maker_fee_plan": plan,
        "offers": [
            {
                "nick": offer1.counterparty,
                "ordertype": offer1.ordertype.value,
                "cjfee": offer1.cjfee,
                "txfee": offer1.txfee,
            },
            {
                "nick": offer2.counterparty,
                "ordertype": offer2.ordertype.value,
                "cjfee": offer2.cjfee,
                "txfee": offer2.txfee,
            },
        ],
        "taker": {
            "utxos": strip(taker_utxos),
            "cj_address": addr("taker-cj"),
            "change_address": addr("taker-change"),
        },
        "makers": {
            nick: {k: v for k, v in d.items()} for nick, d in maker_data.items()
        },
        "locktime_cases": locktime_cases,
        "locktime": locktime,
        "unsigned_tx": unsigned.hex(),
        "unsigned_tx_b64": base64.b64encode(unsigned).decode(),
        "metadata": {
            "input_owners": metadata["input_owners"],
            "output_owners": [list(o) for o in metadata["output_owners"]],
        },
        "outputs": [
            {"value": o.value, "scriptpubkey": o.script.hex()} for o in tx.outputs
        ],
        "inputs": inputs_out,
        "signed_tx": signed.hex(),
        "txid": builder.get_txid(signed),
    }


# ---------------------------------------------------------------------------
# 8. Offers and fidelity bond proof
# ---------------------------------------------------------------------------


def offer_vectors() -> dict[str, Any]:
    taker = NickIdentity(5, h("taker-nick"))
    maker = NickIdentity(5, h("maker1-nick"))
    bond_key = key("maker1-bond-key")
    bond_pub = bytes(bond_key.pub)
    bond_txid = h("txid/maker1-bond")[::-1].hex()
    locktime = 1_798_761_600  # 2027-01-01T00:00:00Z
    bond = fidelity_mod.FidelityBondInfo(
        txid=bond_txid,
        vout=0,
        value=100_000_000,
        locktime=locktime,
        confirmation_time=1_760_000_000,
        bond_value=0,
        pubkey=bond_pub,
        private_key=bond_key,
    )
    with _patched(
        fidelity_mod,
        "_generate_certificate_private_key",
        lambda: key("maker1-cert-key"),
    ):
        proof = fidelity_mod.create_fidelity_bond_proof(
            bond, maker.nick, taker.nick, 961700
        )
    assert proof is not None
    offer_data = f"0 27300 2500000000 0 0.0002!tbond {proof}"
    signed = maker.sign_message(offer_data, ONION_HOSTID)
    line = f"{maker.nick}!{taker.nick}!sw0reloffer {signed}"
    client = DirectoryClient.__new__(DirectoryClient)
    client.peer_features = {}
    parsed = client._parse_offer_from_message(
        line.split("!", 2)[2], maker.nick, taker.nick, MessageType.PRIVMSG.value
    )
    assert parsed is not None
    offer, bond_data, _ = parsed
    ok, verified, err = verify_fidelity_bond_proof(proof, maker.nick, taker.nick)
    raw = base64.b64decode(proof)
    bond_addr = derive_bond_address(bond_pub, locktime, NETWORK)
    value_now = calculate_timelocked_fidelity_bond_value(
        utxo_value=100_000_000,
        confirmation_time=1_760_000_000,
        locktime=locktime,
        current_time=1_763_000_000,
    )
    return {
        "_comment": (
            "Maker answers '!orderbook' (PUBMSG '<taker>!PUBLIC!orderbook') with one PRIVMSG per "
            "offer: '<maker>!<taker>!sw0reloffer <oid> <minsize> <maxsize> <txfee> <cjfee>"
            "[!tbond <proof_b64>] <pubkey> <sig>'. Proof = struct '<72s72s33sH33s32sII' (252 bytes): "
            "nick_sig (0xff left-padded DER, cert key over '<taker>|<maker>'), cert_sig (0xff "
            "left-padded DER, utxo key over 'fidelity-bond-cert|'+cert_pub+'|'+str(expiry), or "
            "the hex-cert_pub variant), cert_pub, cert_expiry (uint16 LE, periods of 2016 "
            "blocks), utxo_pub, txid (32 bytes, display/big-endian order), vout LE, locktime LE. "
            "Both signatures use the Bitcoin signed-message hash."
        ),
        "maker_nick": maker.nick,
        "taker_nick": taker.nick,
        "bond_private_key": h("maker1-bond-key").hex(),
        "cert_private_key": h("maker1-cert-key").hex(),
        "block_height_for_cert": 961700,
        "proof_b64": proof,
        "proof_fields": {
            "nick_sig_padded": raw[0:72].hex(),
            "cert_sig_padded": raw[72:144].hex(),
            "cert_pub": raw[144:177].hex(),
            "cert_expiry_encoded": int.from_bytes(raw[177:179], "little"),
            "utxo_pub": raw[179:212].hex(),
            "txid_display": raw[212:244].hex(),
            "vout": int.from_bytes(raw[244:248], "little"),
            "locktime": int.from_bytes(raw[248:252], "little"),
        },
        "privmsg_line": line,
        "parsed_offer": {
            "counterparty": offer.counterparty,
            "oid": offer.oid,
            "ordertype": offer.ordertype.value,
            "minsize": offer.minsize,
            "maxsize": offer.maxsize,
            "txfee": offer.txfee,
            "cjfee": str(offer.cjfee),
        },
        "parsed_bond": {k: v for k, v in (bond_data or {}).items() if k != "proof"},
        "verify_fidelity_bond_proof": {
            "ok": ok,
            "error": err,
            "cert_expiry_height": (verified or {}).get("cert_expiry"),
        },
        "verify_with_wrong_taker_nick": verify_fidelity_bond_proof(
            proof, maker.nick, maker.nick
        )[0],
        "freeze_script": mk_freeze_script(bond_pub.hex(), locktime).hex(),
        "bond_p2wsh_scriptpubkey": bond_addr.scriptpubkey.hex(),
        "bond_address": bond_addr.address,
        "bond_value_inputs": {
            "utxo_value": 100_000_000,
            "confirmation_time": 1_760_000_000,
            "locktime": locktime,
            "current_time": 1_763_000_000,
            "interest_rate": 0.015,
            "exponent": 1.3,
        },
        "bond_value": value_now,
    }


# ---------------------------------------------------------------------------
# 9. Session messages (exact wire lines, fixed keys/nonces)
# ---------------------------------------------------------------------------


def session_vectors(
    tx_vec: dict[str, Any], podle_vec: dict[str, Any]
) -> dict[str, Any]:
    taker = NickIdentity(5, h("taker-nick"))
    maker = NickIdentity(5, h("maker1-nick"))
    taker_sk = public.PrivateKey(h("taker-nacl"))
    maker_sk = public.PrivateKey(h("maker1-nacl"))
    t_pk = bytes(taker_sk.public_key).hex()
    m_pk = bytes(maker_sk.public_key).hex()
    t_box = create_encryption_box(taker_sk, init_pubkey(m_pk))
    m_box = create_encryption_box(maker_sk, init_pubkey(t_pk))

    def enc(box: Any, text: str, label: str) -> str:
        return base64.b64encode(
            bytes(box.encrypt(text.encode(), nonce24(label)))
        ).decode()

    sent: list[str] = []

    client = DirectoryClient(
        host=DIRECTORY, port=DIRECTORY_PORT, network=NETWORK, nick_identity=taker
    )
    conn = _FakeConnection([])
    client.connection = conn  # type: ignore[assignment]

    def send(cmd: str, data: str) -> str:
        asyncio.run(client.send_private_message(maker.nick, cmd, data))
        sent.append(conn.sent[-1])
        return conn.sent[-1]

    def maker_line(cmd: str, data: str) -> str:
        return json.dumps(
            {
                "type": MessageType.PRIVMSG.value,
                "line": f"{maker.nick}!{taker.nick}!{cmd} {maker.sign_message(data, ONION_HOSTID)}",
            }
        )

    cj_amount = tx_vec["cj_amount"]
    commitment_wire = podle_vec["proofs"][0]["commitment_wire"]
    fill_data = f"0 {cj_amount} {t_pk} {commitment_wire}"
    fill = send("fill", fill_data)
    pubkey_data = f"{m_pk} features=neutrino_compat"
    pubkey_msg = maker_line("pubkey", pubkey_data)

    revelation = podle_vec["revelation_extended"]
    auth_ct = enc(t_box, revelation, "nonce-auth")
    auth = send("auth", auth_ct)

    m1 = tx_vec["makers"]["J5maker1OOOOOOOO"]
    utxo_list = format_utxo_list(
        [
            UTXOMetadata(
                txid=u["txid"],
                vout=u["vout"],
                scriptpubkey=u["scriptpubkey"],
                blockheight=961600,
            )
            for u in m1["utxos"]
        ],
        extended=True,
    )
    auth_pub = pub("maker1-utxo-a").hex()
    btc_sig = ecdsa_sign(m_pk, h("maker1-utxo-a"))
    ioauth_plain = " ".join(
        [utxo_list, auth_pub, m1["cj_addr"], m1["change_addr"], btc_sig]
    )
    ioauth_ct = enc(m_box, ioauth_plain, "nonce-ioauth")
    ioauth_msg = maker_line("ioauth", ioauth_ct)

    tx_b64 = tx_vec["unsigned_tx_b64"]
    tx_ct = enc(t_box, tx_b64, "nonce-tx")
    tx_msg = send("tx", tx_ct)

    sig_msgs = []
    for i, inp in enumerate(
        x for x in tx_vec["inputs"] if x["owner"] == "J5maker1OOOOOOOO"
    ):
        ct = enc(m_box, inp["sig_wire_b64"], f"nonce-sig-{i}")
        sig_msgs.append(
            {
                "plaintext": inp["sig_wire_b64"],
                "ciphertext": ct,
                "line": maker_line("sig", ct),
            }
        )

    push = send("push", base64.b64encode(bytes.fromhex(tx_vec["signed_tx"])).decode())
    error_msg = maker_line(
        "error", "Transaction verification failed: CJ output value too low"
    )
    return {
        "_comment": (
            "Exact PRIVMSG frames (JSON envelope type 685) for one maker, with fixed NaCl keys and "
            "nonces. Plaintext commands: fill, pubkey, push, error. Encrypted (base64 NaCl): auth, "
            "ioauth, tx, sig. The maker sends one !sig per maker input."
        ),
        "taker_nick": taker.nick,
        "maker_nick": maker.nick,
        "fill": {"data": fill_data, "frame": fill},
        "pubkey": {"data": pubkey_data, "frame": pubkey_msg},
        "auth": {
            "plaintext": revelation,
            "nonce": nonce24("nonce-auth").hex(),
            "ciphertext": auth_ct,
            "frame": auth,
        },
        "ioauth": {
            "plaintext": ioauth_plain,
            "utxo_list": utxo_list,
            "auth_pub": auth_pub,
            "btc_sig": btc_sig,
            "btc_sig_valid": ecdsa_verify(m_pk, btc_sig, bytes.fromhex(auth_pub)),
            "parsed_utxos": [u.to_extended_str() for u in parse_utxo_list(utxo_list)],
            "ciphertext": ioauth_ct,
            "frame": ioauth_msg,
        },
        "tx": {
            "plaintext": tx_b64,
            "nonce": nonce24("nonce-tx").hex(),
            "ciphertext": tx_ct,
            "frame": tx_msg,
        },
        "sig": sig_msgs,
        "push": {"frame": push},
        "error": {"frame": error_msg},
    }


def build() -> dict[str, Any]:
    tx_vec = transaction_vectors()
    podle_vec = podle_vectors()
    return {
        "version": VERSION,
        "source": "JoinBlaket v0.43.0-xbt4 (scripts/xbt/taker_vectors.py)",
        "nick": nick_vectors(),
        "handshake": handshake_vectors(),
        "encryption": encryption_vectors(),
        "podle": podle_vec,
        "fees": fee_vectors(),
        "transaction": tx_vec,
        "offer": offer_vectors(),
        "session": session_vectors(tx_vec, podle_vec),
    }


def dumps(vectors: dict[str, Any]) -> str:
    return json.dumps(vectors, indent=2, sort_keys=False, ensure_ascii=False) + "\n"


def main() -> int:
    from loguru import logger

    logger.remove()  # keep stdout/stderr clean; the library logs at DEBUG
    sys.stdout.write(dumps(build()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
