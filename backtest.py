"""The honest scorecard.

Features are causal by construction, so the whole history can be scored in
one pass. Only two things are fitted, and they are fitted on the 2024-25
season alone: the log-linear mean calibration and the negative-binomial
dispersion. Everything reported below comes from 2025-26 and 2026-27, which
those constants never saw.

What is measured:

  accuracy      MAE and RMSE against two baselines a bettor could use for
                free -- the player's last ten games, and his season to date.
  calibration   the part that actually matters for pricing. For a span of
                lines around each projection, bucket the model's P(over)
                and compare it with how often the over actually landed. A
                model whose 60% bucket hits 52% is a losing model however
                good its MAE looks.
  fair line     at the line the model calls even money, the over should
                land about half the time.
  by history    the same calibration, split by how many games of the
                player's own record the projection had to work with. The
                floor is one game, so this is the table that says what a
                rookie's number is actually worth.
  naive-line    a soft-line proxy: hang the line at the player's last-ten
                average, bet the side the model prefers, and see what
                happens. This is NOT a market test -- real books are
                sharper than a ten-game average, and no closing prop lines
                are available from this data source. Treat it as an upper
                bound on edge, not an estimate of it.
"""
import sys

import numpy as np
import pandas as pd

import model as M
from features import STATS

DIR = __file__.rsplit("/", 1)[0]
FEAT = DIR + "/data/features.pkl"
TRAIN = [2025]
TEST = [2026, 2027]
REPORT = ["pts", "reb", "ast", "pra", "fg3m", "pr", "pa", "ra", "stl", "blk",
          "tov", "stocks", "fga", "fta"]
# Lines are drawn at random offsets in units of sigma rather than on a
# fixed grid: a grid leaves gaps in the probability it covers, and a
# calibration table that never looks at those gaps does not know what is
# in them.
SPAN_DRAWS = 16


def prep():
    f = pd.read_pickle(FEAT)
    f = f[(f.dnp == 0) & (f["min"] >= M.MIN_PLAYED) &
          (f.games_seen >= M.MIN_GAMES_SEEN)].copy()
    # Baselines a bettor has for free, built the same causal way.
    f = f.sort_values(["date", "game_id"])
    g = f.groupby("player_id", sort=False)
    for s in REPORT:
        f[f"b10_{s}"] = g[s].transform(
            lambda x: x.shift(1).rolling(10, min_periods=3).mean())
        f[f"bsn_{s}"] = g[s].transform(
            lambda x: x.shift(1).expanding(min_periods=3).mean())
    return f


def accuracy(te, cal):
    rows = []
    for s in REPORT:
        d = te[np.isfinite(te[f"b10_{s}"])]
        mu = M.mean(d[f"proj_{s}"], cal[s])
        y = d[s].to_numpy(float)
        b10 = d[f"b10_{s}"].to_numpy(float)
        bsn = d[f"bsn_{s}"].to_numpy(float)
        rows.append({
            "stat": s, "n": len(d), "mean": y.mean(),
            "MAE": np.abs(mu - y).mean(),
            "MAE_l10": np.abs(b10 - y).mean(),
            "MAE_szn": np.abs(bsn - y).mean(),
            "RMSE": np.sqrt(((mu - y) ** 2).mean()),
            "RMSE_l10": np.sqrt(((b10 - y) ** 2).mean()),
            "bias": (mu - y).mean(),
        })
    r = pd.DataFrame(rows)
    r["vs_l10"] = 100 * (r.MAE_l10 - r.MAE) / r.MAE_l10
    return r


def line_grid(te, cal, stats=REPORT):
    """Every (row, line) pair the calibration test runs on."""
    out = []
    for s in stats:
        d = te[np.isfinite(te[f"b10_{s}"])]
        mu = M.mean(d[f"proj_{s}"], cal[s])
        c = cal[s]
        y = d[s].to_numpy(float)
        seen = d.games_seen.to_numpy(float)
        for i, line in enumerate(M.sample_lines(mu, c, draws=SPAN_DRAWS,
                                                seed=99)):
            po = M.p_over_vec(mu, c, line)
            out.append(pd.DataFrame({
                "stat": s, "line": line, "p_over": po,
                "over": (y > line).astype(float), "mu": mu, "y": y,
                "seen": seen, "draw": i,
            }))
    return pd.concat(out, ignore_index=True)


def calibration(grid, bins=10):
    g = grid[(grid.p_over > 0.02) & (grid.p_over < 0.98)].copy()
    g["bucket"] = pd.cut(g.p_over, np.linspace(0, 1, bins + 1))
    t = g.groupby("bucket", observed=True).agg(
        n=("over", "size"), pred=("p_over", "mean"), actual=("over", "mean"))
    t["gap"] = t.actual - t.pred
    brier = ((g.p_over - g.over) ** 2).mean()
    base = g.over.mean()
    brier_base = ((base - g.over) ** 2).mean()
    ll = -(g.over * np.log(g.p_over.clip(1e-9)) +
           (1 - g.over) * np.log((1 - g.p_over).clip(1e-9))).mean()
    ll_base = -(g.over * np.log(base) + (1 - g.over) * np.log(1 - base)).mean()
    return t, {"brier": brier, "brier_base": brier_base,
               "brier_skill": 1 - brier / brier_base,
               "logloss": ll, "logloss_base": ll_base, "n": len(g)}


def by_history(grid, te, cal):
    """Calibration and accuracy split by depth of the player's own record.

    The grid carries each row's games_seen, because its blocks are not all
    the same length -- a stat whose baseline is undefined for a row drops
    that row -- so a mask built from `te` cannot be laid over it."""
    rows = []
    bands = [(0, 3), (3, 8), (8, 15), (15, 30), (30, 1e9)]
    for lo, hi in bands:
        g = grid[(grid.seen >= lo) & (grid.seen < hi)]
        g = g[(g.p_over > 0.02) & (g.p_over < 0.98)]
        d = te[(te.games_seen >= lo) & (te.games_seen < hi)]
        d = d[np.isfinite(d.b10_pts)]
        if len(g) < 500 or len(d) < 50:
            continue
        base = g.over.mean()
        mu = M.mean(d["proj_pts"], cal["pts"])
        y = d["pts"].to_numpy(float)
        mae = np.abs(mu - y).mean()
        mae_b = np.abs(d["b10_pts"].to_numpy(float) - y).mean()
        rows.append({
            "games_seen": f"{lo:g}-{hi:g}" if hi < 1e9 else f"{lo:g}+",
            "player_games": int(len(d)), "pairs": len(g),
            "pred": g.p_over.mean(), "actual": g.over.mean(),
            "gap": g.over.mean() - g.p_over.mean(),
            "brier_skill": 1 - ((g.p_over - g.over) ** 2).mean() /
                           ((base - g.over) ** 2).mean(),
            "pts_mean": y.mean(), "pts_MAE": mae,
            "MAE_l10": mae_b, "vs_l10": 100 * (mae_b - mae) / mae_b,
        })
    return pd.DataFrame(rows)


def per_stat_calibration(grid):
    rows = []
    for s, g in grid.groupby("stat"):
        g = g[(g.p_over > 0.02) & (g.p_over < 0.98)]
        if len(g) < 500:
            continue
        brier = ((g.p_over - g.over) ** 2).mean()
        base = g.over.mean()
        rows.append({"stat": s, "n": len(g), "pred": g.p_over.mean(),
                     "actual": g.over.mean(),
                     "gap": g.over.mean() - g.p_over.mean(),
                     "brier_skill": 1 - brier / ((base - g.over) ** 2).mean()})
    return pd.DataFrame(rows).sort_values("stat")


def fair_line_test(te, cal):
    rows = []
    for s in REPORT:
        d = te[np.isfinite(te[f"b10_{s}"])]
        c = cal[s]
        mu = M.mean(d[f"proj_{s}"], c)
        y = d[s].to_numpy(float)
        lines = np.array([M.fair_line(m, c) for m in mu])
        ps = M.p_over_vec(mu, c, lines)
        rows.append({"stat": s, "n": len(d), "p_over_said": ps.mean(),
                     "over_hit": (y > lines).mean()})
    return pd.DataFrame(rows)


def naive_line_test(te, cal, edge_min=0.04):
    """Bet into a line hung at the player's last-ten average."""
    rows = []
    for s in REPORT:
        d = te[np.isfinite(te[f"b10_{s}"])].copy()
        c = cal[s]
        mu = M.mean(d[f"proj_{s}"], c)
        y = d[s].to_numpy(float)
        line = np.floor(np.maximum(d[f"b10_{s}"].to_numpy(float), 0.5)) + 0.5
        po = M.p_over_vec(mu, c, line)
        side_over = po > 0.5
        edge = np.abs(po - 0.5)
        take = edge >= edge_min
        won = np.where(side_over, y > line, y < line)
        push = y == line
        bet = take & ~push
        n = int(bet.sum())
        wr = won[bet].mean() if n else np.nan
        # Flat -110 is the usual prop price on a main number.
        roi = (wr * (100 / 110) - (1 - wr)) * 100 if n else np.nan
        rows.append({"stat": s, "bets": n, "pct_of_rows": 100 * n / len(d),
                     "win%": 100 * wr if n else np.nan, "roi@-110": roi})
    return pd.DataFrame(rows)


def ablation(tr, te, stats=("pts", "reb", "ast", "pra", "fg3m")):
    """Drop one factor at a time. The projection is a product, so a factor
    can be switched off by setting it to 1 -- no rebuild needed."""
    variants = {"full": (), "no pace": ("pace",), "no opponent": ("opp",),
                "no home/away": ("home",),
                "minutes x rate only": ("pace", "opp", "home"),
                "last-10 average": ("baseline",)}
    rows = []
    for name, off in variants.items():
        rec = {"variant": name}
        for s in stats:
            if "baseline" in off:
                d = te[np.isfinite(te[f"b10_{s}"])]
                mu = d[f"b10_{s}"].to_numpy(float)
                y = d[s].to_numpy(float)
                rec[s] = np.abs(mu - y).mean()
                continue
            raw_tr = tr[f"rate_{s}"] * tr["proj_min"]
            raw_te = te[f"rate_{s}"] * te["proj_min"]
            for fac, col in (("pace", "pace_mult"), ("opp", f"opp_{s}"),
                             ("home", f"home_{s}")):
                if fac not in off:
                    raw_tr = raw_tr * tr[col]
                    raw_te = raw_te * te[col]
            c = M.fit_stat(raw_tr.to_numpy(float), tr[s].to_numpy(float))
            mu = M.mean(raw_te, c)
            rec[s] = np.abs(mu - te[s].to_numpy(float)).mean()
        rows.append(rec)
    r = pd.DataFrame(rows).set_index("variant")
    return (r / r.loc["full"] * 100 - 100).round(2).add_suffix(" %worse") \
        .join(r.round(4))


def fmt(df, floats="%.3f"):
    return df.to_string(index=False, float_format=lambda x: floats % x)


def main():
    f = prep()
    tr = f[f.season.isin(TRAIN)]
    te = f[f.season.isin(TEST)]
    print(f"train {len(tr)} player-games ({TRAIN}) | "
          f"test {len(te)} ({TEST}, {te.date.min()} .. {te.date.max()})")
    cal = M.fit(tr, STATS)
    print("\n--- fitted constants (2024-25 only) ---")
    print(fmt(pd.DataFrame([
        {"stat": s, "a": cal[s]["a"], "b": cal[s]["b"],
         "c": cal[s]["c"] if cal[s]["c"] else np.nan,
         "p": cal[s]["p"] if cal[s]["p"] else np.nan,
         "sd@mu=20": float(M.sd(20.0, cal[s])),
         "shape@.25": float(M.shape(0.25, cal[s])),
         "shape@.50": float(M.shape(0.50, cal[s])),
         "shape@.95": float(M.shape(0.95, cal[s])),
         "dispersion": cal[s]["dispersion"]} for s in REPORT])))

    acc = accuracy(te, cal)
    print("\n--- accuracy, out of sample ---")
    print(fmt(acc[["stat", "n", "mean", "MAE", "MAE_l10", "MAE_szn",
                   "vs_l10", "RMSE", "RMSE_l10", "bias"]], "%.3f"))

    print("\n--- ablation: MAE when a factor is switched off ---")
    print("    positive % means the full model is better without it")
    print(ablation(tr, te).to_string())

    grid = line_grid(te, cal)
    tab, agg = calibration(grid)
    print(f"\n--- calibration over {agg['n']:,} (row, line) pairs ---")
    print(fmt(tab.reset_index().astype({"bucket": str})))
    print(f"\nBrier {agg['brier']:.4f} vs base rate {agg['brier_base']:.4f} "
          f"(skill {agg['brier_skill']:+.3%})   "
          f"log loss {agg['logloss']:.4f} vs {agg['logloss_base']:.4f}")

    print("\n--- by depth of the player's own record ---")
    print("    the floor is one game, so the top row is the rookie case")
    print(fmt(by_history(grid, te, cal)))

    print("\n--- calibration by stat ---")
    print(fmt(per_stat_calibration(grid)))

    print("\n--- at the model's own fair line, the over should land ~50% ---")
    print(fmt(fair_line_test(te, cal)))

    print("\n--- naive-line test (line = last-10 average, flat -110) ---")
    print("    not a market test; see this file's docstring")
    print(fmt(naive_line_test(te, cal)))

    grid.to_pickle(DIR + "/data/backtest_grid.pkl")
    acc.to_pickle(DIR + "/data/backtest_acc.pkl")
    print(f"\nwrote data/calib.json, data/backtest_grid.pkl")


if __name__ == "__main__":
    main()
