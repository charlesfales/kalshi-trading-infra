"""Monte Carlo: how should a binary-contract strategy size its trades?

Synthetic world (no real trade data is used):
  * bankroll $1,000; 25 trades a day for 60 days
  * entry price p ~ U(0.30, 0.70); taker fee 0.07 * p * (1 - p) per contract
  * the true edge comes in DAILY REGIMES: day d has edge e_d ~ N(mu, 0.04), so a trade
    wins with probability p + e_d. Clustering is what makes streaks convincing.
  * two worlds: a real edge (mu = +5c gross, about +3c after fees) and no edge (mu = 0)

Policies:
  flat         $25 a trade, regardless of balance
  proportional balance / 30
  kelly_point  quarter-Kelly from the running POINT ESTIMATE of edge
  kelly_lower  quarter-Kelly from the 90% LOWER BOUND of a by-day interval; one contract
               until 8 days and 60 trades exist (what kalshi_infra.stats.stake_state does)

Run:  python research/sizing_study.py      (writes docs/img/*.png and prints the table)
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

PATHS, DAYS, PER_DAY = 4000, 60, 25
BANKROLL, REGIME_SD, KELLY_FRAC, MAX_FRAC = 1_000.0, 0.04, 0.25, 0.10
POLICIES = ("flat", "proportional", "kelly_point", "kelly_lower")


def simulate(mu: float, seed: int = 11) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(seed)
    n = DAYS * PER_DAY
    price = rng.uniform(0.30, 0.70, size=(PATHS, n))
    day_edge = rng.normal(mu, REGIME_SD, size=(PATHS, DAYS)).repeat(PER_DAY, axis=1)
    win = rng.random((PATHS, n)) < np.clip(price + day_edge, 0.01, 0.99)
    fee = 0.07 * price * (1 - price)
    pnl_c = np.where(win, 1 - price, -price) - fee           # per contract, net

    out = {}
    for pol in POLICIES:
        bal = np.full(PATHS, BANKROLL)
        hist = np.empty((PATHS, DAYS + 1))
        hist[:, 0] = bal
        cum = np.zeros(PATHS)                                  # running sum pnl/contract
        day_means: list[np.ndarray] = []
        day_acc = np.zeros(PATHS)
        for t in range(n):
            p = price[:, t]
            if pol == "flat":
                stake = np.full(PATHS, 25.0)
            elif pol == "proportional":
                stake = bal / 30
            elif pol == "kelly_point":
                est = cum / t if t >= 20 else np.zeros(PATHS)
                stake = bal * np.clip(KELLY_FRAC * est / 0.5, 0, MAX_FRAC)
            else:
                d = len(day_means)
                if d >= 8 and t >= 60:
                    m = np.stack(day_means)
                    lo = m.mean(0) - 1.645 * m.std(0, ddof=1) / np.sqrt(d)
                    stake = bal * np.clip(KELLY_FRAC * lo / 0.5, 0, MAX_FRAC)
                else:
                    stake = np.zeros(PATHS)
            contracts = np.maximum(np.floor(stake / p), 1.0)      # always at least one
            contracts = np.where(bal >= contracts * p * 1.1, contracts, 0.0)  # can't fund
            bal = bal + contracts * pnl_c[:, t]
            cum += pnl_c[:, t]
            day_acc += pnl_c[:, t]
            if (t + 1) % PER_DAY == 0:
                day_means.append(day_acc / PER_DAY)
                day_acc = np.zeros(PATHS)
                hist[:, (t + 1) // PER_DAY] = bal
        out[pol] = hist
    return out


def summarise(hist: np.ndarray) -> dict[str, float]:
    peak = np.maximum.accumulate(hist, axis=1)
    dd = 1 - hist / peak
    final = hist[:, -1]
    return {"median_final": float(np.median(final)),
            "p10_final": float(np.percentile(final, 10)),
            "p_dd_40": float((dd.max(1) >= 0.40).mean()),
            "p_below_half": float((final < BANKROLL / 2).mean())}


def plot(results: dict[str, dict[str, np.ndarray]], path: Path) -> None:
    import matplotlib.pyplot as plt

    surface, ink, ink2, grid = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
    colors = {"proportional": "#2a78d6", "kelly_point": "#eb6834", "kelly_lower": "#1baf7a"}
    labels = {"proportional": "balance / 30", "kelly_point": "Kelly at point estimate",
              "kelly_lower": "Kelly at lower bound"}
    titles = {"edge": "Real edge (~+3¢ a contract after fees)", "none": "No edge"}

    from matplotlib.ticker import FuncFormatter

    fig, axes = plt.subplots(1, 2, figsize=(11, 4.6), sharey=True, facecolor=surface)
    days = np.arange(DAYS + 1)
    for ax, (key, res) in zip(axes, results.items(), strict=True):
        ax.set_facecolor(surface)
        ax.set_yscale("log")
        ends = []
        for pol in ("proportional", "kelly_point", "kelly_lower"):
            h = np.maximum(res[pol], 1.0)
            med = np.median(h, 0)
            ax.fill_between(days, np.percentile(h, 10, 0), np.percentile(h, 90, 0),
                            color=colors[pol], alpha=0.10, linewidth=0)
            ax.plot(days, med, color=colors[pol], linewidth=2, label=labels[pol])
            ends.append([np.log10(med[-1]), pol])
        # Direct labels at line ends, pushed apart in log space so they never collide.
        ends.sort()
        for i in range(1, len(ends)):
            ends[i][0] = max(ends[i][0], ends[i - 1][0] + 0.2)
        for y, pol in ends:
            ax.annotate(labels[pol], (days[-1], 10 ** y), xytext=(6, 0),
                        textcoords="offset points", va="center", fontsize=9, color=ink,
                        annotation_clip=False)
        ax.axhline(BANKROLL, color=ink2, linewidth=1, linestyle=(0, (2, 3)))
        ax.set_ylim(10, 200_000)
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"${v:,.0f}"))
        ax.yaxis.set_minor_locator(plt.NullLocator())
        ax.set_title(titles[key], loc="left", fontsize=11, color=ink)
        ax.set_xlabel("day", color=ink2, fontsize=9)
        ax.grid(axis="y", color=grid, linewidth=0.8)
        ax.tick_params(colors=ink2, labelsize=8)
        for s in ("top", "right", "left"):
            ax.spines[s].set_visible(False)
        ax.spines["bottom"].set_color(grid)
        ax.set_xlim(0, DAYS)
    axes[0].set_ylabel("balance, log scale (median, 10–90% band)", color=ink2, fontsize=9)
    fig.suptitle("Same trades, different sizing: 4,000 simulated paths from $1,000",
                 x=0.06, ha="left", fontsize=12, color=ink)
    handles, lbls = axes[0].get_legend_handles_labels()
    fig.legend(handles, lbls, loc="upper left", ncol=3, frameon=False, fontsize=9,
               labelcolor=ink, bbox_to_anchor=(0.055, 0.93))
    fig.tight_layout(rect=(0, 0, 0.9, 0.87), w_pad=10)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150, facecolor=surface)


def main() -> None:
    results = {"edge": simulate(0.05), "none": simulate(0.0)}
    print(f"{'world':<6} {'policy':<13} {'median $':>9} {'p10 $':>8} "
          f"{'P(dd>=40%)':>11} {'P(<$500)':>9}")
    for world, res in results.items():
        for pol in POLICIES:
            s = summarise(res[pol])
            print(f"{world:<6} {pol:<13} {s['median_final']:>9,.0f} {s['p10_final']:>8,.0f} "
                  f"{s['p_dd_40']:>11.1%} {s['p_below_half']:>9.1%}")
    root = Path(__file__).resolve().parents[1]
    plot(results, root / "docs" / "img" / "sizing_paths.png")


if __name__ == "__main__":
    main()
