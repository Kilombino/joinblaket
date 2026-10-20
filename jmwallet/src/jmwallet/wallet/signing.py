"""
Bitcoin transaction signing utilities for P2WPKH and P2WSH inputs.

BIP-143 sighash computation delegates to ``python-bitcointx``'s
``SignatureHash`` so consensus-critical preimage construction lives in
an audited library. The transaction is serialized via the in-tree
``ParsedTransaction`` and handed to bitcointx's ``CTransaction``.

XBT fork: this build only ever signs with the opt-in *unified* sighash
(hash type ``0x21`` = ``SIGHASH_UNIFIED | SIGHASH_ALL``) defined by Bitcoin
Knots BLAKE2b (``doc/unified-sighash.md``, Knots v29.4.1). A signature with
that hash type is only valid on the BLAKE2b chain, so a CoinJoin signed here
cannot be replayed onto the shared-history SHA-256 chain. The unified message
commits to every input's outpoint, amount, scriptPubKey and sequence, so
signers need the full list of spent outputs, not just their own input's value.
bitcointx does not know this message, so it is built in-tree below and checked
byte for byte against the Knots ``unified_sighash.json`` vectors and a regtest
node (see ``tests/test_unified_sighash.py``).
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import cast

from bitcointx.core import CMutableTransaction, CTransaction
from bitcointx.core.key import CKey
from bitcointx.core.script import SIGVERSION_WITNESS_V0, CScript, SIGHASH_Type, SignatureHash
from jmcore.bitcoin import (
    ParsedTransaction,
    TxInput,
    TxOutput,
    create_p2wpkh_script_code,
    decode_varint,
    encode_varint,
    hash256,
    parse_transaction_bytes,
    serialize_transaction,
)
from jmcore.crypto import verify_strict_ecdsa

# Backward-compat alias: old code imports ``Transaction`` from here.
Transaction = ParsedTransaction

# Alias for backward compatibility
read_varint = decode_varint


class TransactionSigningError(Exception):
    pass


# Unified opt-in sighash (XBT replay protection). See module docstring.
SIGHASH_ALL = 0x01
SIGHASH_UNIFIED = 0x20
SIGHASH_UNIFIED_ALL = SIGHASH_UNIFIED | SIGHASH_ALL  # 0x21

# Script type byte of the unified message: 0 bare/P2SH, 1 segwit v0 (P2WPKH and
# P2WSH alike), 2/3 taproot (not used by this wallet).
UNIFIED_SCRIPT_TYPE_SEGWIT_V0 = 1

# One (amount_sats, scriptPubKey) per transaction input, in input order.
SpentOutputs = Sequence[tuple[int, bytes]]


def _sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def _tagged_hash(tag: str, msg: bytes) -> bytes:
    tag_hash = _sha256(tag.encode())
    return _sha256(tag_hash + tag_hash + msg)


def _push_bytes(data: bytes) -> bytes:
    return encode_varint(len(data)) + data


def unified_sighash_message(
    *,
    version: int,
    locktime: int,
    hash_type: int,
    script_type: int,
    prevouts: Sequence[bytes],
    amounts: Sequence[int],
    spent_scripts: Sequence[bytes],
    sequences: Sequence[int],
    outputs: Sequence[tuple[int, bytes]],
    input_index: int,
    script_code: bytes,
) -> bytes:
    """Unified opt-in sighash for a bare/P2SH (type 0) or segwit v0 (type 1) input.

    Port of Kilowallet's ``TxBuilder.unifiedMessage`` / Knots'
    ``SignatureHashUnified``. Differs from BIP-143: single-SHA256 aggregates,
    two extra aggregates over every spent amount and scriptPubKey, a 5-byte
    zero-extended locktime, an epoch and script-type byte, the input index
    (or under ANYONECANPAY the input's own data) and a BIP-340 tagged hash
    ("UnifiedSighash") in place of the final double SHA-256.

    ``prevouts`` are 36-byte serialized outpoints (txid LE + vout LE).
    """
    n = len(prevouts)
    if not (len(amounts) == len(spent_scripts) == len(sequences) == n):
        raise TransactionSigningError("Unified sighash needs one spent output per input")
    if not 0 <= input_index < n:
        raise TransactionSigningError("Input index out of range")
    if script_type not in (0, 1):
        raise TransactionSigningError(f"Unsupported unified script type {script_type}")

    anyone_can_pay = (hash_type & 0x80) != 0
    base = hash_type & 0x1F  # 1 ALL, 2 NONE, 3 SINGLE

    msg = bytearray()
    msg += b"\x00"  # epoch
    msg += bytes([hash_type & 0xFF])
    msg += (version & 0xFFFFFFFF).to_bytes(4, "little")
    msg += (locktime & 0xFFFFFFFFFF).to_bytes(5, "little")  # 5-byte, zero-extended
    if not anyone_can_pay:
        msg += _sha256(b"".join(prevouts))
        msg += _sha256(b"".join(a.to_bytes(8, "little") for a in amounts))
        msg += _sha256(b"".join(_push_bytes(s) for s in spent_scripts))
        msg += _sha256(b"".join((q & 0xFFFFFFFF).to_bytes(4, "little") for q in sequences))
    if base not in (0x02, 0x03):
        msg += _sha256(b"".join(v.to_bytes(8, "little") + _push_bytes(s) for v, s in outputs))
    msg += bytes([script_type])
    if anyone_can_pay:
        msg += prevouts[input_index]
        msg += amounts[input_index].to_bytes(8, "little")
        msg += _push_bytes(spent_scripts[input_index])
        msg += (sequences[input_index] & 0xFFFFFFFF).to_bytes(4, "little")
    else:
        msg += input_index.to_bytes(4, "little")
    msg += _push_bytes(script_code)
    if base == 0x03:
        if input_index >= len(outputs):
            raise TransactionSigningError("SIGHASH_SINGLE has no output at the input's index")
        value, script = outputs[input_index]
        msg += _sha256(value.to_bytes(8, "little") + _push_bytes(script))
    return _tagged_hash("UnifiedSighash", bytes(msg))


def compute_sighash_unified(
    tx: ParsedTransaction,
    input_index: int,
    script_code: bytes,
    spent_outputs: SpentOutputs,
    sighash_type: int = SIGHASH_UNIFIED_ALL,
) -> bytes:
    """Unified sighash (0x21) for a segwit v0 input of ``tx``.

    ``script_code`` is the BIP-143 scriptCode: the key's P2PKH script for
    P2WPKH, the witness script for P2WSH. ``spent_outputs`` gives every
    input's (amount, scriptPubKey) in input order; the caller must have
    obtained them from a trusted source (own wallet or own node).
    """
    if sighash_type != SIGHASH_UNIFIED_ALL:
        raise TransactionSigningError(
            f"Unsupported unified sighash type {sighash_type:#x}; only 0x21 is allowed"
        )
    if len(spent_outputs) != len(tx.inputs):
        raise TransactionSigningError(
            f"Unified sighash needs all {len(tx.inputs)} spent outputs, got {len(spent_outputs)}"
        )
    return unified_sighash_message(
        version=tx.version,
        locktime=tx.locktime,
        hash_type=sighash_type,
        script_type=UNIFIED_SCRIPT_TYPE_SEGWIT_V0,
        prevouts=[i.txid_le + i.vout.to_bytes(4, "little") for i in tx.inputs],
        amounts=[int(v) for v, _ in spent_outputs],
        spent_scripts=[bytes(s) for _, s in spent_outputs],
        sequences=[i.sequence for i in tx.inputs],
        outputs=[(o.value, o.script) for o in tx.outputs],
        input_index=input_index,
        script_code=script_code,
    )


def _check_spent_value(spent_outputs: SpentOutputs, input_index: int, value: int) -> None:
    if not 0 <= input_index < len(spent_outputs):
        raise TransactionSigningError("Input index out of range")
    if int(spent_outputs[input_index][0]) != int(value):
        raise TransactionSigningError(
            f"Spent output value mismatch for input {input_index}: "
            f"{spent_outputs[input_index][0]} != {value}"
        )


def _signature_hash(
    tx: ParsedTransaction,
    input_index: int,
    script_code: bytes,
    value: int,
    sighash_type: int,
    spent_outputs: SpentOutputs | None,
) -> bytes:
    """Sighash for signing/verifying; XBT build accepts only 0x21."""
    if sighash_type != SIGHASH_UNIFIED_ALL:
        # Plain SIGHASH_ALL (0x01) would be valid on the SHA-256 chain too and
        # make the input replayable, so this build never signs or accepts it.
        raise TransactionSigningError(
            f"Unsupported sighash type {sighash_type:#x}; this XBT build only "
            "allows the unified sighash SIGHASH_UNIFIED|SIGHASH_ALL (0x21)"
        )
    if spent_outputs is None:
        raise TransactionSigningError(
            "Unified sighash requires the amount and scriptPubKey of every input"
        )
    _check_spent_value(spent_outputs, input_index, value)
    return compute_sighash_unified(tx, input_index, script_code, spent_outputs, sighash_type)


def deserialize_transaction(tx_bytes: bytes) -> ParsedTransaction:
    """Deserialize a raw transaction for signing.

    Delegates to :func:`jmcore.bitcoin.parse_transaction_bytes` which now
    returns typed ``TxInput`` / ``TxOutput`` objects with the dual-accessor
    API required by the signing code.

    Raises:
        TransactionSigningError: If the transaction bytes cannot be parsed.
    """
    try:
        return parse_transaction_bytes(tx_bytes)
    except Exception as e:
        raise TransactionSigningError(f"Failed to parse transaction: {e}") from e


def compute_sighash_segwit(
    tx: ParsedTransaction,
    input_index: int,
    script_code: bytes,
    value: int,
    sighash_type: int,
) -> bytes:
    """Compute the BIP-143 sighash for a segwit input.

    Delegates to :func:`bitcointx.core.script.SignatureHash` with
    ``SIGVERSION_WITNESS_V0``. The ``ParsedTransaction`` is re-serialized
    and parsed by bitcointx so we never re-implement the BIP-143 preimage
    layout in-tree.
    """
    try:
        if input_index >= len(tx.inputs):
            raise TransactionSigningError("Input index out of range")

        ctx = CTransaction.deserialize(
            serialize_transaction(tx.version, tx.inputs, tx.outputs, tx.locktime)
        )
        if ctx.nLockTime > 0x7FFFFFFF:
            # bitcointx packs nLockTime into the BIP-143 preimage as a signed
            # int32; its two's complement gives the same four bytes.
            mutable = CMutableTransaction.from_tx(ctx)
            mutable.nLockTime -= 1 << 32
            ctx = mutable
        return bytes(
            SignatureHash(
                CScript(script_code),
                ctx,
                input_index,
                SIGHASH_Type(sighash_type),
                amount=value,
                sigversion=SIGVERSION_WITNESS_V0,
            )
        )

    except TransactionSigningError:
        raise
    except Exception as e:
        raise TransactionSigningError(f"Failed to compute sighash: {e}") from e


def sign_p2wpkh_input(
    tx: ParsedTransaction,
    input_index: int,
    script_code: bytes,
    value: int,
    private_key: CKey,
    sighash_type: int = SIGHASH_UNIFIED_ALL,
    spent_outputs: SpentOutputs | None = None,
) -> bytes:
    """Sign a P2WPKH input.

    Args:
        tx: The transaction to sign
        input_index: Index of the input to sign
        script_code: The scriptCode for signing (P2PKH script for P2WPKH)
        value: The value of the input being spent (in satoshis)
        private_key: Signing private key
        sighash_type: Sighash type (XBT: only SIGHASH_UNIFIED|SIGHASH_ALL = 0x21)
        spent_outputs: (amount, scriptPubKey) of every input, in input order

    Returns:
        DER-encoded signature with sighash type byte appended
    """
    sighash = _signature_hash(tx, input_index, script_code, value, sighash_type, spent_outputs)

    # Sign the pre-hashed sighash.
    signature = cast(bytes, private_key.sign(sighash, _ecdsa_sig_grind_low_r=False))

    return signature + bytes([sighash_type])


def verify_p2wpkh_signature(
    tx: ParsedTransaction,
    input_index: int,
    script_code: bytes,
    value: int,
    signature: bytes,
    pubkey: bytes,
    spent_outputs: SpentOutputs | None = None,
) -> bool:
    """Verify a P2WPKH signature.

    XBT build: only unified (0x21) signatures verify; a plain SIGHASH_ALL
    signature is rejected because it would be replayable on the SHA-256 chain.

    Args:
        tx: The transaction containing the input
        input_index: Index of the input to verify
        script_code: The scriptCode (P2PKH script for P2WPKH)
        value: The value of the input being spent (in satoshis)
        signature: DER-encoded signature with sighash type byte appended
        pubkey: Public key bytes (compressed or uncompressed)

    Returns:
        True if signature is valid, False otherwise
    """
    try:
        # Extract sighash type from last byte of signature
        if not signature:
            return False
        sighash_type = signature[-1]
        der_signature = signature[:-1]

        sighash = _signature_hash(tx, input_index, script_code, value, sighash_type, spent_outputs)

        return verify_strict_ecdsa(sighash, der_signature, pubkey)
    except Exception:
        return False


def create_witness_stack(signature: bytes, pubkey_bytes: bytes) -> list[bytes]:
    return [signature, pubkey_bytes]


def sign_p2wsh_input(
    tx: ParsedTransaction,
    input_index: int,
    witness_script: bytes,
    value: int,
    private_key: CKey,
    sighash_type: int = SIGHASH_UNIFIED_ALL,
    spent_outputs: SpentOutputs | None = None,
) -> bytes:
    """Sign a P2WSH input.

    For P2WSH, the scriptCode in BIP143 signing is the witness script itself.

    Args:
        tx: The transaction to sign
        input_index: Index of the input to sign
        witness_script: The witness script (e.g., timelocked freeze script)
        value: The value of the input being spent (in satoshis)
        private_key: Signing private key
        sighash_type: Sighash type (XBT: only SIGHASH_UNIFIED|SIGHASH_ALL = 0x21)
        spent_outputs: (amount, scriptPubKey) of every input, in input order

    Returns:
        DER-encoded signature with sighash type byte appended
    """
    # For P2WSH, the scriptCode is the witness script itself (unified script
    # type 1, same as P2WPKH, as in Knots' interpreter and Kilowallet).
    sighash = _signature_hash(tx, input_index, witness_script, value, sighash_type, spent_outputs)

    # Sign the pre-hashed sighash.
    signature = cast(bytes, private_key.sign(sighash, _ecdsa_sig_grind_low_r=False))

    return signature + bytes([sighash_type])


def verify_p2wsh_signature(
    tx: ParsedTransaction,
    input_index: int,
    witness_script: bytes,
    value: int,
    signature: bytes,
    pubkey: bytes,
    spent_outputs: SpentOutputs | None = None,
) -> bool:
    """Verify a P2WSH unified (0x21) signature against its witness script."""
    try:
        if not signature:
            return False
        sighash_type = signature[-1]
        sighash = _signature_hash(
            tx, input_index, witness_script, value, sighash_type, spent_outputs
        )
        return verify_strict_ecdsa(sighash, signature[:-1], pubkey)
    except Exception:
        return False


def create_p2wsh_witness_stack(signature: bytes, witness_script: bytes) -> list[bytes]:
    """Create witness stack for P2WSH input.

    For timelocked scripts (CLTV), the witness is: [signature, witness_script]

    Args:
        signature: DER signature with sighash byte
        witness_script: The witness script (e.g., freeze script)

    Returns:
        Witness stack: [signature, witness_script]
    """
    return [signature, witness_script]


# Re-export from jmcore for backward compatibility
__all__ = [
    "SIGHASH_ALL",
    "SIGHASH_UNIFIED",
    "SIGHASH_UNIFIED_ALL",
    "ParsedTransaction",
    "SpentOutputs",
    "compute_sighash_unified",
    "unified_sighash_message",
    "Transaction",
    "TransactionSigningError",
    "TxInput",
    "TxOutput",
    "compute_sighash_segwit",
    "create_p2wpkh_script_code",
    "create_p2wsh_witness_stack",
    "create_witness_stack",
    "deserialize_transaction",
    "encode_varint",
    "hash256",
    "read_varint",
    "sign_p2wpkh_input",
    "sign_p2wsh_input",
    "verify_p2wpkh_signature",
    "verify_p2wsh_signature",
]
