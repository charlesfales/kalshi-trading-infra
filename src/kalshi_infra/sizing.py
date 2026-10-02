"""Stake sizing.

Two layers:

1. ``stake_for``: proportional sizing, ``clamp(balance / divisor, floor, cap)``. A fixed
   dollar stake is dominated: it fails to fund trades after a drawdown and under-bets after
   a run-up. The cap is a capacity ceiling (thin books cannot absorb unlimited size), not a
   risk preference.

2. ``read_stake_fraction``: reads the fraction the track record justifies (see
   ``stats.stake_state``) from a state file written by another process. Every path that is
   not a fresh, well-formed file returns 0.0 with a reason, which callers read as "minimum
   size". A dead or stale writer is exactly when its last opinion is most dangerous.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

#: A loss costs the stake plus fees. Require this much headroom before funding a trade.
FUNDING_HEADROOM = 1.10


def stake_for(
    balance_usd: float,
    *,
    divisor: float,
    floor_usd: float,
    cap_usd: float,
    low_balance_threshold: float | None = None,
    low_divisor: float | None = None,
) -> float:
    """Dollars to risk on the next trade, or 0.0 when the balance can't fund one.

    ``low_balance_threshold``/``low_divisor`` form a drawdown tier: at or below the
    threshold the stake is sized on the larger divisor. BOTH must be set or the tier is
    off; a half-configured tier must never silently resize the money path.
    """
    if divisor <= 0:
        raise ValueError(f"divisor must be > 0, got {divisor}")
    if low_divisor is not None and low_divisor <= 0:
        raise ValueError(f"low_divisor must be > 0, got {low_divisor}")
    if floor_usd > cap_usd:
        raise ValueError(f"floor {floor_usd} exceeds cap {cap_usd}")
    if (low_balance_threshold is not None and low_divisor is not None
            and balance_usd <= low_balance_threshold):
        divisor = low_divisor
    if balance_usd <= 0:
        return 0.0
    # Compare money at cent precision: 25.0 * 1.10 is 27.500000000000004 in binary float,
    # which would otherwise refuse to fund a balance of exactly $27.50.
    if balance_usd < round(floor_usd * FUNDING_HEADROOM, 2):
        return 0.0
    stake = max(floor_usd, min(cap_usd, balance_usd / divisor))
    return round(stake, 2)


def contracts_for(stake_usd: float, price: float, *, max_contracts: int) -> int:
    """Whole contracts a stake buys at ``price`` (dollars), at least 1, at most the ceiling."""
    if not 0 < price < 1:
        raise ValueError(f"price must be in (0, 1), got {price}")
    # Integer cents: in binary float 10 // 0.40 is 24, not 25.
    n = round(stake_usd * 100) // round(price * 100)
    return max(1, min(n, max_contracts))


def read_stake_fraction(
    path: Path, *, max_stale_hours: float, cohort: str | None = None,
    now: datetime | None = None, future_slack_s: float = 60.0,
) -> tuple[float, str]:
    """(fraction of bankroll, reason). Fails closed to 0.0 on every irregularity."""
    try:
        st = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return 0.0, "state file missing"
    except (OSError, ValueError) as e:
        return 0.0, f"state unreadable: {type(e).__name__}"
    if not isinstance(st, dict):
        return 0.0, "state is not an object"
    # A rule sizes only from its OWN record, never from a neighbouring rule's bound.
    if cohort is not None and st.get("cohort") != cohort:
        return 0.0, f"state cohort {st.get('cohort')!r} is not {cohort!r}"
    frac = st.get("fraction")
    if isinstance(frac, bool) or not isinstance(frac, (int, float)):
        return 0.0, "fraction is not a number"
    if not 0.0 <= float(frac) <= 1.0:
        return 0.0, f"fraction {frac} outside [0, 1]"
    # Freshness is checked BEFORE the fraction is trusted, so a stale file carrying a
    # good-looking number cannot size a trade.
    try:
        when = datetime.fromisoformat(str(st.get("computed_at")))
    except (TypeError, ValueError):
        return 0.0, "state has no usable computed_at"
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    age = ((now or datetime.now(UTC)) - when).total_seconds()
    if age < -future_slack_s:
        return 0.0, f"state is {-age:.0f}s in the future"
    if max_stale_hours > 0 and age > max_stale_hours * 3600:
        return 0.0, f"state is stale by {age / 3600:.1f}h"
    if float(frac) <= 0:
        return 0.0, str(st.get("reason") or "fraction is 0")
    return float(frac), str(st.get("reason") or "ok")
