# Sizing study: sizing on the evidence, not on the streak

`research/sizing_study.py` simulates 4,000 paths of 60 days × 25 trades from a $1,000
bankroll, in two worlds: one with a real edge (about +3¢ a contract after fees) and one
with none. The edge arrives in **daily regimes**, so good and bad days cluster, which is
what makes a lucky week look like a real edge. Everything is synthetic.

![Balance paths under three sizing policies](img/sizing_paths.png)

| world | policy | median final | 10th pct final | P(drawdown ≥ 40%) | P(end < $500) |
|---|---|---:|---:|---:|---:|
| real edge | flat $25 | $3,641 | $2,188 | 21.2% | 1.4% |
| real edge | balance / 30 | $13,554 | $2,022 | 88.3% | 1.3% |
| real edge | ¼-Kelly at point estimate | $3,579 | $1,043 | 35.4% | 0.0% |
| real edge | ¼-Kelly at 90% lower bound | $1,180 | $999 | 1.1% | 0.0% |
| no edge | flat $25 | $20 | $5 | 98.1% | 81.6% |
| no edge | balance / 30 | $75 | $17 | 100.0% | 91.7% |
| no edge | ¼-Kelly at point estimate | $893 | $726 | 10.4% | 0.4% |
| no edge | ¼-Kelly at 90% lower bound | $974 | $947 | 0.0% | 0.0% |

## What it says

- **You don't know which world you're in on day one.** That is the whole problem. A
  policy has to be judged in both columns at once.
- **Fixed-fraction sizing ignores the evidence.** `balance / 30` compounds hardest when
  the edge is real, but at 25 trades a day it has an 88% chance of a 40% drawdown along
  the way, and with no edge it loses 92% of the bankroll in nine paths out of ten.
- **Kelly at the point estimate chases streaks.** Regime clustering makes the running
  mean swing, and the stake swings with it: a 35% chance of a 40% drawdown even when
  the edge is real.
- **Kelly at the lower bound is insurance, and insurance has a price.** It almost never
  draws down in either world, but it gives up most of the upside in the first 60 days,
  because the bound clears zero only once enough days have accumulated. That is the
  intended trade: risk scales with what is known, not with what is hoped.

In practice I use the lower bound as a **switch**: one contract until a rule's own record
clears zero at the lower bound, then a fixed fraction of the balance, with a hard floor
that drops back to minimum size if the account falls below what was deposited.

Reproduce: `python research/sizing_study.py`
