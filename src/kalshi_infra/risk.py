"""Pre-trade risk gates.

Every live order passes ``RiskController.check_pre_order`` immediately before submission.
Each gate returns a reason string, so a refused order is always explainable after the fact.
Gates fail closed: anything the controller cannot verify is a refusal, never a guess.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RiskLimits:
    live_enabled: bool = False                  # master switch; off by default
    kill_file: Path = Path("KILL")              # touch this file to halt new entries
    daily_loss_kill_usd: float = 100.0
    max_open_positions: int = 5
    max_contracts_per_order: int = 1_000        # sanity ceiling, not a sizing rule
    max_stake_usd: float = 25.0                 # dollars at risk per order
    max_slippage_cents: int = 5                 # signal price vs. price at submit
    max_feed_age_s: float = 2.0                 # every input the decision used must be fresh
    min_seconds_to_expiry: float = 0.0
    max_seconds_to_expiry: float = float("inf")
    latency_cap_ms: float = 2500.0
    latency_min_slow: int = 2                   # this many slow samples in the window blocks
    latency_min_samples: int = 10


@dataclass(slots=True)
class RiskDecision:
    allow: bool
    reason: str | None = None


@dataclass(slots=True)
class RiskState:
    realized_pnl_today: float = 0.0
    open_positions: int = 0
    killed: bool = False
    killed_reason: str | None = None
    latency_samples: deque[float] = field(default_factory=lambda: deque(maxlen=100))


class RiskController:
    def __init__(self, limits: RiskLimits, *, today: date | None = None) -> None:
        self.limits = limits
        self.state = RiskState()
        self._today = today or datetime.now(UTC).date()

    # -- state updates ---------------------------------------------------------------
    def record_latency_ms(self, ms: float) -> None:
        self.state.latency_samples.append(ms)

    def record_realized_pnl(self, pnl: float, *, today: date | None = None) -> None:
        """Add settled P&L; the daily counter rotates at UTC midnight."""
        d = today or datetime.now(UTC).date()
        if d != self._today:
            self._today = d
            self.state.realized_pnl_today = 0.0
        self.state.realized_pnl_today += pnl

    def note_open_position(self, delta: int) -> None:
        self.state.open_positions = max(0, self.state.open_positions + delta)

    def trip_kill(self, reason: str) -> None:
        """Latching: once tripped, only a restart clears it."""
        if not self.state.killed:
            self.state.killed = True
            self.state.killed_reason = reason

    # -- gates -----------------------------------------------------------------------
    def _slow_latency(self) -> tuple[int, int, float] | None:
        """(slow, total, worst) over the window, or None while it is too small.

        Counts samples over the cap rather than reading a percentile: any percentile
        estimated from n < 100 collapses to the MAX (nearest-rank p99 is index n-1 for
        every n < 100), so a "p99 > cap" rule is really a one-blip rule for the first
        ~100 samples after every restart. A count says what the gate means: the venue
        is slow repeatedly, not once.
        """
        n = len(self.state.latency_samples)
        if n < self.limits.latency_min_samples:
            return None
        slow = sum(1 for s in self.state.latency_samples if s > self.limits.latency_cap_ms)
        return slow, n, max(self.state.latency_samples)

    def check_global(self) -> RiskDecision:
        """Gates checked once per loop tick, independent of any order."""
        if self.state.killed:
            return RiskDecision(False, f"killed:{self.state.killed_reason}")
        if self.limits.kill_file.exists():
            self.trip_kill("kill_file_present")
            return RiskDecision(False, "kill_file_present")
        if self.state.realized_pnl_today <= -abs(self.limits.daily_loss_kill_usd):
            self.trip_kill(f"daily_loss_limit pnl={self.state.realized_pnl_today:.2f}")
            return RiskDecision(False, "daily_loss_limit")
        return RiskDecision(True)

    def check_pre_order(
        self,
        *,
        count: int,
        signal_price_cents: int,
        current_price_cents: int,
        feed_age_s: float,
        seconds_to_expiry: float,
    ) -> RiskDecision:
        """Per-order gates, called immediately before submission."""
        L = self.limits
        if not L.live_enabled:
            return RiskDecision(False, "live_disabled")
        glob = self.check_global()
        if not glob.allow:
            return glob
        if self.state.open_positions >= L.max_open_positions:
            return RiskDecision(False, "max_open_positions")
        if count <= 0:
            return RiskDecision(False, "non_positive_count")
        if count > L.max_contracts_per_order:
            return RiskDecision(False, f"count_over_ceiling:{count}>{L.max_contracts_per_order}")
        # Out-of-range prices are refused, never clamped: a clamped price is an order
        # nobody decided to place.
        for name, px in (("signal", signal_price_cents), ("current", current_price_cents)):
            if not 1 <= px <= 99:
                return RiskDecision(False, f"{name}_price_out_of_band:{px}")
        stake_usd = count * current_price_cents / 100.0
        if stake_usd > L.max_stake_usd:
            return RiskDecision(False, f"stake_over_cap:${stake_usd:.2f}>${L.max_stake_usd:.2f}")
        if abs(current_price_cents - signal_price_cents) > L.max_slippage_cents:
            return RiskDecision(
                False, f"slippage:{signal_price_cents}->{current_price_cents}")
        if feed_age_s > L.max_feed_age_s:
            return RiskDecision(False, f"feed_stale:{feed_age_s:.1f}s")
        if seconds_to_expiry < L.min_seconds_to_expiry:
            return RiskDecision(False, "too_close_to_expiry")
        if seconds_to_expiry > L.max_seconds_to_expiry:
            return RiskDecision(False, "too_far_from_expiry")
        lat = self._slow_latency()
        if lat is not None and lat[0] >= L.latency_min_slow:
            slow, n, worst = lat
            return RiskDecision(
                False, f"latency:{slow}/{n} over {L.latency_cap_ms:.0f}ms (worst {worst:.0f}ms)")
        return RiskDecision(True)
