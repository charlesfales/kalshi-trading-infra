from __future__ import annotations

import math
import random
from decimal import Decimal as D

import pytest

from kalshi_infra.book import BookManager, OrderBook, to_ticks
from kalshi_infra.replay import (
    Event,
    Intent,
    Replayer,
    Series,
    View,
    estimate_lead_lag,
    merge_streams,
    settle,
)


def snap(ticker="T", sid=1, seq=1, yes=(("0.40", "10"),), no=(("0.55", "20"),)):
    msg = {"market_ticker": ticker}
    if yes is not None:
        msg["yes_dollars_fp"] = [list(x) for x in yes]
    if no is not None:
        msg["no_dollars_fp"] = [list(x) for x in no]
    return {"type": "orderbook_snapshot", "sid": sid, "seq": seq, "msg": msg}


def delta(side, px, d, ticker="T", sid=1, seq=2):
    return {"type": "orderbook_delta", "sid": sid, "seq": seq,
            "msg": {"market_ticker": ticker, "side": side, "price_dollars": px, "delta_fp": d}}


# ---------------------------------------------------------------- book
def test_bids_not_asks():
    bm = BookManager()
    bm.on_message(snap())
    b = bm.book("T")
    assert b.best_bid("yes") == D("0.40")
    assert b.best_ask("yes") == D("0.45")        # 1 - best NO bid
    assert b.best_ask("no") == D("0.60")         # 1 - best YES bid
    assert b.mid() == D("0.425") and b.spread() == D("0.05")


def test_delta_is_a_change_and_zero_removes_level():
    bm = BookManager()
    bm.on_message(snap())
    bm.on_message(delta("yes", "0.42", "5", seq=2))
    assert bm.book("T").best_bid("yes") == D("0.42")
    bm.on_message(delta("yes", "0.42", "-5", seq=3))
    assert bm.book("T").best_bid("yes") == D("0.40")


def test_negative_level_invalidates():
    bm = BookManager()
    bm.on_message(snap())
    bm.on_message(delta("yes", "0.40", "-11", seq=2))
    assert not bm.book("T").valid and bm.book("T").best_ask("no") is None


def test_seq_gap_invalidates_every_book_on_subscription():
    bm = BookManager()
    bm.on_message(snap("A", seq=1))
    bm.on_message(snap("B", seq=2))
    bm.on_message(delta("yes", "0.41", "1", ticker="A", seq=4))     # 3 missing
    assert bm.gaps == 1 and bm.needs_resnapshot() == ["A", "B"]
    bm.on_message(snap("A", seq=5))
    assert bm.needs_resnapshot() == ["B"]


def test_unseeded_side_is_unknown_not_empty():
    bm = BookManager()
    bm.on_message(snap(no=None))
    b = bm.book("T")
    assert b.best_ask("yes") is None             # needs NO bids, never seeded
    assert b.best_ask("no") == D("0.60")


def test_price_ticks_are_exact():
    assert to_ticks("0.1") + to_ticks("0.2") == to_ticks("0.3")
    with pytest.raises(ValueError):
        to_ticks("0.123456")


def test_sweep_respects_limit():
    b = OrderBook("T")
    b.apply_snapshot({"yes_dollars_fp": [["0.40", "10"]],
                      "no_dollars_fp": [["0.55", "5"], ["0.53", "10"]]})
    assert b.ask_ladder("yes") == [(D("0.45"), D("5")), (D("0.47"), D("10"))]
    assert b.sweep_cost("yes", D("8"), D("0.46")) == (D("5"), D("2.25"))
    assert b.sweep_cost("yes", D("8"), D("0.50")) == (D("8"), D("2.25") + 3 * D("0.47"))


def test_verify_against_rest_detects_drift():
    b = OrderBook("T")
    b.apply_snapshot({"yes_dollars_fp": [["0.40", "10"]], "no_dollars_fp": [["0.55", "5"]]})
    assert b.verify({"yes": [("0.40", "10")], "no": [("0.55", "5")]})
    assert not b.verify({"yes": [("0.41", "10")], "no": [("0.55", "5")]})
    assert not b.valid and "drift" in b.invalid_reason


# ---------------------------------------------------------------- replay
def test_series_asof_never_returns_future():
    s = Series()
    s.append(1.0, 10)
    s.append(2.0, 20)
    assert s.asof(0.5) is None and s.asof(1.9) == (1.0, 10) and s.asof(2.0) == (2.0, 20)
    with pytest.raises(ValueError):
        s.append(1.5, 15)


class BuyOnce:
    def __init__(self):
        self.done = False
        self.seen_now = []

    def decide(self, view: View):
        self.seen_now.append(view.now)
        ask = view.books.book("T").best_ask("yes")
        if not self.done and ask is not None:
            self.done = True
            return Intent("T", "yes", limit=ask, contracts=D("8"))
        return None


def test_fill_uses_book_after_latency_not_at_decision():
    events = [
        Event(0.0, "ws", snap(no=(("0.55", "5"),))),
        # 0.1s after the decision, someone takes most of the liquidity at our price.
        Event(0.1, "ws", delta("no", "0.55", "-4", seq=2)),
    ]
    fills = Replayer(BuyOnce(), latency_s=0.25).run(events)
    assert len(fills) == 1 and fills[0].filled == D("1")      # not 5: the book moved


def test_replay_rejects_out_of_order_events():
    with pytest.raises(ValueError):
        Replayer(BuyOnce()).run([Event(1.0, "series", ("x", 1)), Event(0.5, "series", ("x", 2))])


def test_merge_orders_by_receive_time():
    a = [Event(0.0, "series", ("a", 1)), Event(2.0, "series", ("a", 2))]
    b = [Event(1.0, "series", ("b", 1))]
    assert [e.ts_recv for e in merge_streams(a, b)] == [0.0, 1.0, 2.0]


def test_settle():
    events = [Event(0.0, "ws", snap())]
    fills = Replayer(BuyOnce(), latency_s=0.0).run(events)
    (r,) = settle(fills, {"T": "yes"})
    assert r["won"] and r["pnl"] == D("8") * (1 - D("0.45"))
    (r,) = settle(fills, {"T": "no"})
    assert r["pnl"] == -D("8") * D("0.45")


def test_lead_lag_recovers_clock_offset():
    rng = random.Random(1)
    x, pts = 0.0, []
    for i in range(4000):
        x += rng.gauss(0, 1)
        pts.append((i * 0.1, x))
    a = pts
    b = [(t + 0.8, v) for t, v in pts]           # b's recorder is 0.8s late
    lag, corr = estimate_lead_lag(a, b, max_lag_s=2.0, step_s=0.1, grid_s=0.1)
    assert math.isclose(lag, 0.8, abs_tol=0.1) and corr > 0.9
