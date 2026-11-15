"""JoinBlaket: coded coin-selection errors and ``max_selectable_amount``.

``max_selectable_amount`` must agree exactly with ``select_utxos``: the
selection succeeds for the maximum and fails one satoshi above it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from jmwallet.wallet.coin_selection import CoinSelectionError
from jmwallet.wallet.models import UTXOInfo
from jmwallet.wallet.service import WalletService


def _utxo(
    tag: str,
    value: int,
    mixdepth: int,
    *,
    confirmations: int = 10,
    coinjoin_output: bool = False,
    frozen: bool = False,
) -> UTXOInfo:
    utxo = UTXOInfo(
        txid=tag * 64,
        vout=0,
        value=value,
        address=f"bcrt1{tag}",
        confirmations=confirmations,
        scriptpubkey="0014" + tag * 40,
        path=f"m/84'/0'/{mixdepth}'/0/0",
        mixdepth=mixdepth,
        frozen=frozen,
    )
    utxo.coinjoin_output = coinjoin_output
    return utxo


@pytest.fixture
def wallet(test_mnemonic: str) -> WalletService:
    backend = MagicMock()
    backend.get_utxos = AsyncMock(return_value=[])
    return WalletService(
        mnemonic=test_mnemonic,
        backend=backend,
        network="regtest",
        mixdepth_count=5,
        gap_limit=20,
    )


def _assert_exact(wallet: WalletService, mixdepth: int, min_conf: int) -> int:
    summary = wallet.max_selectable_amount(mixdepth, min_conf)
    wallet.select_utxos(mixdepth, summary.max_amount, min_conf)
    with pytest.raises(CoinSelectionError):
        wallet.select_utxos(mixdepth, summary.max_amount + 1, min_conf)
    return summary.max_amount


def test_md0_two_deposits_max_is_largest_single(wallet: WalletService) -> None:
    # The real case from 15 Nov 2026: two non-CoinJoin deposits in jar 0.
    wallet.utxo_cache = {0: [_utxo("a", 297_776, 0), _utxo("b", 120_000, 0)]}
    assert _assert_exact(wallet, 0, 5) == 297_776
    summary = wallet.max_selectable_amount(0, 5)
    assert summary.merge_restricted is True
    assert summary.eligible_value == 417_776
    assert summary.largest_non_mergeable == 297_776


def test_md0_coinjoin_outputs_merge(wallet: WalletService) -> None:
    wallet.utxo_cache = {
        0: [
            _utxo("a", 100_000, 0, coinjoin_output=True),
            _utxo("b", 90_000, 0, coinjoin_output=True),
            _utxo("c", 150_000, 0),
        ]
    }
    # The CoinJoin outputs merge (190k), the deposit stays single (150k).
    assert _assert_exact(wallet, 0, 5) == 190_000


def test_other_mixdepths_merge_everything_eligible(wallet: WalletService) -> None:
    wallet.utxo_cache = {
        2: [
            _utxo("a", 100_000, 2),
            _utxo("b", 50_000, 2),
            _utxo("c", 40_000, 2, confirmations=2),  # too young
            _utxo("d", 30_000, 2, frozen=True),
        ]
    }
    assert _assert_exact(wallet, 2, 5) == 150_000


def test_empty_mixdepth(wallet: WalletService) -> None:
    wallet.utxo_cache = {0: []}
    summary = wallet.max_selectable_amount(0, 5)
    assert summary.max_amount == 0
    assert summary.eligible_count == 0


def test_md0_error_code_and_params(wallet: WalletService) -> None:
    wallet.utxo_cache = {0: [_utxo("a", 297_776, 0), _utxo("b", 120_000, 0)]}
    with pytest.raises(CoinSelectionError) as info:
        wallet.select_utxos(0, 354_414, 5)
    assert info.value.code == "md0_no_merge"
    assert info.value.params["largest_utxo"] == 297_776
    assert info.value.params["needed"] == 354_414
    assert info.value.params["max_amount"] == 297_776
    # Still the upstream message (CLI users and log readers rely on it).
    assert "Cannot merge non-CJ md0 UTXOs for privacy reasons" in str(info.value)


def test_unconfirmed_and_insufficient_codes(wallet: WalletService) -> None:
    wallet.utxo_cache = {
        1: [_utxo("a", 100_000, 1), _utxo("b", 80_000, 1, confirmations=1)],
    }
    with pytest.raises(CoinSelectionError) as info:
        wallet.select_utxos(1, 150_000, 5)
    assert info.value.code == "unconfirmed_funds"
    assert info.value.params["unconfirmed"] == 80_000
    with pytest.raises(CoinSelectionError) as info:
        wallet.select_utxos(1, 500_000, 0)
    assert info.value.code == "insufficient_funds"
    assert isinstance(info.value, ValueError)
