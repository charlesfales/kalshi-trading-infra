"""Event replay: run the SAME decision code the live bot runs over recorded data.

A backtest that reimplements the strategy is a second strategy. Here a ``Strategy`` is
one object with one ``decide`` method, called by the live loop and by ``Replayer`` alike.

Look-ahead is prevented by construction:

* Events are ordered by when WE RECEIVED them (``ts_recv``), never by the exchange's
  stamp. The decision at time t may only use what had arrived by t.
* The strategy sees a ``View``. Its series accessor answers "latest value at or before
  now" and nothing else, so there is no API through which a future value can leak.
* Orders are filled against the book as it stood ``latency_s`` AFTER the decision, not
  at the decision, and only up to the limit (IOC). Unfilled size is simply gone.

``estimate_lead_lag`` audits the recording itself: if two feeds that should move
together are offset, one recorder's clock is wrong, and a backtest joining them on
timestamps is quietly reading the future.
"""
from __future__ import annotations

import bisect
import heapq
import math
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from .book import BookManager


@dataclass(frozen=True, order=True)
class Event:
    ts_recv: float                                   # seconds; when it reached us
    kind: str = field(compare=False)                 # "ws" | "series" | anything else
    payload: Any = field(compare=False, default=None)


def merge_streams(*streams: Iterable[Event]) -> Iterator[Event]:
    """Merge already-sorted streams into one, ordered by receive time."""
    return heapq.merge(*streams)


class Series:
    """A timestamped scalar series readable only as-of a time."""

    def __init__(self) -> None:
        self._ts: list[float] = []
        self._v: list[float] = []

    def append(self, ts: float, v: float) -> None:
        if self._ts and ts < self._ts[-1]:
            raise ValueError("series must be appended in time order")
        self._ts.append(ts)
        self._v.append(v)

    def asof(self, now: float) -> tuple[float, float] | None:
        """(ts, value) of the latest point at or before ``now``."""
        i = bisect.bisect_right(self._ts, now) - 1
        return None if i < 0 else (self._ts[i], self._v[i])


@dataclass
class View:
    """Everything a strategy may look at, frozen at ``now``."""
    now: float
    books: BookManager
    series: Mapping[str, Series]

    def latest(self, name: str) -> tuple[float, float] | None:
        s = self.series.get(name)
        return None if s is None else s.asof(self.now)


@dataclass(frozen=True)
class Intent:
    ticker: str
    side: str            # "yes" | "no"
    limit: Decimal       # worst price willing to pay
    contracts: Decimal


class Strategy(Protocol):
    def decide(self, view: View) -> Intent | None: ...


@dataclass(frozen=True)
class SimFill:
    decided_at: float
    filled_at: float
    intent: Intent
    filled: Decimal
    avg_price: Decimal | None


class Replayer:
    def __init__(self, strategy: Strategy, *, latency_s: float = 0.25,
                 on_event: Callable[[Event, View], None] | None = None) -> None:
        self.strategy = strategy
        self.latency_s = latency_s
        self.books = BookManager()
        self.series: dict[str, Series] = {}
        self.fills: list[SimFill] = []
        self._pending: list[tuple[float, float, Intent]] = []   # (fill_at, decided_at, intent)
        self._on_event = on_event

    def _ingest(self, ev: Event) -> None:
        if ev.kind == "ws":
            self.books.on_message(ev.payload)
        elif ev.kind == "series":
            name, value = ev.payload
            self.series.setdefault(name, Series()).append(ev.ts_recv, value)

    def _fill_due(self, now: float) -> None:
        while self._pending and self._pending[0][0] <= now:
            fill_at, decided_at, it = heapq.heappop(self._pending)
            res = self.books.book(it.ticker).sweep_cost(it.side, it.contracts, it.limit)
            filled, cost = res if res is not None else (Decimal(0), Decimal(0))
            self.fills.append(SimFill(decided_at, fill_at, it, filled,
                                      cost / filled if filled else None))

    def run(self, events: Iterable[Event]) -> list[SimFill]:
        last = -math.inf
        for ev in events:
            if ev.ts_recv < last:
                raise ValueError(f"events out of order at {ev.ts_recv} < {last}")
            last = ev.ts_recv
            # Orders due before this event see the book as it stood before it.
            self._fill_due(ev.ts_recv - 1e-9)
            self._ingest(ev)
            view = View(ev.ts_recv, self.books, self.series)
            if self._on_event:
                self._on_event(ev, view)
            intent = self.strategy.decide(view)
            if intent is not None:
                heapq.heappush(self._pending, (ev.ts_recv + self.latency_s, ev.ts_recv, intent))
        self._fill_due(math.inf)
        return self.fills


def settle(fills: Sequence[SimFill], results: Mapping[str, str]) -> list[dict]:
    """P&L per filled order given each market's result ("yes"/"no"); fees excluded."""
    out = []
    for f in fills:
        if not f.filled or f.intent.ticker not in results:
            continue
        won = results[f.intent.ticker] == f.intent.side
        assert f.avg_price is not None
        pnl = f.filled * ((1 - f.avg_price) if won else -f.avg_price)
        out.append({"ticker": f.intent.ticker, "side": f.intent.side,
                    "contracts": f.filled, "price": f.avg_price, "won": won, "pnl": pnl})
    return out


def estimate_lead_lag(a: Sequence[tuple[float, float]], b: Sequence[tuple[float, float]],
                      *, max_lag_s: float = 3.0, step_s: float = 0.05,
                      grid_s: float = 0.25) -> tuple[float, float]:
    """(lag, corr) maximising corr(returns of a at t, returns of b at t + lag).

    A positive lag means ``a`` leads ``b``. Two recordings of feeds that should move
    together but peak at a non-zero lag have a clock problem to fix before any backtest
    joins them on timestamps.
    """
    def resample(xs: Sequence[tuple[float, float]], t0: float, t1: float, shift: float
                 ) -> list[float]:
        ts = [t for t, _ in xs]
        out, t = [], t0
        while t <= t1:
            i = bisect.bisect_right(ts, t + shift) - 1
            out.append(xs[max(i, 0)][1])
            t += grid_s
        return [out[k + 1] - out[k] for k in range(len(out) - 1)]

    def corr(x: list[float], y: list[float]) -> float:
        n = min(len(x), len(y))
        x, y = x[:n], y[:n]
        mx, my = sum(x) / n, sum(y) / n
        sxy = sum((p - mx) * (q - my) for p, q in zip(x, y, strict=True))
        sx = math.sqrt(sum((p - mx) ** 2 for p in x))
        sy = math.sqrt(sum((q - my) ** 2 for q in y))
        return sxy / (sx * sy) if sx and sy else 0.0

    t0 = max(a[0][0], b[0][0]) + max_lag_s
    t1 = min(a[-1][0], b[-1][0]) - max_lag_s
    if t1 <= t0:
        raise ValueError("series overlap too short for the requested lag window")
    ra = resample(a, t0, t1, 0.0)
    best = (0.0, -math.inf)
    k = -max_lag_s
    while k <= max_lag_s + 1e-12:
        c = corr(ra, resample(b, t0, t1, k))
        if c > best[1]:
            best = (round(k, 6), c)
        k += step_s
    return best
