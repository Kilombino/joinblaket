import hashlib
"""
Tests for transaction signing utilities.
"""

import struct

import pytest
from jmcore.btc_script import mk_freeze_script
from jmcore.constants import SECP256K1_N
from jmcore.timenumber import TIMENUMBER_COUNT, timenumber_to_timestamp

from jmwallet.wallet.bip32 import HDKey, mnemonic_to_seed
from jmwallet.wallet.signing import (
    Transaction,
    TransactionSigningError,
    TxInput,
    TxOutput,
    compute_sighash_segwit,
    create_p2wpkh_script_code,
    create_p2wsh_witness_stack,
    create_witness_stack,
    deserialize_transaction,
    encode_varint,
    hash256,
    read_varint,
    sign_p2wpkh_input,
    sign_p2wsh_input,
    verify_p2wpkh_signature,
    verify_p2wsh_signature,
)

# XBT build: signing uses the unified sighash (0x21), which needs every
# input's amount and scriptPubKey. These single-input tests spend a dummy
# P2WPKH output of the given value.
_DUMMY_SPK = bytes.fromhex("0014" + "00" * 20)


def _spent(value: int = 100000) -> list[tuple[int, bytes]]:
    return [(value, _DUMMY_SPK)]


def _encode_der_integer(value: int) -> bytes:
    encoded = value.to_bytes((value.bit_length() + 7) // 8, "big")
    if encoded[0] & 0x80:
        encoded = b"\x00" + encoded
    return b"\x02" + bytes([len(encoded)]) + encoded


def _with_high_s(signature: bytes) -> bytes:
    r_length = signature[3]
    r_start = 4
    s_length = signature[r_start + r_length + 1]
    s_start = r_start + r_length + 2
    r_value = int.from_bytes(signature[r_start : r_start + r_length], "big")
    s_value = int.from_bytes(signature[s_start : s_start + s_length], "big")
    high_s = SECP256K1_N - s_value
    assert high_s > SECP256K1_N // 2
    encoded = _encode_der_integer(r_value) + _encode_der_integer(high_s)
    return b"\x30" + bytes([len(encoded)]) + encoded


class TestHash256:
    def test_empty_input(self):
        result = hash256(b"")
        assert len(result) == 32
        # SHA256(SHA256("")) = known value
        expected = bytes.fromhex("5df6e0e2761359d30a8275058e299fcc0381534545f55cf43e41983f5d4c9456")
        assert result == expected

    def test_known_value(self):
        result = hash256(b"hello")
        assert len(result) == 32
        assert isinstance(result, bytes)


class TestVarint:
    def test_read_single_byte(self):
        data = bytes([0x05, 0xFF])
        value, offset = read_varint(data, 0)
        assert value == 5
        assert offset == 1

    def test_read_two_bytes(self):
        data = bytes([0xFD, 0x01, 0x00])
        value, offset = read_varint(data, 0)
        assert value == 1
        assert offset == 3

    def test_read_four_bytes(self):
        data = bytes([0xFE, 0x01, 0x00, 0x00, 0x00])
        value, offset = read_varint(data, 0)
        assert value == 1
        assert offset == 5

    def test_encode_single_byte(self):
        result = encode_varint(5)
        assert result == bytes([5])

    def test_encode_two_bytes(self):
        result = encode_varint(0x100)
        assert result == bytes([0xFD, 0x00, 0x01])

    def test_encode_four_bytes(self):
        result = encode_varint(0x10000)
        assert result == bytes([0xFE, 0x00, 0x00, 0x01, 0x00])

    def test_roundtrip(self):
        for value in [0, 1, 252, 253, 0xFFFF, 0x10000, 0xFFFFFFFF]:
            encoded = encode_varint(value)
            decoded, _ = read_varint(encoded, 0)
            assert decoded == value


class TestDeserializeTransaction:
    # A simple P2WPKH transaction (segwit)
    # This is a minimal valid regtest transaction structure
    SAMPLE_TX_HEX = (
        "02000000"  # version
        "0001"  # marker + flag (segwit)
        "01"  # input count
        "0000000000000000000000000000000000000000000000000000000000000000"  # prev txid
        "00000000"  # prev vout
        "00"  # scriptSig length (empty for segwit)
        "ffffffff"  # sequence
        "01"  # output count
        "0000000000000000"  # value (0 sats)
        "16"  # scriptPubKey length
        "0014751e76e8199196d454941c45d1b3a323f1433bd6"  # P2WPKH scriptPubKey
        "00"  # witness stack count for input 0
        "00000000"  # locktime
    )

    def test_deserialize_basic(self):
        tx_bytes = bytes.fromhex(self.SAMPLE_TX_HEX)
        tx = deserialize_transaction(tx_bytes)

        assert tx.version == 2
        assert tx.version_bytes == bytes.fromhex("02000000")
        assert tx.has_witness is True
        assert len(tx.inputs) == 1
        assert len(tx.outputs) == 1
        assert tx.locktime == 0
        assert tx.locktime_bytes == bytes.fromhex("00000000")

    def test_deserialize_input_fields(self):
        tx_bytes = bytes.fromhex(self.SAMPLE_TX_HEX)
        tx = deserialize_transaction(tx_bytes)

        inp = tx.inputs[0]
        assert len(inp.txid_le) == 32
        assert inp.vout == 0
        assert inp.sequence == 0xFFFFFFFF
        assert inp.sequence_bytes == b"\xff\xff\xff\xff"

    def test_deserialize_output_fields(self):
        tx_bytes = bytes.fromhex(self.SAMPLE_TX_HEX)
        tx = deserialize_transaction(tx_bytes)

        out = tx.outputs[0]
        assert out.value == 0
        assert len(out.script) == 22  # P2WPKH

    def test_invalid_transaction(self):
        with pytest.raises(TransactionSigningError):
            deserialize_transaction(b"\x00\x01\x02")


class TestScriptCode:
    def test_p2wpkh_script_code(self):
        # Known pubkey (secp256k1 generator point G)
        pubkey = bytes.fromhex("0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798")
        script_code = create_p2wpkh_script_code(pubkey)

        # Script code format: 0x76 0xa9 0x14 <20-byte-hash> 0x88 0xac (25 bytes)
        # Note: BIP 143 says the scriptCode is the bare script without length prefix
        # The length prefix is added by compute_sighash_segwit via encode_varint
        assert len(script_code) == 25
        assert script_code[0] == 0x76  # OP_DUP
        assert script_code[1] == 0xA9  # OP_HASH160
        assert script_code[2] == 0x14  # 20 bytes follow
        assert script_code[-2] == 0x88  # OP_EQUALVERIFY
        assert script_code[-1] == 0xAC  # OP_CHECKSIG


class TestWitnessStack:
    def test_create_witness_stack(self):
        sig = bytes.fromhex("3044" + "00" * 68)  # Mock DER signature
        pubkey = bytes.fromhex("02" + "00" * 32)

        stack = create_witness_stack(sig, pubkey)

        assert len(stack) == 2
        assert stack[0] == sig
        assert stack[1] == pubkey


class TestSigning:
    @pytest.fixture
    def test_key(self, test_mnemonic):
        """Get a test private key from BIP32 derivation."""
        seed = mnemonic_to_seed(test_mnemonic)
        master = HDKey.from_seed(seed)
        return master.derive("m/84'/0'/0'/0/0")

    def test_compute_sighash_segwit(self, test_key):
        """Test BIP143 sighash computation."""
        tx = Transaction(
            version=2,
            has_witness=True,
            inputs=[
                TxInput(
                    txid_le=bytes(32),
                    vout=0,
                    scriptsig=b"",
                    sequence=0xFFFFFFFF,
                )
            ],
            outputs=[TxOutput(value=50000, script=bytes.fromhex("0014" + "00" * 20))],
            locktime=0,
            witnesses=[],
        )

        pubkey = test_key.get_public_key_bytes(compressed=True)
        script_code = create_p2wpkh_script_code(pubkey)

        sighash = compute_sighash_segwit(
            tx=tx,
            input_index=0,
            script_code=script_code,
            value=100000,
            sighash_type=1,  # SIGHASH_ALL
        )

        assert len(sighash) == 32
        assert isinstance(sighash, bytes)

    def test_sign_p2wpkh_input(self, test_key):
        """Test actual signing of a P2WPKH input."""
        tx = Transaction(
            version=2,
            has_witness=True,
            inputs=[
                TxInput(
                    txid_le=bytes(32),
                    vout=0,
                    scriptsig=b"",
                    sequence=0xFFFFFFFF,
                )
            ],
            outputs=[TxOutput(value=50000, script=bytes.fromhex("0014" + "00" * 20))],
            locktime=0,
            witnesses=[],
        )

        pubkey = test_key.get_public_key_bytes(compressed=True)
        script_code = create_p2wpkh_script_code(pubkey)

        # Pass the signing key directly.
        signature = sign_p2wpkh_input(
            tx=tx,
            input_index=0,
            script_code=script_code,
            value=100000,
            private_key=test_key.private_key,
            spent_outputs=_spent(),
        )

        # Signature should be DER-encoded + sighash byte
        assert len(signature) > 64  # DER is variable length
        assert signature[-1] == 0x21  # SIGHASH_UNIFIED | SIGHASH_ALL

    def test_sign_invalid_input_index(self, test_key):
        """Test that invalid input index raises error."""
        tx = Transaction(
            version=2,
            has_witness=True,
            inputs=[],
            outputs=[],
            locktime=0,
            witnesses=[],
        )

        pubkey = test_key.get_public_key_bytes(compressed=True)
        script_code = create_p2wpkh_script_code(pubkey)

        with pytest.raises(TransactionSigningError):
            sign_p2wpkh_input(
                tx=tx,
                input_index=0,
                script_code=script_code,
                value=100000,
                private_key=test_key.private_key,
            )

    def test_compute_sighash_allows_non_default_type(self, test_key):
        """compute_sighash_segwit should allow any sighash type (pure computation)."""
        tx = Transaction(
            version=2,
            has_witness=True,
            inputs=[
                TxInput(
                    txid_le=bytes(32),
                    vout=0,
                    scriptsig=b"",
                    sequence=0xFFFFFFFF,
                )
            ],
            outputs=[TxOutput(value=50000, script=bytes.fromhex("0014" + "00" * 20))],
            locktime=0,
            witnesses=[],
        )
        pubkey = test_key.get_public_key_bytes(compressed=True)
        script_code = create_p2wpkh_script_code(pubkey)

        # Should succeed -- compute is a pure BIP143 function, not a signing policy gate
        sighash = compute_sighash_segwit(
            tx=tx,
            input_index=0,
            script_code=script_code,
            value=100000,
            sighash_type=2,
        )
        assert len(sighash) == 32

    def test_sign_p2wpkh_rejects_non_default_sighash(self, test_key):
        """sign_p2wpkh_input should reject non-SIGHASH_ALL types."""
        tx = Transaction(
            version=2,
            has_witness=True,
            inputs=[
                TxInput(
                    txid_le=bytes(32),
                    vout=0,
                    scriptsig=b"",
                    sequence=0xFFFFFFFF,
                )
            ],
            outputs=[TxOutput(value=50000, script=bytes.fromhex("0014" + "00" * 20))],
            locktime=0,
            witnesses=[],
        )
        pubkey = test_key.get_public_key_bytes(compressed=True)
        script_code = create_p2wpkh_script_code(pubkey)

        with pytest.raises(TransactionSigningError, match="0x21"):
            sign_p2wpkh_input(
                tx=tx,
                input_index=0,
                script_code=script_code,
                value=100000,
                private_key=test_key.private_key,
                sighash_type=2,
                spent_outputs=_spent(),
            )

    def test_sign_p2wsh_rejects_non_default_sighash(self, test_key):
        """sign_p2wsh_input should reject non-SIGHASH_ALL types."""
        tx = Transaction(
            version=2,
            has_witness=True,
            inputs=[
                TxInput(
                    txid_le=bytes(32),
                    vout=0,
                    scriptsig=b"",
                    sequence=0xFFFFFFFF,
                )
            ],
            outputs=[TxOutput(value=50000, script=bytes.fromhex("0014" + "00" * 20))],
            locktime=0,
            witnesses=[],
        )

        with pytest.raises(TransactionSigningError, match="0x21"):
            sign_p2wsh_input(
                tx=tx,
                input_index=0,
                witness_script=b"\x00" * 32,
                value=100000,
                private_key=test_key.private_key,
                sighash_type=0x83,  # SIGHASH_SINGLE|ANYONECANPAY
                spent_outputs=_spent(),
            )

    @pytest.mark.parametrize(
        "locktime", [0, 0x7FFFFFFF, 0x80000000, 2_148_595_200, 4_099_766_400, 0xFFFFFFFF]
    )
    def test_compute_sighash_segwit_unsigned_locktime(self, test_key, locktime):
        """nLockTime is an unsigned field of the BIP-143 preimage."""
        txid_le = bytes(range(32))
        sequence = 0xFFFFFFFD
        output_script = bytes.fromhex("0014" + "11" * 20)
        tx = Transaction(
            version=2,
            has_witness=True,
            inputs=[TxInput(txid_le=txid_le, vout=1, scriptsig=b"", sequence=sequence)],
            outputs=[TxOutput(value=50000, script=output_script)],
            locktime=locktime,
            witnesses=[],
        )
        script_code = create_p2wpkh_script_code(test_key.get_public_key_bytes(compressed=True))
        outpoint = txid_le + struct.pack("<I", 1)
        preimage = (
            struct.pack("<i", 2)
            + hash256(outpoint)
            + hash256(struct.pack("<I", sequence))
            + outpoint
            + encode_varint(len(script_code))
            + script_code
            + struct.pack("<q", 100000)
            + struct.pack("<I", sequence)
            + hash256(struct.pack("<q", 50000) + encode_varint(len(output_script)) + output_script)
            + struct.pack("<I", locktime)
            + struct.pack("<I", 1)
        )

        sighash = compute_sighash_segwit(
            tx=tx, input_index=0, script_code=script_code, value=100000, sighash_type=1
        )

        assert sighash == hash256(preimage)

    def test_sign_p2wsh_bond_locked_until_2099(self, test_mnemonic):
        """A bond locked until the last timenumber can be spent."""
        locktime = timenumber_to_timestamp(TIMENUMBER_COUNT - 1)
        key = HDKey.from_seed(mnemonic_to_seed(test_mnemonic)).derive(
            f"m/84'/0'/0'/2/{TIMENUMBER_COUNT - 1}"
        )
        pubkey = key.get_public_key_bytes(compressed=True)
        witness_script = mk_freeze_script(pubkey.hex(), locktime)
        tx = Transaction(
            version=2,
            has_witness=True,
            inputs=[TxInput(txid_le=bytes(32), vout=0, scriptsig=b"", sequence=0xFFFFFFFD)],
            outputs=[TxOutput(value=99000, script=bytes.fromhex("0014" + "00" * 20))],
            locktime=locktime,
            witnesses=[],
        )

        # XBT build: unified sighash commits to every spent output.
        spent = [(100000, b"\x00\x20" + hashlib.sha256(witness_script).digest())]
        signature = sign_p2wsh_input(
            tx=tx,
            input_index=0,
            witness_script=witness_script,
            value=100000,
            private_key=key.private_key,
            spent_outputs=spent,
        )

        assert verify_p2wsh_signature(tx, 0, witness_script, 100000, signature, pubkey, spent)


class TestSignatureVerification:
    """Integration tests to verify signatures are valid."""

    def test_signature_deterministic(self, test_mnemonic):
        """Signing the same transaction twice should produce the same signature."""
        seed = mnemonic_to_seed(test_mnemonic)
        master = HDKey.from_seed(seed)
        key = master.derive("m/84'/0'/0'/0/0")

        tx = Transaction(
            version=2,
            has_witness=True,
            inputs=[
                TxInput(
                    txid_le=bytes(32),
                    vout=0,
                    scriptsig=b"",
                    sequence=0xFFFFFFFF,
                )
            ],
            outputs=[TxOutput(value=50000, script=bytes.fromhex("0014" + "00" * 20))],
            locktime=0,
            witnesses=[],
        )

        pubkey = key.get_public_key_bytes(compressed=True)
        script_code = create_p2wpkh_script_code(pubkey)

        sig1 = sign_p2wpkh_input(
            tx, 0, script_code, 100000, key.private_key, spent_outputs=_spent()
        )
        sig2 = sign_p2wpkh_input(
            tx, 0, script_code, 100000, key.private_key, spent_outputs=_spent()
        )

        assert sig1 == sig2
        assert len(sig1) > 64

    def test_verify_p2wpkh_signature(self, test_mnemonic):
        """Test that a valid signature passes verification."""
        seed = mnemonic_to_seed(test_mnemonic)
        master = HDKey.from_seed(seed)
        key = master.derive("m/84'/0'/0'/0/0")

        tx = Transaction(
            version=2,
            has_witness=True,
            inputs=[
                TxInput(
                    txid_le=bytes(32),
                    vout=0,
                    scriptsig=b"",
                    sequence=0xFFFFFFFF,
                )
            ],
            outputs=[TxOutput(value=50000, script=bytes.fromhex("0014" + "00" * 20))],
            locktime=0,
            witnesses=[],
        )

        pubkey = key.get_public_key_bytes(compressed=True)
        script_code = create_p2wpkh_script_code(pubkey)
        value = 100000

        # Sign
        signature = sign_p2wpkh_input(
            tx, 0, script_code, value, key.private_key, spent_outputs=_spent(value)
        )

        # Verify
        assert verify_p2wpkh_signature(tx, 0, script_code, value, signature, pubkey, _spent(value))

        high_s_signature = _with_high_s(signature[:-1]) + signature[-1:]
        assert not verify_p2wpkh_signature(
            tx, 0, script_code, value, high_s_signature, pubkey, _spent(value)
        )
        assert not verify_p2wpkh_signature(
            tx, 0, script_code, value, b"\xff" + signature, pubkey, _spent(value)
        )

        # Verify fails with wrong value
        assert not verify_p2wpkh_signature(
            tx, 0, script_code, value - 1, signature, pubkey, _spent(value)
        )

        # Verify fails with wrong pubkey
        other_key = master.derive("m/84'/0'/0'/0/1")
        other_pubkey = other_key.get_public_key_bytes(compressed=True)
        assert not verify_p2wpkh_signature(
            tx, 0, script_code, value, signature, other_pubkey, _spent(value)
        )

        # Verify fails with wrong signature (corrupted)
        bad_sig = bytearray(signature)
        bad_sig[10] ^= 0xFF  # Flip a byte in the signature
        assert not verify_p2wpkh_signature(tx, 0, script_code, value, bytes(bad_sig), pubkey)


class TestP2WSHSigning:
    """Tests for P2WSH (fidelity bond) signing."""

    @pytest.fixture
    def test_key(self, test_mnemonic):
        """Get a test private key from BIP32 derivation."""
        seed = mnemonic_to_seed(test_mnemonic)
        master = HDKey.from_seed(seed)
        # Use fidelity bond path: m/84'/0'/0'/2/0
        return master.derive("m/84'/0'/0'/2/0")

    @pytest.fixture
    def freeze_script(self, test_key):
        """Create a freeze (timelocked) script."""
        from jmcore.btc_script import mk_freeze_script

        pubkey_hex = test_key.get_public_key_bytes(compressed=True).hex()
        locktime = 1700000000  # Example locktime in the past
        return mk_freeze_script(pubkey_hex, locktime)

    def test_sign_p2wsh_input(self, test_key, freeze_script):
        """Test signing a P2WSH input with a timelocked script."""
        tx = Transaction(
            version=2,
            has_witness=True,
            inputs=[
                TxInput(
                    txid_le=bytes(32),
                    vout=0,
                    scriptsig=b"",
                    sequence=0xFFFFFFFE,  # Enable locktime
                )
            ],
            outputs=[TxOutput(value=50000, script=bytes.fromhex("0014" + "00" * 20))],
            locktime=1700000000,  # Must be >= CLTV locktime
            witnesses=[],
        )

        signature = sign_p2wsh_input(
            tx=tx,
            input_index=0,
            witness_script=freeze_script,
            value=100000,
            private_key=test_key.private_key,
            spent_outputs=_spent(),
        )

        # Signature should be DER-encoded + sighash byte
        assert len(signature) > 64  # DER is variable length
        assert signature[-1] == 0x21  # SIGHASH_UNIFIED | SIGHASH_ALL

        pubkey = test_key.get_public_key_bytes(compressed=True)
        assert verify_p2wsh_signature(tx, 0, freeze_script, 100000, signature, pubkey, _spent())
        high_s_signature = _with_high_s(signature[:-1]) + signature[-1:]
        assert not verify_p2wsh_signature(
            tx, 0, freeze_script, 100000, high_s_signature, pubkey, _spent()
        )

    def test_create_p2wsh_witness_stack(self, freeze_script):
        """Test creating P2WSH witness stack."""
        sig = bytes.fromhex("3044" + "00" * 68)  # Mock DER signature

        stack = create_p2wsh_witness_stack(sig, freeze_script)

        assert len(stack) == 2
        assert stack[0] == sig
        assert stack[1] == freeze_script

    def test_p2wsh_signature_deterministic(self, test_key, freeze_script):
        """Signing same P2WSH transaction twice should produce same signature."""
        tx = Transaction(
            version=2,
            has_witness=True,
            inputs=[
                TxInput(
                    txid_le=bytes(32),
                    vout=0,
                    scriptsig=b"",
                    sequence=0xFFFFFFFE,
                )
            ],
            outputs=[TxOutput(value=50000, script=bytes.fromhex("0014" + "00" * 20))],
            locktime=1700000000,
            witnesses=[],
        )

        sig1 = sign_p2wsh_input(
            tx, 0, freeze_script, 100000, test_key.private_key, spent_outputs=_spent()
        )
        sig2 = sign_p2wsh_input(
            tx, 0, freeze_script, 100000, test_key.private_key, spent_outputs=_spent()
        )

        assert sig1 == sig2
        assert len(sig1) > 64

    def test_p2wsh_vs_p2wpkh_different_sighash(self, test_key, freeze_script):
        """P2WSH and P2WPKH signatures for same tx should differ (different script_code)."""
        tx = Transaction(
            version=2,
            has_witness=True,
            inputs=[
                TxInput(
                    txid_le=bytes(32),
                    vout=0,
                    scriptsig=b"",
                    sequence=0xFFFFFFFE,
                )
            ],
            outputs=[TxOutput(value=50000, script=bytes.fromhex("0014" + "00" * 20))],
            locktime=1700000000,
            witnesses=[],
        )

        pubkey = test_key.get_public_key_bytes(compressed=True)
        p2wpkh_script_code = create_p2wpkh_script_code(pubkey)

        sig_p2wsh = sign_p2wsh_input(
            tx, 0, freeze_script, 100000, test_key.private_key, spent_outputs=_spent()
        )
        sig_p2wpkh = sign_p2wpkh_input(
            tx, 0, p2wpkh_script_code, 100000, test_key.private_key, spent_outputs=_spent()
        )

        # Signatures should be different because script_code differs
        assert sig_p2wsh != sig_p2wpkh
