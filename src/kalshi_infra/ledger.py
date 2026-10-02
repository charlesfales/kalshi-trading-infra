"""Fills and ledger reconciliation.

A row is evidence only if somebody was charged for it. An order that was placed and then
cancelled, or that rested unfilled, must never enter a track record priced at its aim.
So the record is built from exchange fills, and the ledger is reconciled to the account
balance before any statistic is trusted.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal


@dataclass(slots=True)
class OrderFill:
    contracts: Decimal
    cost: Decimal
    fees: Decimal

    @property
    def avg_price(self) -> Decimal | None:
        return self.cost / self.contracts if self.contracts else None


def fills_by_order(fills: Iterable[Mapping]) -> dict[str, OrderFill]:
    """Aggregate exchange fills per order id. An order can fill in pieces; never assume one
    fill per order."""
    out: dict[str, OrderFill] = {}
    for f in fills:
        oid = f.get("order_id")
        ct = Decimal(str(f.get("count") or 0))
        if not oid or ct <= 0:
            continue
        side = str(f.get("side") or "").lower()
        px = Decimal(str(f.get("no_price_dollars") if side == "no"
                         else f.get("yes_price_dollars") or 0))
        agg = out.setdefault(oid, OrderFill(Decimal(0), Decimal(0), Decimal(0)))
        agg.contracts += ct
        agg.cost += ct * px
        agg.fees += Decimal(str(f.get("fee_cost") or 0))
    return out


@dataclass(frozen=True)
class Reconciliation:
    expected_balance: Decimal
    actual_balance: Decimal

    @property
    def difference(self) -> Decimal:
        return self.actual_balance - self.expected_balance

    @property
    def ok(self) -> bool:
        return self.difference == 0


def reconcile(*, deposits: Iterable[Decimal], withdrawals: Iterable[Decimal],
              realized_pnl: Iterable[Decimal], open_cost: Decimal,
              actual_balance: Decimal) -> Reconciliation:
    """Cash should equal deposits - withdrawals + realized P&L - cost of open positions.

    Exact Decimal arithmetic: a ledger that "ties to within a cent" is a ledger with an
    unexplained cent. Any difference is a bug to find (a missed fee, a double-counted
    deposit) before the numbers are used for anything.
    """
    expected = (sum(deposits, Decimal(0)) - sum(withdrawals, Decimal(0))
                + sum(realized_pnl, Decimal(0)) - open_cost)
    return Reconciliation(expected, actual_balance)
