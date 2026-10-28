"""What actually happened to the lines we logged.

This is the measurement the project has been missing from the start.
Every other number in this repository is either the model marking its own
homework -- calibration against its own distribution -- or a comparison
with a baseline nobody is offering. Here a real line, at a real price,
meets a real box score.

Three things are reported, in descending order of how much they matter.

**Calibration at market lines.** When the model said 60% at a number a
book actually hung, did it land 60% of the time? That is the only
calibration test that means anything for betting, because it is measured
where the lines are rather than over a spread of hypothetical ones.

**Return.** The model's preferred side, graded at the price offered,
bucketed by the edge it claimed. If the claimed edge is real, return
should rise with it. If it does not, the edge is noise however pretty the
calibration looks.

**Closing line value.** Where a prop was quoted more than once, the move
between the first price seen and the last before tip. CLV is the metric
that settles this fastest, because it needs no outcomes at all -- but it
needs more than one snapshot a day, so it stays empty until the fetch
runs more than once.

Nothing is graded until the game is final, pushes are returned rather
than counted as losses, and every table carries its sample size because
most of them will be far too small to mean anything for weeks.
"""
import sqlite3
import sys

import numpy as np
import pandas as pd

import fetch_odds as FO
import model as M
from features import COMBOS, DB, DIR
from project import LOG as SLATE_LOG

OUT = DIR + "/data/graded.pkl"
CSV = DIR + "/out/graded.csv"
BASE = ["pts", "reb", "ast", "fg3m", "stl", "blk", "tov", "fga", "fta"]


def actuals():
    """Final box-score lines for every completed game, combos included."""
    con = sqlite3.connect(DB)
    d = pd.read_sql(f"""
        SELECT p.game_id, p.player_id, p.dnp, p.min,
               {', '.join('p.' + c for c in BASE)}, g.date, g.state
        FROM player_lines p JOIN games g USING (game_id)
        WHERE g.state = 'post' AND g.season_type IN (2, 3, 5)
    """, con)
    con.close()
    for name, parts in COMBOS.items():
        d[name] = sum(d[p] for p in parts)
    return d


def projections():
    try:
        con = sqlite3.connect(SLATE_LOG)
        d = pd.read_sql("SELECT * FROM slate_log", con)
        con.close()
        return d
    except Exception:
        return pd.DataFrame()


def build():
    con = sqlite3.connect(FO.ODDS_DB)
    quotes = pd.read_sql("SELECT * FROM prop_lines", con)
    con.close()
    if quotes.empty:
        print("no lines logged yet -- run fetch_odds.py")
        return None
    quotes = quotes[quotes.player_id.notna()]
    act, proj = actuals(), projections()
    if act.empty:
        print("no completed games to grade against yet")
        return None

    # Grade the last quote each book gave before tip.
    quotes = quotes.sort_values("fetched")
    last = quotes.groupby(["game_id", "book", "player_id", "stat"],
                          as_index=False).last()
    first = quotes.groupby(["game_id", "book", "player_id", "stat"],
                           as_index=False).first()
    moves = last.merge(first[["game_id", "book", "player_id", "stat", "line",
                              "over_odds", "under_odds", "fetched"]],
                       on=["game_id", "book", "player_id", "stat"],
                       suffixes=("", "_open"))

    rows = []
    cal = M.load()
    av = act.set_index(["game_id", "player_id"])
    pr = (proj.set_index(["game_id", "player_id"])
          if not proj.empty else None)
    for r in moves.itertuples(index=False):
        key = (int(r.game_id), int(r.player_id))
        if key not in av.index:
            continue
        a = av.loc[key]
        if isinstance(a, pd.DataFrame):
            a = a.iloc[0]
        if a.dnp or r.stat not in cal:
            continue          # an ungraded prop is a refund, not a result
        actual = float(a[r.stat])
        line = float(r.line)
        result = "push" if actual == line else (
            "over" if actual > line else "under")
        mu = np.nan
        if pr is not None and key in pr.index:
            p = pr.loc[key]
            if isinstance(p, pd.DataFrame):
                p = p.iloc[0]
            col = f"proj_{r.stat}"
            if col in p and pd.notna(p[col]):
                mu = float(p[col])
        if not np.isfinite(mu):
            continue          # no projection was live that day
        po, pu, push = M.p_over(mu, cal[r.stat], line)
        oo, uo = r.over_odds, r.under_odds
        if pd.isna(oo) or pd.isna(uo):
            continue
        nv_o, nv_u = M.devig(float(oo), float(uo))
        over = M.ev(po, float(oo)) >= M.ev(pu, float(uo))
        side = "over" if over else "under"
        p_model = po if over else pu
        odds = float(oo if over else uo)
        edge = p_model - (nv_o if over else nv_u)
        won = None if result == "push" else (result == side)
        payout = odds / 100.0 if odds > 0 else 100.0 / -odds
        pnl = 0.0 if won is None else (payout if won else -1.0)
        rows.append({
            "date": a.date, "game_id": key[0], "player_id": key[1],
            "book": r.book, "stat": r.stat, "line": line,
            "line_open": float(r.line_open), "actual": actual,
            "result": result, "proj": mu, "p_over": po,
            "side": side, "p_model": p_model, "odds": odds,
            "novig": nv_o if over else nv_u, "edge": edge,
            "won": won, "pnl": pnl,
            "snapshots": 1 if r.fetched == r.fetched_open else 2,
            "line_move": line - float(r.line_open),
        })
    d = pd.DataFrame(rows)
    if d.empty:
        done = act.game_id.nunique()
        have = quotes.game_id.nunique()
        print(f"nothing to grade yet: {have} game(s) have logged lines, and "
              f"none of them is final.\n"
              f"  ({done} completed games are on file, but no lines were "
              "logged for them -- grading starts with tonight's.)")
        return None
    return d


def calibration(d, bins=(0, .4, .45, .5, .55, .6, .65, 1.0)):
    g = d[d.result != "push"].copy()
    g["bucket"] = pd.cut(g.p_over, bins)
    t = g.groupby("bucket", observed=True).agg(
        n=("p_over", "size"), predicted=("p_over", "mean"),
        actual=("result", lambda s: (s == "over").mean()))
    t["gap"] = t.actual - t.predicted
    return t


def by_edge(d, bins=(-1, 0, .02, .05, .10, 1)):
    g = d.copy()
    g["bucket"] = pd.cut(g.edge, bins)
    t = g.groupby("bucket", observed=True).agg(
        bets=("pnl", "size"), pushes=("won", lambda s: s.isna().sum()),
        win_pct=("won", lambda s: 100 * s.dropna().mean()
                 if s.notna().any() else np.nan),
        roi=("pnl", lambda s: 100 * s.mean()))
    return t


def main():
    d = build()
    if d is None:
        return
    d.to_pickle(OUT)
    d.to_csv(CSV, index=False, float_format="%.4f")
    graded = d[d.result != "push"]
    print(f"graded {len(d)} quotes ({len(d.groupby(['game_id','player_id','stat']))} "
          f"props, {d.date.nunique()} day(s), {d.book.nunique()} books); "
          f"{(d.result == 'push').sum()} pushed and were returned")

    print("\n--- calibration at real market lines ---")
    print(calibration(d).to_string(float_format=lambda x: f"{x:.3f}"))
    g = graded
    base = (g.result == "over").mean()
    brier = ((g.p_over - (g.result == "over")) ** 2).mean()
    bb = ((base - (g.result == "over")) ** 2).mean()
    print(f"Brier {brier:.4f} vs {bb:.4f} for quoting the base rate "
          f"(skill {1 - brier / bb:+.1%})")

    print("\n--- return on the model's preferred side, by claimed edge ---")
    print(by_edge(d).to_string(float_format=lambda x: f"{x:.2f}"))
    n, roi = len(graded), 100 * d.pnl.mean()
    se = 100 * d.pnl.std() / np.sqrt(max(len(d), 1))
    print(f"overall {n} bets, ROI {roi:+.2f}% "
          f"(standard error {se:.2f}%, so anything inside "
          f"{2 * se:.1f}% is noise)")

    print("\n--- by market ---")
    t = d.groupby("stat").agg(
        bets=("pnl", "size"),
        win_pct=("won", lambda s: 100 * s.dropna().mean()
                 if s.notna().any() else np.nan),
        roi=("pnl", lambda s: 100 * s.mean()),
        mean_edge=("edge", "mean"))
    print(t.to_string(float_format=lambda x: f"{x:.2f}"))

    moved = d[d.snapshots > 1]
    print("\n--- closing line value ---")
    if moved.empty:
        print("  nothing to measure: every prop was quoted once. CLV needs")
        print("  the fetch to run at least twice before tip -- an opening")
        print("  snapshot and a closing one.")
    else:
        beat = (np.sign(moved.line_move) ==
                np.where(moved.side == "over", -1, 1)).mean()
        print(f"  {len(moved)} props seen twice; the line moved the model's "
              f"way {beat:.1%} of the time")

    if n < 300:
        print(f"\n{n} graded bets is not a sample. Two or three weeks of "
              "daily logging is where these tables start to mean something; "
              "until then they are a plumbing check.")


if __name__ == "__main__":
    main()
