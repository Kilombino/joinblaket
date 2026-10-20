"""XBT build: wire network identity, no default directories, one network identity."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr

from jmcore.config import WalletConfig
from jmcore.directory_client import DirectoryClient, DirectoryClientError
from jmcore.models import (
    DIRECTORY_NODES_MAINNET,
    NetworkType,
    get_default_directory_nodes,
    parse_wire_network,
    require_directory_servers,
    wire_network,
)
from jmcore.nick_auth import NickAuthMode
from jmcore.protocol import create_handshake_request, create_handshake_response
from jmcore.settings import DEFAULT_DIRECTORY_SERVERS

MNEMONIC = SecretStr(
    "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
)


def test_mainnet_is_xbt_on_the_wire() -> None:
    assert wire_network(NetworkType.MAINNET) == "xbt"
    assert wire_network("regtest") == "xbt-regtest"
    assert create_handshake_request("n", "NOT-SERVING-ONION", "mainnet")["network"] == "xbt"
    assert create_handshake_response("d", "mainnet")["network"] == "xbt"


@pytest.mark.parametrize("value", ["mainnet", "testnet", "signet", "regtest", "", None, 1])
def test_upstream_network_names_are_not_accepted(value: object) -> None:
    with pytest.raises(ValueError):
        parse_wire_network(value)


def test_parse_wire_network_roundtrip() -> None:
    for network in NetworkType:
        assert parse_wire_network(wire_network(network)) is network


def test_no_default_directories() -> None:
    assert DIRECTORY_NODES_MAINNET == []
    assert get_default_directory_nodes(NetworkType.MAINNET) == []
    assert all(not servers for servers in DEFAULT_DIRECTORY_SERVERS.values())
    with pytest.raises(ValueError, match="set directory_servers to XBT directories"):
        require_directory_servers([])
    assert require_directory_servers(["x.onion:5222"]) == ["x.onion:5222"]


def test_bitcoin_network_must_equal_network() -> None:
    WalletConfig(mnemonic=MNEMONIC, network=NetworkType.MAINNET)
    WalletConfig(
        mnemonic=MNEMONIC, network=NetworkType.REGTEST, bitcoin_network=NetworkType.REGTEST
    )
    with pytest.raises(ValueError, match="must equal network"):
        WalletConfig(
            mnemonic=MNEMONIC, network=NetworkType.TESTNET, bitcoin_network=NetworkType.REGTEST
        )


def _directory_reply(network: str) -> bytes:
    line = json.dumps(create_handshake_response("dir", "regtest") | {"network": network})
    return json.dumps({"type": 795, "line": line}).encode()


@pytest.mark.asyncio
@pytest.mark.parametrize(("reply_network", "ok"), [("xbt-regtest", True), ("regtest", False)])
async def test_client_rejects_non_xbt_directory(reply_network: str, ok: bool) -> None:
    """An upstream directory answers with its own network name; we must refuse it."""
    client = DirectoryClient.__new__(DirectoryClient)
    client.network = "regtest"
    client.nick = "J5xbt"
    client.location = "NOT-SERVING-ONION"
    client.neutrino_compat = False
    client.timeout = 5
    client.host, client.port = "127.0.0.1", 5222
    client.nick_auth_mode = NickAuthMode.DISABLED
    client.nick_auth_directory_id = None
    client.connection = MagicMock()
    client.connection.send = AsyncMock()
    client.connection.receive = AsyncMock(return_value=_directory_reply(reply_network))
    if ok:
        await client._handshake()
    else:
        with pytest.raises(DirectoryClientError, match="network mismatch"):
            await client._handshake()
