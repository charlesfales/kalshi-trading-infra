"""End-to-end example: one strategy object, replayed offline and wired for live.

The strategy here is a deliberately simple placeholder: it buys whichever side is
cheaper than an externally supplied fair value by more than a threshold. It exists to
show the plumbing (book, risk gates, sizing, replay, settlement, scoring), not an edge.
In this synthetic world it is handed the TRUE fair value, so it has an edge by
construction, which makes it a check that the harness can find one.

    python examples/example_bot.py          # replays a synthetic day and scores it
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from kalshi_infra.prereg import Candidate, score
from kalshi_infra.replay import Event, Intent, Replayer, View, merge_streams, settle
from kalshi_infra.risk import RiskController, RiskLimits
from kalshi_infra.sizing import contracts_for, stake_for

FEE_RATE = Decimal("0.07")


@dataclass
class FairValueDemo:
    """Placeholder signal: buy when the ask is below fair value by ``threshold``."""
    ticker: str
    threshold: Decimal = Decimal("0.05")
    contracts: Decimal = Decimal("1")
    fired: bool = False

    def decide(self, view: View) -> Intent | None:
        if self.fired:
            return None                                   # one fill per market
        fair = view.latest("fair:" + self.ticker)
        book = view.books.books.get(self.ticker)
        if fair is None or book is None:
            return None
        p_yes = Decimal(str(fair[1]))
        for side, p in (("yes", p_yes), ("no", 1 - p_yes)):
            ask = book.best_ask(side)
            if ask is not None and p - ask > self.threshold:
                self.fired = True
                return Intent(self.ticker, side, limit=ask, contracts=self.contracts)
        return None


@dataclass
class Router:
    """Fans one event out to one strategy instance per market."""
    strategies: list[FairValueDemo]

    def decide(self, view: View) -> Intent | None:
        for s in self.strategies:
            it = s.decide(view)
            if it is not None:
                return it
        return None


def synthetic_market(ticker: str, sid: int, rng: random.Random, t0: float
                     ) -> tuple[list[Event], str]:
    """A binary market whose book is quoted noisily around a drifting fair value."""
    fair, events, seq = 0.5, [], 0
    snap = {"market_ticker": ticker,
            "yes_dollars_fp": [["0.4800", "100.00"]], "no_dollars_fp": [["0.4800", "100.00"]]}
    events.append(Event(t0, "ws", {"type": "orderbook_snapshot", "sid": sid, "seq": seq,
                                   "msg": snap}))
    yes_bid, no_bid = 0.48, 0.48
    for k in range(1, 600):
        t = t0 + k
        fair = min(0.97, max(0.03, fair + rng.gauss(0, 0.01)))
        events.append(Event(t, "series", ("fair:" + ticker, fair)))
        if k % 5 == 0:
            # Each touch is re-quoted around fair value with independent noise.
            for side, old in (("yes", yes_bid), ("no", no_bid)):
                target = round((fair if side == "yes" else 1 - fair) - 0.02, 2)
                new = round(target + rng.gauss(0, 0.02), 2)
                if new != old and 0.01 <= new <= 0.98:
                    seq += 1
                    events.append(Event(t + 0.5, "ws", {"type": "orderbook_delta", "sid": sid,
                        "seq": seq, "msg": {"market_ticker": ticker, "side": side,
                        "price_dollars": f"{old:.4f}", "delta_fp": "-100.00"}}))
                    seq += 1
                    events.append(Event(t + 0.5, "ws", {"type": "orderbook_delta", "sid": sid,
                        "seq": seq, "msg": {"market_ticker": ticker, "side": side,
                        "price_dollars": f"{new:.4f}", "delta_fp": "100.00"}}))
                    if side == "yes":
                        yes_bid = new
                    else:
                        no_bid = new
    result = "yes" if rng.random() < fair else "no"
    return sorted(events), result


def main() -> None:
    rng = random.Random(7)
    rows, d0 = [], 0.0
    for day in range(10):
        streams, results, strategies = [], {}, []
        for m in range(30):
            tk = f"DEMO-D{day:02d}-M{m:02d}"
            ev, res = synthetic_market(tk, m + 1, rng, d0 + m * 1000)
            streams.append(ev)
            results[tk] = res
            strategies.append(FairValueDemo(tk))
        fills = Replayer(Router(strategies), latency_s=0.25).run(merge_streams(*streams))
        for r in settle(fills, results):
            fee = FEE_RATE * r["price"] * (1 - r["price"])
            rows.append({"day": f"2026-01-{day + 1:02d}", "price": float(r["price"]),
                         "pnl_per_contract": float(r["pnl"] / r["contracts"] - fee)})
        d0 += 100_000

    cand = Candidate("FAIR_DEMO", hypothesis="ask below fair by >5c is profitable",
                     mechanism="synthetic book lags a synthetic fair value",
                     falsifier="lower bound at or below zero at n>=60",
                     scored_from="2026-01-01", min_n=60, min_days=8)
    s = score(cand, rows)
    print(f"{len(rows)} fills over {s.days} days: {s.net_c:+.2f}c/contract, "
          f"90% CI [{s.ci_lo:+.2f}, {s.ci_hi:+.2f}] -> {s.verdict.value} ({s.note})")

    # The same objects, wired for live: every order passes the risk gates and sizing.
    risk = RiskController(RiskLimits(live_enabled=False, kill_file=Path("KILL")))
    stake = stake_for(1_000, divisor=30, floor_usd=1, cap_usd=50)
    n = contracts_for(stake, 0.45, max_contracts=500)
    d = risk.check_pre_order(count=n, signal_price_cents=45, current_price_cents=45,
                             feed_age_s=0.2, seconds_to_expiry=120)
    print(f"live wiring check: stake ${stake} -> {n} contracts; risk says "
          f"{'ALLOW' if d.allow else 'REFUSE'} ({d.reason})")


if __name__ == "__main__":
    main()
