"""JoinBlaket: precise taker failure messages, stable codes, max CoinJoin.

The case from 15 Nov 2026 (Umbrel): jar 0 held two deposits (not CoinJoin
outputs), the largest 297,776 sats, and the user asked for more. The taker
refused to merge them (correct, privacy) but appended "CoinJoin requires
UTXOs with at least 5 confirmation(s)", which sent the user waiting for
blocks that changed nothing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from _taker_test_helpers import make_taker_config
from jmwallet.wallet.models import UTXOInfo
from jmwallet.wallet.service import WalletService

from taker.eligibility import (
    FAILURE_MD0_NO_MERGE,
    FAILURE_PODLE_UTXO_TOO_SMALL,
    coinjoin_fee_margin,
    estimate_max_coinjoin,
)
from taker.taker import Taker, TakerState

MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
)


def _utxo(tag: str, value: int, mixdepth: int = 0, *, confirmations: int = 10) -> UTXOInfo:
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


def _wallet(utxos: dict[int, list[UTXOInfo]]) -> WalletService:
    backend = MagicMock()
    backend.get_utxos = AsyncMock(return_value=[])
    ws = WalletService(
        mnemonic=MNEMONIC, backend=backend, network="regtest", mixdepth_count=5, gap_limit=20
    )
    ws.utxo_cache = {md: list(utxos.get(md, [])) for md in range(5)}
    return ws


def _backend() -> AsyncMock:
    backend = AsyncMock()
    backend.can_provide_neutrino_metadata = Mock(return_value=False)
    backend.requires_neutrino_metadata = Mock(return_value=False)
    return backend


def _taker(ws: WalletService) -> Taker:
    config = make_taker_config(
        counterparty_count=1, minimum_makers=1, taker_utxo_age=5, taker_utxo_amtpercent=20
    )
    return Taker(ws, _backend(), config)


@pytest.mark.asyncio
async def test_md0_merge_refusal_has_no_confirmation_hint() -> None:
    ws = _wallet({0: [_utxo("a", 297_776), _utxo("b", 120_000)]})
    taker = _taker(ws)
    taker.directory_client.fetch_orderbook = AsyncMock(return_value=[])

    result = await taker.do_coinjoin(amount=354_414, destination="INTERNAL", mixdepth=0)

    assert result is None
    assert taker.state == TakerState.FAILED
    reason = taker.last_failure_reason or ""
    assert "Cannot merge non-CJ md0 UTXOs for privacy reasons" in reason
    assert "confirmation" not in reason
    assert ".." not in reason
    assert taker.last_failure_code == FAILURE_MD0_NO_MERGE
    params = taker.last_failure_params
    assert params["largest_utxo"] == 297_776
    assert params["needed"] == 354_414
    assert params["max_amount"] == 297_776
    assert params["confirmations_hint"] is False
    taker.directory_client.fetch_orderbook.assert_not_called()


@pytest.mark.asyncio
async def test_hint_kept_when_young_coins_would_cover_the_amount() -> None:
    # Jar 1 merges freely; 100k is confirmed, 300k still young: waiting helps.
    ws = _wallet({1: [_utxo("a", 100_000, 1), _utxo("b", 300_000, 1, confirmations=2)]})
    taker = _taker(ws)
    reason = await taker.check_utxo_eligibility(350_000, 1)
    assert reason is not None
    assert "5 confirmation(s) (taker_utxo_age setting)" in reason
    failure = await taker._utxo_eligibility_failure(350_000, 1)
    assert failure is not None
    assert failure.code == "unconfirmed_funds"
    assert failure.params["confirmations_hint"] is True


@pytest.mark.asyncio
async def test_md0_hint_only_if_a_young_coin_alone_is_enough() -> None:
    ws = _wallet({0: [_utxo("a", 100_000), _utxo("b", 400_000, confirmations=1)]})
    taker = _taker(ws)
    failure = await taker._utxo_eligibility_failure(350_000, 0)
    assert failure is not None
    assert failure.code == FAILURE_MD0_NO_MERGE
    assert failure.params["confirmations_hint"] is True
    assert "taker_utxo_age" in failure.message


@pytest.mark.asyncio
async def test_failure_code_cleared_on_next_round() -> None:
    ws = _wallet({0: [_utxo("a", 297_776), _utxo("b", 120_000)]})
    taker = _taker(ws)
    await taker.do_coinjoin(amount=354_414, destination="INTERNAL", mixdepth=0)
    assert taker.last_failure_code == FAILURE_MD0_NO_MERGE
    ws.utxo_cache[0] = []
    await taker.do_coinjoin(amount=1_000, destination="INTERNAL", mixdepth=0)
    assert taker.last_failure_code == "no_eligible_utxos"
    assert "largest_utxo" not in taker.last_failure_params


def test_max_coinjoin_md0_is_largest_single_minus_fees() -> None:
    ws = _wallet({0: [_utxo("a", 297_776), _utxo("b", 120_000)]})
    est = estimate_max_coinjoin(
        ws,
        0,
        min_confirmations=5,
        amtpercent=20,
        counterparties=1,
        fee_rate_sat_vb=2.0,
        max_abs_fee=500,
        max_rel_fee=0.001,
    )
    assert est.max_selectable == 297_776
    assert est.limited_by == FAILURE_MD0_NO_MERGE
    assert est.merge_restricted is True
    assert est.estimated_tx_fee > 0
    expected = coinjoin_fee_margin(
        297_776, counterparties=1, tx_fee=est.estimated_tx_fee, max_abs_fee=500, max_rel_fee=0.001
    )
    assert est.max_sendable == expected
    # Amount + tx fee + worst-case maker fee still fits the single UTXO.
    worst_maker_fee = max(500, int(0.001 * est.max_sendable) + 1)
    assert est.max_sendable + est.estimated_tx_fee + worst_maker_fee <= 297_776 + 1
    # The taker's own preflight accepts it and rejects one sat above max_selectable.
    ws.select_utxos(0, est.max_sendable, 5)
    with pytest.raises(ValueError):
        ws.select_utxos(0, est.max_selectable + 1, 5)


def test_max_coinjoin_podle_cap() -> None:
    # Jar 2 merges, but the PoDLE UTXO must be >= 20% of the amount.
    ws = _wallet({2: [_utxo(c, 100_000, 2) for c in "abcdefgh"]})
    est = estimate_max_coinjoin(
        ws,
        2,
        min_confirmations=5,
        amtpercent=20,
        counterparties=1,
        fee_rate_sat_vb=1.0,
        max_abs_fee=0,
        max_rel_fee=0.0,
    )
    assert est.max_selectable == 500_000
    assert est.limited_by == FAILURE_PODLE_UTXO_TOO_SMALL


def test_max_coinjoin_counts_immature() -> None:
    ws = _wallet({1: [_utxo("a", 50_000, 1), _utxo("b", 70_000, 1, confirmations=1)]})
    est = estimate_max_coinjoin(
        ws,
        1,
        min_confirmations=5,
        amtpercent=20,
        counterparties=2,
        fee_rate_sat_vb=1.0,
        max_abs_fee=100,
        max_rel_fee=0.0,
    )
    assert est.max_selectable == 50_000
    assert est.immature_value == 70_000
    assert est.limited_by == "balance"
    assert est.max_sendable == 50_000 - est.estimated_tx_fee - 200


def test_fee_margin_takes_the_stricter_limit() -> None:
    # rel dominates: X + 1000 + 2 * 0.01 X <= 101000  ->  X = 100000 / 1.02
    assert (
        coinjoin_fee_margin(
            101_000, counterparties=2, tx_fee=1_000, max_abs_fee=10, max_rel_fee=0.01
        )
        == 98_039
    )
    # abs dominates
    assert (
        coinjoin_fee_margin(
            101_000, counterparties=2, tx_fee=1_000, max_abs_fee=5_000, max_rel_fee=0.0
        )
        == 90_000
    )
    assert (
        coinjoin_fee_margin(500, counterparties=1, tx_fee=1_000, max_abs_fee=0, max_rel_fee=0) == 0
    )
