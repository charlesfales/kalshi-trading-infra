# Pre-registration: `<CANDIDATE NAME>`

Written and committed **before** any forward data is scored. Once frozen, the only
allowed edits are appended amendments (below), each saying whether it restarts the clock.

| field | value |
|---|---|
| Registered | `YYYY-MM-DD` (commit `<sha>`) |
| Scored from | `YYYY-MM-DD` (everything earlier is discovery, never evidence) |
| Status | `forward test at 1 contract` / `sized` / `retired` |

## 1. Hypothesis
One sentence: what trades, when, and what the edge is expected to be, in cents per contract.

## 2. Mechanism
Why should this edge exist, and who is on the other side of the trade? No mechanism, no
candidate. "The backtest says so" is not a mechanism.

## 3. The frozen rule
The exact trigger, price limits, timing window, and order type, precise enough that a
replay of the same data must produce the same fires.

## 4. Discovery evidence (not evidence)
What the search found, how many variants were tried, and the null it was compared with
(a permutation or randomised-trigger baseline). The number of variants tried matters as
much as the winner.

## 5. Decision rule
- **Sample target:** n ≥ ___ fills over ≥ ___ calendar days.
- **PASS:** the ___% block-bootstrap (by day) interval on net ¢/contract has its lower bound above 0.
- **FAIL:** the interval's upper bound is at or below 0, or ___.
- Scored mechanically by `kalshi_infra.prereg.score`. No verdict before the sample target.

## 6. Falsifier
The specific observation that kills the idea, written now so it can't be negotiated later.

## 7. Controls
What runs alongside it to show the edge is the rule's and not the market's (for example
the same trigger at a different time, or a randomised trigger at the same size).

## 8. Size
Minimum size until PASS. After PASS: the sizing policy, its cap, and its floor.

## 9. Revert plan
What happens on FAIL, and what happens if the live record diverges from the replay of
the same days.

## Amendments
| date | change | restarts clock? | why |
|---|---|---|---|
