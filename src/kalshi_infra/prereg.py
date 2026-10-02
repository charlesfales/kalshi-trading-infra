"""Pre-registration: decide how a rule will be judged before seeing the data that judges it.

Each candidate is frozen with a hypothesis, a mechanism, a sample-size target and a
falsifier. The scoreboard then prints PASS / FAIL / UNDECIDED mechanically, so the verdict
cannot drift toward whatever the latest numbers would prefer.

Discipline this encodes:
* New rules trade the minimum size until their own record clears the bar.
* Changing a frozen rule restarts its measurement clock: the record it built belongs to
  the old rule.
* Discovery data never counts as evidence. Only fills after ``scored_from`` do.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from .stats import Record, block_ci


class Verdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNDECIDED = "UNDECIDED"


@dataclass(frozen=True)
class Candidate:
    name: str
    hypothesis: str
    mechanism: str              # why the edge should exist; no mechanism, no candidate
    falsifier: str              # the observation that kills it, written in advance
    scored_from: str            # ISO date; earlier rows are discovery, not evidence
    min_n: int = 60
    min_days: int = 8
    level: float = 0.90


@dataclass(frozen=True)
class Score:
    candidate: str
    verdict: Verdict
    n: int
    days: int
    net_c: float
    ci_lo: float
    ci_hi: float
    note: str


def score(c: Candidate, rows: Sequence[Record], *, reps: int = 4000) -> Score:
    """Judge a candidate on its forward record only.

    PASS when the interval's lower bound is above zero; FAIL when its upper bound is at or
    below zero; otherwise UNDECIDED. Nothing is decided before the sample-size target.
    """
    fwd = [r for r in rows if r.get("day") and r["day"] >= c.scored_from]
    days = len({r["day"] for r in fwd})
    net = 100 * sum(r["pnl_per_contract"] for r in fwd) / len(fwd) if fwd else 0.0
    if len(fwd) < c.min_n or days < c.min_days:
        return Score(c.name, Verdict.UNDECIDED, len(fwd), days, net, math.nan, math.nan,
                     f"needs n>={c.min_n} and days>={c.min_days}")
    lo, hi = block_ci(fwd, reps=reps, level=c.level)
    if math.isnan(lo):
        return Score(c.name, Verdict.UNDECIDED, len(fwd), days, net, lo, hi, "no interval")
    if lo > 0:
        v, note = Verdict.PASS, "lower bound above zero"
    elif hi <= 0:
        v, note = Verdict.FAIL, f"falsified: {c.falsifier}"
    else:
        v, note = Verdict.UNDECIDED, "interval spans zero"
    return Score(c.name, v, len(fwd), days, net, lo, hi, note)
