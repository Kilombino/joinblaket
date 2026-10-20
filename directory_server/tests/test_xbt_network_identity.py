"""XBT build: a directory only admits XBT peers and never upstream "mainnet" ones."""

import json

import pytest
from jmcore.models import NetworkType
from jmcore.protocol import JM_VERSION, create_handshake_request

from directory_server.handshake_handler import HandshakeError, HandshakeHandler

ONION = "abcdefghijklmnopqrstuvwxyz234567abcdefghijklmnopqrstuvwx.onion:5222"


@pytest.fixture
def handler() -> HandshakeHandler:
    return HandshakeHandler(network=NetworkType.MAINNET, server_nick="xbt_dir", motd="XBT")


def _handshake(network: str) -> str:
    return json.dumps(
        {
            "app-name": "joinmarket",
            "directory": False,
            "location-string": ONION,
            "proto-ver": JM_VERSION,
            "features": {},
            "nick": "J5peer",
            "network": network,
        }
    )


def test_upstream_mainnet_peer_is_rejected(handler: HandshakeHandler) -> None:
    """What an unpatched JoinMarket(-NG) client sends on real Bitcoin."""
    with pytest.raises(HandshakeError, match="Invalid network: mainnet"):
        handler.process_handshake(_handshake("mainnet"), "127.0.0.1:1")


def test_our_own_handshake_is_accepted_and_answered_as_xbt(handler: HandshakeHandler) -> None:
    request = create_handshake_request("J5peer", ONION, "mainnet")
    assert request["network"] == "xbt"
    peer, response = handler.process_handshake(json.dumps(request), "127.0.0.1:1")
    assert peer.network is NetworkType.MAINNET
    assert response["accepted"] is True
    assert response["network"] == "xbt"


@pytest.mark.parametrize("network", ["xbt-regtest", "xbt-testnet", "regtest", "", "XBT2"])
def test_other_networks_are_rejected(handler: HandshakeHandler, network: str) -> None:
    with pytest.raises(HandshakeError):
        handler.process_handshake(_handshake(network), "127.0.0.1:1")


def test_regtest_directory_requires_xbt_regtest() -> None:
    handler = HandshakeHandler(network=NetworkType.REGTEST, server_nick="d", motd="m")
    handler.process_handshake(_handshake("xbt-regtest"), "127.0.0.1:1")
    with pytest.raises(HandshakeError):
        handler.process_handshake(_handshake("regtest"), "127.0.0.1:1")
