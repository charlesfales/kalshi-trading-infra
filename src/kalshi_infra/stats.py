"""Evaluating a binary-contract track record honestly.

Records are dicts with at least ``day`` (ISO date), ``price`` (entry, dollars in (0,1)) and
``pnl_per_contract`` (dollars, net of fees).

* ``block_ci``: trades cluster inside daily regimes, so an IID bootstrap is too narrow.
  Resample whole days.
* ``market_null_pvalue``: the market's own prices are the null. If every trade wins with
  probability equal to its entry price, how likely is at least this many wins?
* ``stake_state``: the fraction of bankroll the record justifies, Kelly evaluated at the
  LOWER bound of the interval. A lucky streak moves the point estimate a lot and the bound
  very little, so the stake rises only as evidence accumulates and cannot chase luck.
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import math
import random
import statistics
from collections.abc import Sequence
from pathlib import Path

Record = dict


def block_ci(rows: Sequence[Record], *, reps: int = 4000, seed: int = 7,
             level: float = 0.95) -> tuple[float, float]:
    """CI on mean net cents/contract, block-bootstrapped by day. (nan, nan) if undefined.

    Fewer than 3 days is not an interval: a bootstrap over one day resamples the same
    block every time and returns [point, point], which looks impossibly tight rather
    than undefined.
    """
    if len(rows) < 8:
        return (math.nan, math.nan)
    byday: dict[str, list[Record]] = collections.defaultdict(list)
    for r in rows:
        byday[r["day"]].append(r)
    days = list(byday)
    if len(days) < 3:
        return (math.nan, math.nan)
    rnd = random.Random(seed)
    n = len(rows)
    out = []
    for _ in range(reps):
        sample: list[Record] = []
        while len(sample) < n:
            sample += byday[rnd.choice(days)]
        out.append(100 * statistics.mean(r["pnl_per_contract"] for r in sample))
    out.sort()
    a = (1 - level) / 2
    return (out[int(a * reps)], out[min(reps - 1, int((1 - a) * reps))])


def market_null_pvalue(prices: Sequence[float], wins: int) -> float:
    """P(X >= wins) when trade i wins with probability prices[i] (Poisson-binomial)."""
    dist = [1.0]
    for p in prices:
        nd = [0.0] * (len(dist) + 1)
        for i, v in enumerate(dist):
            nd[i] += v * (1 - p)
            nd[i + 1] += v * p
        dist = nd
    return sum(dist[wins:])


def stake_state(rows: Sequence[Record], *, cohort: str, kelly_fraction: float = 0.25,
                min_days: int = 8, min_n: int = 60, max_fraction: float = 0.10,
                as_of: str | None = None, reps: int = 4000) -> dict:
    """The bankroll fraction per trade the record can justify.

    ``f* = edge / (1 - mean_price)`` is the Kelly fraction for a binary bought at
    ``mean_price``; it is evaluated at the CI lower bound and scaled by ``kelly_fraction``.
    Every early exit returns ``fraction: 0.0`` with a reason. No path guesses a size.
    """
    ok = [r for r in rows if r.get("day")]
    days = sorted({r["day"] for r in ok})
    prices = [r["price"] for r in ok if r.get("price")]
    mean_price = statistics.mean(prices) if prices else 0.0
    net_c = 100 * statistics.mean(r["pnl_per_contract"] for r in ok) if ok else 0.0
    out = {"cohort": cohort,
           "computed_at": as_of or dt.datetime.now(dt.UTC).isoformat(),
           "n": len(ok), "days": len(days), "net_c": round(net_c, 4),
           "ci_lo": None, "ci_hi": None, "mean_price": round(mean_price, 6),
           "kelly_full": 0.0, "kelly_fraction": kelly_fraction, "fraction": 0.0,
           "capped": False, "reason": "ok"}
    if len(days) < min_days:
        out["reason"] = f"days {len(days)} < {min_days}"
        return out
    if len(ok) < min_n:
        out["reason"] = f"n {len(ok)} < {min_n}"
        return out
    lo, hi = block_ci(ok, reps=reps)
    if math.isnan(lo):
        out["reason"] = "no interval"
        return out
    out["ci_lo"], out["ci_hi"] = round(lo, 4), round(hi, 4)
    if not 0 < mean_price < 1:
        out["reason"] = f"mean_price {mean_price} outside (0,1)"
        return out
    if lo <= 0:
        out["reason"] = "ci_lo <= 0"
        return out
    # Computed from the ROUNDED fields in the dict, so the conclusion can be re-derived
    # from the file itself later. A record whose numbers don't reproduce its own
    # conclusion is an assertion, not a record.
    full = (out["ci_lo"] / 100.0) / (1 - out["mean_price"])
    frac = full * kelly_fraction
    out["kelly_full"] = round(full, 8)
    if frac > max_fraction:
        frac, out["capped"] = max_fraction, True
    out["fraction"] = round(frac, 8)
    return out


def write_atomic(path: Path, state: dict) -> None:
    """Temp file then rename: the reader is another process and must never see half a file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
