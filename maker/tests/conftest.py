"""
Pytest configuration and fixtures for maker tests.
"""

import pytest
from bitcointx.core.key import CKey


@pytest.fixture
def test_mnemonic() -> str:
    """Test mnemonic (BIP39 test vector)"""
    return (
        "abandon abandon abandon abandon abandon abandon "
        "abandon abandon abandon abandon abandon about"
    )


@pytest.fixture
def test_network() -> str:
    """Test network"""
    return "regtest"


@pytest.fixture
def test_private_key() -> CKey:
    """Generate a test ECDSA private key for fidelity bond tests."""
    return CKey(b"\x01" * 32)


@pytest.fixture
def test_pubkey(test_private_key: CKey) -> bytes:
    """Get compressed public key from test private key."""
    return bytes(test_private_key.pub)


# XBT build: there are no default directory servers and makers/takers refuse to
# start without one. Tests that build configs from default settings get a
# placeholder XBT directory here; tests that pass directories explicitly are
# unaffected (init/CLI values take precedence over the environment).
XBT_TEST_DIRECTORY = "xbttestdirectoryxbttestdirectoryxbttestdirectoryxbttestd.onion:5222"


@pytest.fixture(autouse=True)
def _xbt_test_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NETWORK_CONFIG__DIRECTORY_SERVERS", XBT_TEST_DIRECTORY)
