"""
Pre-flight UTXO eligibility checks for the taker.

These helpers let the taker decide, *before* connecting to directory servers
and fetching the orderbook, whether a mixdepth can actually fund a CoinJoin.
This avoids making the user wait 5-10 minutes for network operations only to
fail with "No eligible UTXOs in mixdepth" at the end (issue #528).

The classification mirrors the filters applied later in ``do_coinjoin`` /
``CoinSelectionMixin.select_utxos`` so the pre-flight verdict matches the real
selection outcome:

- minimum confirmations (``taker_utxo_age``),
- frozen UTXOs (never auto-selected),
- fidelity bonds (never CoinJoin inputs, expired or not),
- inputs locked by another in-flight CoinJoin round,
- the mixdepth-0 merge restriction (handled by a dry-run of ``select_utxos``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from jmwallet.wallet.models import UTXOInfo

# Kept as a stable prefix so downstream consumers (e.g. the tumbler's
# ``_LOW_CONFIRMATION_HINTS``) can recognise confirmation-related failures and
# wait for more confirmations before retrying.
NO_ELIGIBLE_PREFIX = "No eligible UTXOs in mixdepth"


@dataclass
class EligibilityBreakdown:
    """Classification of a mixdepth's UTXOs for automatic CoinJoin selection."""

    mixdepth: int
    min_confirmations: int
    eligible: list[UTXOInfo] = field(default_factory=list)
    immature: list[UTXOInfo] = field(default_factory=list)
    frozen: list[UTXOInfo] = field(default_factory=list)
    locked_bonds: list[UTXOInfo] = field(default_factory=list)
    unlocked_bonds: list[UTXOInfo] = field(default_factory=list)
    reserved: list[UTXOInfo] = field(default_factory=list)

    @property
    def total(self) -> int:
        """Total number of UTXOs considered (across all categories)."""
        return (
            len(self.eligible)
            + len(self.immature)
            + len(self.frozen)
            + len(self.locked_bonds)
            + len(self.unlocked_bonds)
            + len(self.reserved)
        )

    @property
    def eligible_value(self) -> int:
        """Summed value of the eligible UTXOs in satoshis."""
        return sum(u.value for u in self.eligible)

    def no_eligible_reason(self) -> str:
        """Build a human-readable reason explaining why nothing is eligible.

        The message starts with :data:`NO_ELIGIBLE_PREFIX` so confirmation-aware
        retry logic keeps working.
        """
        parts: list[str] = []
        if self.immature:
            parts.append(
                f"{len(self.immature)} below {self.min_confirmations} "
                f"confirmation(s) (taker_utxo_age)"
            )
        if self.frozen:
            parts.append(f"{len(self.frozen)} frozen")
        if self.locked_bonds:
            parts.append(f"{len(self.locked_bonds)} time-locked fidelity bond(s)")
        if self.unlocked_bonds:
            parts.append(f"{len(self.unlocked_bonds)} fidelity bond(s) (not auto-spent)")
        if self.reserved:
            parts.append(f"{len(self.reserved)} locked by another in-flight CoinJoin")

        if not self.total:
            return f"{NO_ELIGIBLE_PREFIX} {self.mixdepth} (no UTXOs present)"
        detail = ", ".join(parts) if parts else "none selectable"
        return f"{NO_ELIGIBLE_PREFIX} {self.mixdepth} ({self.total} UTXOs: {detail})"


def classify_utxos(
    utxos: list[UTXOInfo],
    mixdepth: int,
    min_confirmations: int,
    reserved_outpoints: set[tuple[str, int]] | None = None,
) -> EligibilityBreakdown:
    """Classify a mixdepth's UTXOs into eligibility categories.

    Mirrors the auto-selection filters used by ``select_utxos``/``get_all_utxos``.
    A UTXO is *eligible* only if it is confirmed enough, not frozen, not a
    fidelity bond, and not locked by another in-flight round. The remaining
    categories explain *why* an otherwise present UTXO is unavailable, so the
    caller can produce an actionable error message.
    """
    reserved_outpoints = reserved_outpoints or set()
    result = EligibilityBreakdown(mixdepth=mixdepth, min_confirmations=min_confirmations)

    for utxo in utxos:
        if (utxo.txid, utxo.vout) in reserved_outpoints:
            result.reserved.append(utxo)
        elif utxo.frozen:
            result.frozen.append(utxo)
        elif utxo.is_fidelity_bond:
            if utxo.is_locked:
                result.locked_bonds.append(utxo)
            else:
                result.unlocked_bonds.append(utxo)
        elif utxo.confirmations < min_confirmations:
            result.immature.append(utxo)
        else:
            result.eligible.append(utxo)

    return result


def selectable_for_interactive(
    utxos: list[UTXOInfo],
    min_confirmations: int,
    excluded_outpoints: set[tuple[str, int]] | None = None,
) -> list[UTXOInfo]:
    """Return UTXOs a user may pick in the interactive CoinJoin selector.

    The interactive selector (``--select-utxos``) shows frozen, locked and
    fidelity bond UTXOs but renders them unselectable. Expired bonds are
    excluded too: they can only be redeemed with a direct send, never used as
    CoinJoin inputs. ``excluded_outpoints`` keeps in-flight CoinJoin inputs
    visible while preventing their reuse.
    """
    excluded_outpoints = excluded_outpoints or set()
    return [
        u
        for u in utxos
        if (u.confirmations >= min_confirmations)
        and not u.frozen
        and not u.is_fidelity_bond
        and (u.txid, u.vout) not in excluded_outpoints
    ]


def fidelity_bond_inputs(utxos: list[UTXOInfo]) -> list[UTXOInfo]:
    """Return the UTXOs that must never be CoinJoin inputs.

    Fidelity bonds (any UTXO carrying a locktime) and P2WSH outputs (the only
    P2WSH scripts a BIP84 taker wallet holds are bonds) are rejected whether or
    not the timelock has expired: the inputs would be disclosed to makers and
    then refused at signing time. Mirrors the signer guard in
    ``CoinJoinSession._sign_our_inputs``.
    """
    return [u for u in utxos if u.is_fidelity_bond or u.is_p2wsh]


def podle_threshold_met(
    utxos: list[UTXOInfo], cj_amount: int, min_confirmations: int, min_percent: int
) -> bool:
    """Check whether any eligible UTXO can back a PoDLE commitment.

    A CoinJoin requires a commitment from a UTXO worth at least
    ``min_percent`` of ``cj_amount`` with at least ``min_confirmations``. If no
    such UTXO exists the round fails deterministically, so this lets us reject
    early with a precise message.
    """
    min_value = int(cj_amount * min_percent / 100)
    return any(u.confirmations >= min_confirmations and u.value >= min_value for u in utxos)


# ---------------------------------------------------------------------------
# JoinBlaket: stable failure codes and the largest amount one CoinJoin can send
# ---------------------------------------------------------------------------

#: Nothing in the mixdepth can be a CoinJoin input (immature, frozen, bonds...).
FAILURE_NO_ELIGIBLE_UTXOS = "no_eligible_utxos"
#: Enough coins, but some are below ``taker_utxo_age`` confirmations.
FAILURE_UNCONFIRMED_FUNDS = "unconfirmed_funds"
#: The mixdepth does not hold enough eligible coins.
FAILURE_INSUFFICIENT_FUNDS = "insufficient_funds"
#: Mixdepth 0 never merges UTXOs that do not come from a CoinJoin (privacy):
#: one CoinJoin can spend at most the largest single such UTXO.
FAILURE_MD0_NO_MERGE = "md0_no_merge"
#: No eligible UTXO is worth ``taker_utxo_amtpercent`` of the amount (PoDLE).
FAILURE_PODLE_UTXO_TOO_SMALL = "podle_utxo_too_small"
#: The orderbook has fewer offers than ``minimum_makers``.
FAILURE_NOT_ENOUGH_OFFERS = "not_enough_offers"
#: Fewer acceptable makers (fee limits, bonds) than ``minimum_makers``.
FAILURE_NOT_ENOUGH_MAKERS = "not_enough_makers"
#: Any other failure: only the message explains it.
FAILURE_OTHER = "other"


@dataclass
class TakerFailure:
    """A failure reason with a stable ``code`` and its ``params``."""

    message: str
    code: str = FAILURE_OTHER
    params: dict[str, int | str | bool] = field(default_factory=dict)


@dataclass
class MaxCoinjoinEstimate:
    """Largest amount one (non-sweep) CoinJoin can send from a mixdepth.

    ``max_selectable`` is what the taker's coin selection can fund at all (the
    same rules: unfrozen, ``taker_utxo_age`` confirmations, no fidelity bonds,
    no inputs of another round, the mixdepth-0 no-merge rule, PoDLE size).
    Asking for more always fails. ``max_sendable`` also leaves room for the
    mining fee and the makers' fees (bounded by the user's fee limits), which
    the taker adds to the amount before choosing its inputs.
    """

    mixdepth: int
    counterparties: int
    min_confirmations: int
    max_selectable: int
    max_sendable: int
    limited_by: str
    merge_restricted: bool
    eligible_count: int
    eligible_value: int
    largest_utxo: int
    mergeable_pool: int
    immature_value: int
    fee_rate_sat_vb: float
    estimated_tx_fee: int
    max_maker_fee_abs: int
    max_maker_fee_rel: float

    def as_dict(self) -> dict[str, int | float | str | bool]:
        return dict(self.__dict__)


def coinjoin_fee_margin(
    amount_cap: int,
    *,
    counterparties: int,
    tx_fee: int,
    max_abs_fee: int,
    max_rel_fee: float,
) -> int:
    """Largest amount X with X + tx_fee + n * max(abs, rel * X) <= amount_cap."""
    room = amount_cap - tx_fee
    if room <= 0:
        return 0
    n = max(1, counterparties)
    by_abs = room - n * max(0, max_abs_fee)
    by_rel = math.floor(room / (1 + n * max(0.0, max_rel_fee)))
    return max(0, min(by_abs, by_rel))


def estimate_max_coinjoin(
    wallet: Any,
    mixdepth: int,
    *,
    min_confirmations: int,
    amtpercent: int,
    counterparties: int,
    fee_rate_sat_vb: float,
    max_abs_fee: int,
    max_rel_fee: float,
    reserved_outpoints: set[tuple[str, int]] | None = None,
) -> MaxCoinjoinEstimate:
    """Estimate the largest amount one CoinJoin can send from ``mixdepth``.

    Uses the wallet's own ``max_selectable_amount`` (the rules of
    ``select_utxos``, which the taker calls) plus the PoDLE size rule. The fee
    margin uses the transaction shape the taker budgets for when choosing its
    inputs, with one extra input per maker as a safety margin.
    """
    from jmcore.fee_policy import estimate_p2wpkh_vsize

    reserved = reserved_outpoints or set()
    summary = wallet.max_selectable_amount(mixdepth, min_confirmations, exclude=reserved)
    breakdown = classify_utxos(
        wallet.utxo_cache.get(mixdepth, []), mixdepth, min_confirmations, reserved
    )
    max_selectable = summary.max_amount
    limited_by = "balance"
    if summary.merge_restricted and max_selectable < summary.eligible_value:
        limited_by = FAILURE_MD0_NO_MERGE
    if amtpercent > 0 and summary.largest_utxo > 0:
        podle_cap = summary.largest_utxo * 100 // amtpercent
        if podle_cap < max_selectable:
            max_selectable = podle_cap
            limited_by = FAILURE_PODLE_UTXO_TOO_SMALL
    if summary.eligible_count == 0:
        limited_by = FAILURE_NO_ELIGIBLE_UTXOS

    n = max(1, counterparties)
    vsize = estimate_p2wpkh_vsize(2 + 3 * n, 2 + 2 * n)
    tx_fee = math.ceil(vsize * fee_rate_sat_vb)
    max_sendable = coinjoin_fee_margin(
        max_selectable,
        counterparties=n,
        tx_fee=tx_fee,
        max_abs_fee=max_abs_fee,
        max_rel_fee=max_rel_fee,
    )
    return MaxCoinjoinEstimate(
        mixdepth=mixdepth,
        counterparties=n,
        min_confirmations=min_confirmations,
        max_selectable=max_selectable,
        max_sendable=max_sendable,
        limited_by=limited_by,
        merge_restricted=summary.merge_restricted,
        eligible_count=summary.eligible_count,
        eligible_value=summary.eligible_value,
        largest_utxo=summary.largest_utxo,
        mergeable_pool=summary.mergeable_pool,
        immature_value=sum(u.value for u in breakdown.immature),
        fee_rate_sat_vb=round(fee_rate_sat_vb, 3),
        estimated_tx_fee=tx_fee,
        max_maker_fee_abs=max_abs_fee,
        max_maker_fee_rel=max_rel_fee,
    )
