"""Unified opt-in sighash (0x21) used by the XBT build for replay protection.

The vectors in ``data/unified_sighash_type01.tsv`` are the script-type-0 and
script-type-1 cases of Bitcoin Knots BLAKE2b's ``unified_sighash.json``
(v29.4.1), as carried by Kilowallet's ``UnifiedSighashTest``. Columns:
scriptCode, rawTx, inIdx, hashType, scriptType, spentOutputs(value:spk|...),
expected sighash (hex, as the node prints it).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bitcointx.core.key import CKey
from jmcore.bitcoin import (
    TxInput,
    TxOutput,
    create_p2wpkh_script_code,
    parse_transaction_bytes,
    pubkey_to_p2wpkh_script,
    serialize_transaction,
)

from jmwallet.wallet.signing import (
    SIGHASH_UNIFIED_ALL,
    TransactionSigningError,
    compute_sighash_unified,
    sign_p2wpkh_input,
    unified_sighash_message,
    verify_p2wpkh_signature,
)

VECTORS = Path(__file__).parent / "data" / "unified_sighash_type01.tsv"


def _read_varint(b: bytes, i: int) -> tuple[int, int]:
    n = b[i]
    if n < 0xFD:
        return n, i + 1
    size = {0xFD: 2, 0xFE: 4, 0xFF: 8}[n]
    return int.from_bytes(b[i + 1 : i + 1 + size], "little"), i + 1 + size


def _parse_legacy_tx(raw: bytes) -> tuple[int, list[tuple[bytes, int]], list, int]:
    """Minimal parser (vector txs carry arbitrary junk scripts)."""
    version = int.from_bytes(raw[0:4], "little")
    i = 4
    segwit = raw[4] == 0 and raw[5] == 1
    if segwit:
        i += 2
    n_in, i = _read_varint(raw, i)
    ins = []
    for _ in range(n_in):
        prevout = raw[i : i + 36]
        i += 36
        slen, i = _read_varint(raw, i)
        i += slen
        seq = int.from_bytes(raw[i : i + 4], "little")
        i += 4
        ins.append((prevout, seq))
    n_out, i = _read_varint(raw, i)
    outs = []
    for _ in range(n_out):
        value = int.from_bytes(raw[i : i + 8], "little")
        i += 8
        slen, i = _read_varint(raw, i)
        outs.append((value, raw[i : i + slen]))
        i += slen
    if segwit:
        for _ in range(n_in):
            n_items, i = _read_varint(raw, i)
            for _ in range(n_items):
                ilen, i = _read_varint(raw, i)
                i += ilen
    locktime = int.from_bytes(raw[i : i + 4], "little")
    assert i + 4 == len(raw)
    return version, ins, outs, locktime


def _vectors() -> list[list[str]]:
    rows = []
    for line in VECTORS.read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            rows.append(line.split("\t"))
    return rows


def test_vector_file_has_all_type01_cases() -> None:
    assert len(_vectors()) == 142


@pytest.mark.parametrize("row", _vectors(), ids=lambda r: r[6][:12])
def test_unified_sighash_matches_knots_vectors(row: list[str]) -> None:
    script_code, raw_tx, in_idx, hash_type, script_type, spent, expected = row
    version, ins, outs, locktime = _parse_legacy_tx(bytes.fromhex(raw_tx))
    spent_outputs = []
    for item in spent.split("|"):
        value, spk = item.split(":")
        spent_outputs.append((int(value), bytes.fromhex(spk)))
    digest = unified_sighash_message(
        version=version,
        locktime=locktime,
        hash_type=int(hash_type),
        script_type=int(script_type),
        prevouts=[p for p, _ in ins],
        amounts=[v for v, _ in spent_outputs],
        spent_scripts=[s for _, s in spent_outputs],
        sequences=[q for _, q in ins],
        outputs=outs,
        input_index=int(in_idx),
        script_code=bytes.fromhex(script_code),
    )
    # Raw digest bytes (not the reversed display order used for txids).
    assert digest.hex() == expected


def _sample_tx() -> tuple:
    key_a = CKey(bytes.fromhex("11" * 32))
    key_b = CKey(bytes.fromhex("22" * 32))
    pub_a, pub_b = bytes(key_a.pub), bytes(key_b.pub)
    inputs = [
        TxInput.from_hex("aa" * 32, 0, sequence=0xFFFFFFFD),
        TxInput.from_hex("bb" * 32, 3, sequence=0xFFFFFFFD),
    ]
    outputs = [TxOutput(value=150_000, script=pubkey_to_p2wpkh_script(pub_b))]
    tx = parse_transaction_bytes(serialize_transaction(2, inputs, outputs, 0))
    spent = [
        (100_000, pubkey_to_p2wpkh_script(pub_a)),
        (60_000, pubkey_to_p2wpkh_script(pub_b)),
    ]
    return tx, key_a, pub_a, spent


def test_sign_and_verify_unified_roundtrip() -> None:
    tx, key_a, pub_a, spent = _sample_tx()
    code = create_p2wpkh_script_code(pub_a)
    sig = sign_p2wpkh_input(tx, 0, code, 100_000, key_a, spent_outputs=spent)
    assert sig[-1] == SIGHASH_UNIFIED_ALL
    assert verify_p2wpkh_signature(tx, 0, code, 100_000, sig, pub_a, spent_outputs=spent)


def test_unified_signature_commits_to_other_inputs_amounts() -> None:
    """The point of the unified message: a lie about another input breaks it."""
    tx, key_a, pub_a, spent = _sample_tx()
    code = create_p2wpkh_script_code(pub_a)
    sig = sign_p2wpkh_input(tx, 0, code, 100_000, key_a, spent_outputs=spent)
    tampered = [spent[0], (spent[1][0] + 1, spent[1][1])]
    assert not verify_p2wpkh_signature(tx, 0, code, 100_000, sig, pub_a, spent_outputs=tampered)


def test_plain_sighash_all_is_rejected() -> None:
    tx, key_a, pub_a, spent = _sample_tx()
    code = create_p2wpkh_script_code(pub_a)
    with pytest.raises(TransactionSigningError, match="0x21"):
        sign_p2wpkh_input(tx, 0, code, 100_000, key_a, sighash_type=1, spent_outputs=spent)
    sig = sign_p2wpkh_input(tx, 0, code, 100_000, key_a, spent_outputs=spent)
    replayable = sig[:-1] + b"\x01"
    assert not verify_p2wpkh_signature(tx, 0, code, 100_000, replayable, pub_a, spent)


def test_signing_requires_every_spent_output() -> None:
    tx, key_a, pub_a, spent = _sample_tx()
    code = create_p2wpkh_script_code(pub_a)
    with pytest.raises(TransactionSigningError):
        sign_p2wpkh_input(tx, 0, code, 100_000, key_a)
    with pytest.raises(TransactionSigningError):
        compute_sighash_unified(tx, 0, code, spent[:1])
    with pytest.raises(TransactionSigningError, match="mismatch"):
        sign_p2wpkh_input(tx, 0, code, 99_999, key_a, spent_outputs=spent)
