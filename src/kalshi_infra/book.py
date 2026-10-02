"""Order books rebuilt from Kalshi's ``orderbook_delta`` WebSocket channel.

Things this gets right that are easy to get wrong:

* **The channel carries BIDS, not asks.** Each side of a binary is a ladder of resting
  bids. The price to BUY yes is ``1 - best NO bid``, and vice versa. Reading the levels
  as asks prices a different instrument entirely.
* **Sequence gaps invalidate the book.** Every message carries a per-subscription
  ``seq``. One missed delta means every level after it may be wrong, so a gap marks
  every book on that subscription invalid until a fresh snapshot arrives. An invalid
  book answers ``None``, never a stale number.
* **Unseeded sides are unknown, not empty.** A snapshot can omit a side. A side that
  was never seeded holds only levels that happened to tick since, so its touch is
  fiction. It reports ``None``.
* **Prices are integer ticks.** Dollar strings are parsed to 1/10,000ths of a dollar so
  that level keys are exact. ``0.1 + 0.2`` must never create two levels.
* **Trust, but verify.** ``verify`` compares the rebuilt book against an independent
  REST read and invalidates it if the touch has drifted. A rebuilt book that has never
  been checked against ground truth is a hypothesis.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal

TICKS_PER_DOLLAR = 10_000
SIDES = ("yes", "no")


def to_ticks(price: str | float | Decimal) -> int:
    t = Decimal(str(price)) * TICKS_PER_DOLLAR
    if t != t.to_integral_value():
        raise ValueError(f"price {price!r} is finer than 1/{TICKS_PER_DOLLAR} dollar")
    return int(t)


def to_dollars(ticks: int) -> Decimal:
    return Decimal(ticks) / TICKS_PER_DOLLAR


def _other(side: str) -> str:
    return "no" if side == "yes" else "yes"


@dataclass
class OrderBook:
    ticker: str
    bids: dict[str, dict[int, Decimal]] = field(
        default_factory=lambda: {"yes": {}, "no": {}})
    seeded: dict[str, bool] = field(default_factory=lambda: {"yes": False, "no": False})
    valid: bool = False
    invalid_reason: str | None = "no snapshot yet"
    last_update_ms: int | None = None

    # -- mutation ----------------------------------------------------------------------
    def apply_snapshot(self, msg: Mapping, *, ts_ms: int | None = None) -> None:
        self.bids = {"yes": {}, "no": {}}
        for side in SIDES:
            ladder = msg.get(f"{side}_dollars_fp")
            self.seeded[side] = ladder is not None
            for px, qty in ladder or []:
                q = Decimal(str(qty))
                if q > 0:
                    self.bids[side][to_ticks(px)] = q
        self.valid, self.invalid_reason = True, None
        self.last_update_ms = ts_ms

    def apply_delta(self, msg: Mapping, *, ts_ms: int | None = None) -> None:
        """``delta_fp`` is a signed change in resting contracts at ``price_dollars``."""
        side = str(msg["side"]).lower()
        if side not in SIDES:
            self.invalidate(f"unknown side {side!r}")
            return
        px = to_ticks(msg["price_dollars"])
        lv = self.bids[side]
        q = lv.get(px, Decimal(0)) + Decimal(str(msg["delta_fp"]))
        if q < 0:
            # A level cannot go negative. Our state disagrees with the exchange's.
            self.invalidate(f"negative size at {side} {to_dollars(px)}")
            return
        if q == 0:
            lv.pop(px, None)
        else:
            lv[px] = q
        self.last_update_ms = ts_ms

    def invalidate(self, reason: str) -> None:
        self.valid, self.invalid_reason = False, reason

    # -- reads (None whenever the answer is not knowable) --------------------------------
    def best_bid(self, side: str) -> Decimal | None:
        if not self.valid or not self.seeded[side] or not self.bids[side]:
            return None
        return to_dollars(max(self.bids[side]))

    def best_ask(self, side: str) -> Decimal | None:
        """Price to buy ``side`` now: 1 minus the best bid on the other side."""
        b = self.best_bid(_other(side))
        return None if b is None else 1 - b

    def mid(self) -> Decimal | None:
        bid, ask = self.best_bid("yes"), self.best_ask("yes")
        return None if bid is None or ask is None else (bid + ask) / 2

    def spread(self) -> Decimal | None:
        bid, ask = self.best_bid("yes"), self.best_ask("yes")
        return None if bid is None or ask is None else ask - bid

    def ask_ladder(self, side: str) -> list[tuple[Decimal, Decimal]] | None:
        """(price, contracts) available to BUY ``side``, cheapest first."""
        other = _other(side)
        if not self.valid or not self.seeded[other]:
            return None
        return sorted((1 - to_dollars(p), q) for p, q in self.bids[other].items())

    def sweep_cost(self, side: str, contracts: Decimal, limit: Decimal
                   ) -> tuple[Decimal, Decimal] | None:
        """(filled, cost) for an IOC buy of ``contracts`` at no worse than ``limit``."""
        ladder = self.ask_ladder(side)
        if ladder is None:
            return None
        filled = cost = Decimal(0)
        for px, q in ladder:
            if px > limit or filled >= contracts:
                break
            take = min(q, contracts - filled)
            filled += take
            cost += take * px
        return filled, cost

    # -- ground truth ---------------------------------------------------------------------
    def verify(self, rest_bids: Mapping[str, Iterable[tuple[str | float, str | float]]],
               *, tolerance: Decimal = Decimal("0")) -> bool:
        """Compare the touch on both sides with a REST read; invalidate on drift."""
        for side in SIDES:
            levels = [(to_ticks(p), Decimal(str(q))) for p, q in rest_bids.get(side, [])]
            truth = to_dollars(max(p for p, q in levels if q > 0)) if any(
                q > 0 for _, q in levels) else None
            mine = self.best_bid(side)
            if (truth is None) != (mine is None) or (
                    truth is not None and mine is not None and abs(truth - mine) > tolerance):
                self.invalidate(f"drift on {side}: rebuilt {mine} vs rest {truth}")
                return False
        return True


class BookManager:
    """Routes WebSocket messages to books and enforces per-subscription sequencing."""

    def __init__(self) -> None:
        self.books: dict[str, OrderBook] = {}
        self._last_seq: dict[int, int] = {}
        self._tickers_by_sid: dict[int, set[str]] = {}
        self.gaps = 0

    def book(self, ticker: str) -> OrderBook:
        return self.books.setdefault(ticker, OrderBook(ticker))

    def on_message(self, m: Mapping) -> str | None:
        """Apply one message. Returns the ticker it touched, if any."""
        typ = m.get("type")
        if typ not in ("orderbook_snapshot", "orderbook_delta"):
            return None
        msg = m.get("msg") or {}
        ticker = msg.get("market_ticker")
        if not ticker:
            return None
        sid, seq = m.get("sid"), m.get("seq")
        ts_ms = m.get("sending_ts_ms") or msg.get("ts_ms")
        if isinstance(sid, int):
            self._tickers_by_sid.setdefault(sid, set()).add(ticker)
            if isinstance(seq, int):
                prev = self._last_seq.get(sid)
                if prev is not None and seq != prev + 1 and typ == "orderbook_delta":
                    self.gaps += 1
                    for t in self._tickers_by_sid[sid]:
                        self.book(t).invalidate(f"seq gap on sid {sid}: {prev} -> {seq}")
                self._last_seq[sid] = seq
        b = self.book(ticker)
        if typ == "orderbook_snapshot":
            b.apply_snapshot(msg, ts_ms=ts_ms)
        elif b.valid:
            b.apply_delta(msg, ts_ms=ts_ms)
        return ticker

    def needs_resnapshot(self) -> list[str]:
        return sorted(t for t, b in self.books.items() if not b.valid)
