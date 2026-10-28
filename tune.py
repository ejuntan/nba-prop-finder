"""Hyper-parameter sweep, on the training season only.

The half-lives and shrinkage strengths in features.py are choices, not
measurements. This scores them on 2024-25 and leaves the test seasons
untouched, so the numbers backtest.py reports stay out of sample.

Scoring deliberately skips the calibration fit: each candidate's raw
projection is mean-matched to the observed total and then scored by MAE,
averaged over the four stats that carry most of the volume. Ranking
configurations this way cannot be flattered by a per-config recalibration.
"""
import itertools
import sqlite3
import sys

import numpy as np
import pandas as pd

import features as F

SCORE_ON = [2025]
KEY = ["pts", "reb", "ast", "fg3m", "pra"]


def score(f):
    d = f[(f.season.isin(SCORE_ON)) & (f.dnp == 0) &
          (f["min"] >= F.MIN_PLAYED) & (f.games_seen >= 12)]
    out = {}
    for s in KEY:
        raw = d[f"proj_{s}"].to_numpy(float)
        y = d[s].to_numpy(float)
        k = y.mean() / max(raw.mean(), 1e-9)
        out[s] = np.abs(raw * k - y).mean() / max(y.mean(), 1e-9)
    out["score"] = float(np.mean([out[s] for s in KEY]))
    out["n"] = len(d)
    return out


def run(d, t, **kw):
    for k, v in kw.items():
        setattr(F, k.upper(), v)
    return score(F.build(d, t)[0])


def main():
    con = sqlite3.connect(F.DB)
    d, t = F.load(con)
    base = dict(hl_min=F.HL_MIN, hl_rate=F.HL_RATE, hl_def=F.HL_DEF,
                hl_pace=F.HL_PACE, k_rate=F.K_RATE, k_def=F.K_DEF,
                season_carry=F.SEASON_CARRY)
    rows = []
    b = run(d, t, **base)
    rows.append({"what": "baseline", **{k: base[k] for k in base}, **b})
    print(f"baseline score {b['score']:.5f}  (n={b['n']})")

    grid = {
        "hl_min": [2, 3, 4, 6, 8, 12],
        "hl_rate": [25, 35, 50, 70, 100, 140],
        "k_rate": [5, 10, 20, 40, 80, 150],
        "hl_def": [8, 15, 25],
        "k_def": [6, 14, 30],
        "hl_pace": [10, 20, 40],
    }
    # season_carry only bites at a season boundary, and the scoring season
    # is the first one in the data, so it cannot be tuned here. It stays at
    # its default and the test seasons meet it untuned.
    # One axis at a time, keeping the best found so far: a full product is
    # 2,000 rebuilds for a surface this smooth.
    cur = dict(base)
    for axis, vals in grid.items():
        best, bestv = None, None
        for v in vals:
            cfg = dict(cur, **{axis: v})
            r = run(d, t, **cfg)
            rows.append({"what": axis, **cfg, **r})
            flag = ""
            if best is None or r["score"] < best:
                best, bestv, flag = r["score"], v, "  <-"
            print(f"  {axis}={v!r:>6}  score {r['score']:.5f}"
                  f"  pts {r['pts']:.4f} reb {r['reb']:.4f}{flag}")
        cur[axis] = bestv
        print(f"  -> {axis} = {bestv}  ({best:.5f})\n")
    print("best config:", cur)
    r = run(d, t, **cur)
    print(f"best score {r['score']:.5f} vs baseline {b['score']:.5f} "
          f"({100 * (b['score'] - r['score']) / b['score']:+.2f}%)")
    pd.DataFrame(rows).to_csv(F.DIR + "/data/tune.csv", index=False)
    print("wrote data/tune.csv")


if __name__ == "__main__":
    main()
