"""JoinBlaket: taker failures with a stable code on /taker/status, and
GET /taker/max-coinjoin (largest amount one CoinJoin can send from a jar).

On 15 Nov 2026 a Jam user's CoinJoin stopped silently; the reason (jar 0
does not merge coins that do not come from a CoinJoin) was only in the logs.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from fastapi.testclient import TestClient

from jmwallet.wallet.models import UTXOInfo
from jmwallet.wallet.service import WalletService
from jmwalletd.deps import get_daemon_state
from taker.models import TakerState

WALLET = "test_wallet.jmdat"


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _utxo(tag: str, value: int, mixdepth: int = 0, confirmations: int = 10) -> UTXOInfo:
    return UTXOInfo(
        txid=tag * 64,
        vout=0,
        value=value,
        address=f"bcrt1{tag}",
        confirmations=confirmations,
        scriptpubkey="0014" + tag * 40,
        path=f"m/84'/0'/{mixdepth}'/0/0",
        mixdepth=mixdepth,
    )


def _wait_not_running(client: TestClient, token: str) -> dict[str, Any]:
    for _ in range(100):
        body = client.get(f"/api/v1/wallet/{WALLET}/taker/status", headers=_headers(token)).json()
        if not body["running"] and body["status"] is not None:
            return body
        time.sleep(0.02)
    raise AssertionError("taker did not finish")


@pytest.fixture
def client_token(app_with_wallet: TestClient, auth_token: str) -> tuple[TestClient, str]:
    return app_with_wallet, auth_token


@pytest.fixture(autouse=True)
def _settings() -> Any:
    from jmcore.models import NetworkType
    from jmcore.settings import JoinMarketSettings

    settings = JoinMarketSettings()
    settings.network_config.network = NetworkType.REGTEST
    settings.network_config.bitcoin_network = NetworkType.REGTEST
    settings.network_config.directory_servers = ["testdirectoryfakeaddress.onion:5222"]
    with patch("jmwalletd.routers.coinjoin.get_settings", return_value=settings):
        yield settings


@patch("jmwalletd._backend.get_backend", new_callable=AsyncMock)
@patch("taker.taker.Taker")
@patch("taker.config.TakerConfig")
def test_failed_run_exposes_code_and_params(
    _config: Mock,
    taker_cls: Mock,
    _backend: AsyncMock,
    client_token: tuple[TestClient, str],
) -> None:
    client, token = client_token
    taker = AsyncMock()
    taker.state = TakerState.FAILED
    taker.txid = ""
    taker.last_broadcast_policy = None
    taker.last_broadcast_method = None
    taker.last_broadcast_fallback_reason = None
    taker.last_failure_reason = (
        "Insufficient funds: rotation-lineage pool has 0, largest non-lineage UTXO has "
        "297776, need 354414. Cannot merge non-CJ md0 UTXOs for privacy reasons."
    )
    taker.last_failure_code = "md0_no_merge"
    taker.last_failure_params = {
        "mixdepth": 0,
        "needed": 354_414,
        "largest_utxo": 297_776,
        "max_amount": 297_776,
        "confirmations_hint": False,
    }
    taker_cls.return_value = taker

    resp = client.post(
        f"/api/v1/wallet/{WALLET}/taker/coinjoin",
        json={"mixdepth": 0, "amount_sats": 354_414, "destination": "bcrt1qd", "counterparties": 1},
        headers=_headers(token),
    )
    assert resp.status_code == 202
    body = _wait_not_running(client, token)
    assert body["status"] == "failed"
    assert body["error_code"] == "md0_no_merge"
    assert body["error_params"]["largest_utxo"] == 297_776
    assert "Cannot merge non-CJ md0" in body["error"]


@patch("jmwalletd._backend.get_backend", new_callable=AsyncMock)
@patch("taker.taker.Taker")
@patch("taker.config.TakerConfig")
def test_unexpected_exception_is_code_other_and_next_start_clears(
    _config: Mock,
    taker_cls: Mock,
    _backend: AsyncMock,
    client_token: tuple[TestClient, str],
) -> None:
    client, token = client_token
    taker = AsyncMock()
    taker.state = TakerState.FETCHING_ORDERBOOK
    taker.txid = ""
    taker.last_broadcast_policy = None
    taker.last_broadcast_method = None
    taker.last_broadcast_fallback_reason = None
    taker.last_failure_reason = None
    taker.do_coinjoin = AsyncMock(side_effect=RuntimeError("directory unreachable"))
    taker_cls.return_value = taker
    payload = {"mixdepth": 1, "amount_sats": 50_000, "destination": "bcrt1qd", "counterparties": 1}

    assert (
        client.post(
            f"/api/v1/wallet/{WALLET}/taker/coinjoin", json=payload, headers=_headers(token)
        ).status_code
        == 202
    )
    body = _wait_not_running(client, token)
    assert body["status"] == "failed"
    assert body["error_code"] == "other"
    assert body["error"] == "directory unreachable"

    # The next start clears the previous error at once.
    state = get_daemon_state()
    taker.do_coinjoin = AsyncMock(return_value="a" * 64)
    taker.state = TakerState.COMPLETE
    taker.txid = "a" * 64
    assert (
        client.post(
            f"/api/v1/wallet/{WALLET}/taker/coinjoin", json=payload, headers=_headers(token)
        ).status_code
        == 202
    )
    body = _wait_not_running(client, token)
    assert body["status"] == "complete"
    assert body["error_code"] is None
    assert body["error_params"] is None
    assert state.last_taker_error is None


def _real_wallet(utxos: dict[int, list[UTXOInfo]]) -> WalletService:
    backend = MagicMock()
    backend.get_utxos = AsyncMock(return_value=[])
    backend.can_estimate_fee = Mock(return_value=True)
    backend.estimate_fee = AsyncMock(return_value=2.0)
    backend.get_mempool_min_fee = AsyncMock(return_value=1.0)
    ws = WalletService(
        mnemonic="abandon " * 11 + "about",
        backend=backend,
        network="regtest",
        mixdepth_count=5,
        gap_limit=20,
    )
    ws.utxo_cache = {md: list(utxos.get(md, [])) for md in range(5)}
    return ws


def test_max_coinjoin_md0_two_deposits(client_token: tuple[TestClient, str]) -> None:
    client, token = client_token
    state = get_daemon_state()
    state.wallet_service = _real_wallet({0: [_utxo("a", 297_776), _utxo("b", 120_000)]})

    resp = client.get(
        f"/api/v1/wallet/{WALLET}/taker/max-coinjoin",
        params={"mixdepth": 0, "counterparties": 1},
        headers=_headers(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["max_selectable"] == 297_776
    assert body["limited_by"] == "md0_no_merge"
    assert body["merge_restricted"] is True
    assert body["eligible_value"] == 417_776
    assert body["min_confirmations"] >= 1
    assert body["fee_rate_sat_vb"] >= 2.0
    assert 0 < body["max_sendable"] < 297_776
    assert body["max_sendable"] + body["estimated_tx_fee"] <= 297_776


def test_max_coinjoin_other_jar_merges(client_token: tuple[TestClient, str]) -> None:
    client, token = client_token
    state = get_daemon_state()
    state.wallet_service = _real_wallet(
        {2: [_utxo("a", 300_000, 2), _utxo("b", 200_000, 2), _utxo("c", 99_000, 2, 1)]}
    )
    resp = client.get(
        f"/api/v1/wallet/{WALLET}/taker/max-coinjoin",
        params={"mixdepth": 2},
        headers=_headers(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["max_selectable"] == 500_000
    assert body["limited_by"] == "balance"
    assert body["immature_value"] == 99_000
    assert body["counterparties"] == 1


def test_max_coinjoin_rejects_bad_mixdepth(client_token: tuple[TestClient, str]) -> None:
    client, token = client_token
    get_daemon_state().wallet_service = _real_wallet({})
    resp = client.get(
        f"/api/v1/wallet/{WALLET}/taker/max-coinjoin",
        params={"mixdepth": 7},
        headers=_headers(token),
    )
    assert resp.status_code == 400


def test_max_coinjoin_requires_auth(client_token: tuple[TestClient, str]) -> None:
    client, _token = client_token
    resp = client.get(f"/api/v1/wallet/{WALLET}/taker/max-coinjoin", params={"mixdepth": 0})
    assert resp.status_code == 401
