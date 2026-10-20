"""XBT build: the descriptor-wallet backend refuses non-BLAKE2b nodes."""

from __future__ import annotations

from typing import Any

import pytest

from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend, XBTChainError

# Shapes taken from Bitcoin Knots BLAKE2b (v29.4.2.knots20260508) on regtest.
XBT_INFO = {"chain": "regtest", "blocks": 228, "bestblockhash": "aa" * 32, "difficulty_blake2b": 2}
XBT_HEADER = {"hash": "aa" * 32, "height": 228, "header_version": 2, "difficulty_blake2b": 2}
# A SHA-256 Bitcoin Core / Knots node.
SHA_INFO = {"chain": "main", "blocks": 961700, "bestblockhash": "bb" * 32, "difficulty": 1.0}
SHA_HEADER = {"hash": "bb" * 32, "height": 961700, "difficulty": 1.0}
# A BLAKE2b node before the fork activates (old 80-byte header).
PRE_FORK_INFO = {"chain": "regtest", "blocks": 100, "bestblockhash": "cc" * 32, "difficulty": 1}
PRE_FORK_HEADER = {"hash": "cc" * 32, "height": 100, "header_version": 0}


def _backend(info: dict[str, Any], header: dict[str, Any]) -> tuple[DescriptorWalletBackend, list]:
    backend = DescriptorWalletBackend(
        rpc_url="http://127.0.0.1:18999", rpc_user="u", rpc_password="p", wallet_name="t"
    )
    calls: list[str] = []

    async def fake_inner(method: str, params: Any = None, *_a: Any, **_k: Any) -> Any:
        calls.append(method)
        if method == "getblockchaininfo":
            return info
        if method == "getblockheader":
            return header
        if method == "getblockcount":
            return info["blocks"]
        raise AssertionError(method)

    backend._rpc_call_inner = fake_inner  # type: ignore[method-assign]
    return backend, calls


@pytest.mark.asyncio
async def test_blake2b_node_is_accepted_once() -> None:
    backend, calls = _backend(XBT_INFO, XBT_HEADER)
    assert await backend._rpc_call("getblockcount", use_wallet=False) == 228
    assert await backend._rpc_call("getblockcount", use_wallet=False) == 228
    assert calls == ["getblockchaininfo", "getblockheader", "getblockcount", "getblockcount"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("info", "header"), [(SHA_INFO, SHA_HEADER), (PRE_FORK_INFO, PRE_FORK_HEADER)]
)
async def test_non_blake2b_node_is_refused(info: dict, header: dict) -> None:
    backend, calls = _backend(info, header)
    for _ in range(2):  # fail closed on every call, not just the first
        with pytest.raises(XBTChainError):
            await backend._rpc_call("getblockcount", use_wallet=False)
    assert "getblockcount" not in calls


@pytest.mark.asyncio
async def test_fee_estimation_does_not_hide_wrong_chain() -> None:
    backend, _ = _backend(SHA_INFO, SHA_HEADER)
    with pytest.raises(XBTChainError):
        await backend.estimate_fee(3)
