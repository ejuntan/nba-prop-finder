"""Real lines against the model's own numbers.

Two different questions get confused in prop betting and this keeps them
apart.

**Is this price good against the book offering it?** Devig that book's own
two-way market and compare. It is the number most tools show, and it is
the weaker one: a book that hangs a bad line on both sides looks like
enormous edge to a model that is itself wrong in the same direction.

**Is this price good against the market?** Devig every book quoting the
same number, take the median of their no-vig probabilities, and compare
with that. The consensus is a far better estimate of the true probability
than any single book, so edge measured against it is the honest figure.
Where only one book is quoting, there is no consensus and the column says
so rather than quietly reusing the first answer.

**One book is not a market.** Ranking by raw expected value puts the
thinnest quotes on top, because the only way to show a thirty-point edge
against an efficient market is for the quote to be wrong, stale, or
alone. The first table here therefore requires a consensus and ranks by
edge against it; single-book quotes get their own table underneath,
clearly labelled, because they are where both the mistakes and the
occasional real opportunity live.

Every run also prints two standing sanity checks. The first is how
closely the model tracks the market overall: agreement to within a couple
of points makes the small disagreements worth reading, and a model that
has drifted far from the market is broken, not brilliant.

The second is which side it keeps landing on. Against an efficient market
a calibrated model should favour overs about half the time. Anything far
from that is a bias being reported as edge -- the same projection being
slightly high on every player produces a long list of confident overs,
and every one of them will look like value right up until they are
graded.

Nothing here refits anything. The projections come from the slate, the
dispersion from data/calib.json, and the prices from the last snapshot
data/odds.sqlite3 holds.
"""
import os
import sys

import numpy as np
import pandas as pd

import fetch_odds as FO
import model as M
from features import DIR

SLATE = DIR + "/data/slate.pkl"
OUT = DIR + "/data/edges.pkl"
CSV = DIR + "/out/edges.csv"
MIN_BOOKS_FOR_CONSENSUS = 3
# The ceiling on a believable edge. The backtest says this model beats a
# ten-game average by one to three per cent of projection error and is
# calibrated to within a point; nothing in it supports beating a four-book
# consensus by twenty-five points of probability. A disagreement that size
# is the model carrying a stale rate far more often than it is a market
# mistake, so it is flagged rather than ranked as an opportunity.
#
# Note how little movement it takes: a gap of under one standard deviation
# between the line and the projection is worth twenty-five points of
# probability near the middle of a distribution, which is why the test is
# on the edge itself and not on the gap.
SUSPECT_EDGE = 0.10


def build():
    sl = pd.read_pickle(SLATE)
    cal = M.load()
    q = FO.latest()
    if q.empty:
        print("no prices stored yet -- run fetch_odds.py")
        return None
    q = q[q.player_id.notna() & q.over_odds.notna() & q.under_odds.notna()]
    # A quote belongs to one game, not to a player. Matching on the player
    # alone would carry Saturday's number onto tonight's board for every
    # club that was not sampled tonight -- a line no book is holding,
    # reported as though it were. The pair is the join.
    pair = {(int(a), int(b)) for a, b in zip(sl.player_id, sl.game_id)}
    before = len(q)
    q = q[[(int(a), int(b)) in pair
           for a, b in zip(q.player_id.fillna(-1), q.game_id.fillna(-1))]]
    if before - len(q):
        print(f"edges: {before - len(q)} of {before} stored quotes are for "
              "other games; they are not tonight's market")
    # Never price a player the model is not pricing: ruled out, or too
    # thin to have a projection at all.
    live = sl[sl.out == 0].set_index("player_id")
    q = q[q.player_id.isin(live.index)]
    if q.empty:
        print("no quotes matched a priceable player on tonight's slate")
        return None

    rows = []
    for (pid, stat, line), grp in q.groupby(["player_id", "stat", "line"]):
        if stat not in cal:
            continue
        p = live.loc[pid]
        if isinstance(p, pd.DataFrame):
            p = p.iloc[0]
        mu = p[f"proj_{stat}"]
        c = cal[stat]
        po, pu, push = M.p_over(mu, c, line)
        sd = float(M.sd(mu, c))
        gap_sd = (line - mu) / sd if sd > 0 else 0.0
        # Each book's own no-vig view of the same number.
        nov = [M.devig(float(r.over_odds), float(r.under_odds))
               for r in grp.itertuples(index=False)]
        cons_o = float(np.median([x[0] for x in nov]))
        n_books = len(grp)
        for (bk, (nv_o, nv_u)) in zip(grp.itertuples(index=False), nov):
            for side, pm, odds, nv in (("over", po, bk.over_odds, nv_o),
                                       ("under", pu, bk.under_odds, nv_u)):
                cons = cons_o if side == "over" else 1 - cons_o
                rows.append({
                    "name": p["name"], "team": p.team, "opp": p.opp,
                    "pos": p.pos, "player_id": int(pid), "stat": stat,
                    "line": line, "side": side, "book": bk.book,
                    "odds": int(odds), "proj": mu, "model": pm,
                    "novig_book": nv, "edge_book": pm - nv,
                    "novig_cons": cons if n_books >= MIN_BOOKS_FOR_CONSENSUS
                                  else np.nan,
                    "edge_cons": (pm - cons)
                                 if n_books >= MIN_BOOKS_FOR_CONSENSUS
                                 else np.nan,
                    "n_books": n_books,
                    "gap_sd": gap_sd, "sd": sd,
                    "ev": M.ev(pm, odds), "kelly": M.kelly(pm, odds),
                    "proj_min": p.proj_min, "play_prob": p.play_prob,
                    "thin": int(p.thin), "push": push,
                })
    d = pd.DataFrame(rows)
    if d.empty:
        return None
    d["suspect"] = (d.edge_cons.abs() > SUSPECT_EDGE).fillna(False).astype(int)
    return d.sort_values("ev", ascending=False).reset_index(drop=True)


def best_per_prop(d):
    """One row per player and market: the best price anyone is offering."""
    return (d.sort_values("ev", ascending=False)
             .groupby(["player_id", "stat"], as_index=False).first())


def side_balance(d):
    """How often the model lands on the over, per market type."""
    best = best_per_prop(d)
    t = best.groupby(["stat", "side"]).size().unstack(fill_value=0)
    if "over" not in t or "under" not in t:
        return None
    t["n"] = t["over"] + t["under"]
    t["over_pct"] = 100 * t["over"] / t["n"]
    return t


def agreement(d):
    """How closely the model tracks the market, over every quoted market."""
    one = d.groupby(["player_id", "stat"], as_index=False).first()
    line, proj = one.line.to_numpy(float), one.proj.to_numpy(float)
    if len(one) < 5:
        return None
    return {"markets": len(one), "corr": float(np.corrcoef(line, proj)[0, 1]),
            "bias": float((line - proj).mean()),
            "mae": float(np.abs(line - proj).mean())}


def for_tool(d):
    """Per player and market: the line the market is on, the best price on
    each side and who is offering it, plus the consensus."""
    out = {}
    for (pid, stat), g in d.groupby(["player_id", "stat"]):
        # Books disagree on the number as well as the price; the one most
        # of them are on is the market's line.
        line = g.line.value_counts().idxmax()
        g = g[g.line == line]
        o = g[g.side == "over"]
        u = g[g.side == "under"]
        if o.empty or u.empty:
            continue
        # American odds order by their numeric value across the sign
        # boundary -- +120 beats -110 beats -145 -- so the best price on
        # a side is simply the largest number.
        bo = o.loc[o.odds.idxmax()]
        bu = u.loc[u.odds.idxmax()]
        cons = o.novig_cons.iloc[0]
        out.setdefault(int(pid), {})[stat] = {
            "line": float(line),
            "over": int(bo.odds), "over_book": bo.book,
            "under": int(bu.odds), "under_book": bu.book,
            "n": int(o.n_books.iloc[0]),
            "cons": None if pd.isna(cons) else round(float(cons), 4),
        }
    return out


def main():
    d = build()
    if d is None:
        # Clear last night's file rather than leave it for the board to
        # bake in. Having no market is an honest state; showing a market
        # that closed yesterday as though it were tonight's is not.
        for f in (OUT, CSV):
            try:
                os.remove(f)
                print(f"edges: removed stale {os.path.basename(f)}")
            except FileNotFoundError:
                pass
        return
    d.to_pickle(OUT)
    d.to_csv(CSV, index=False, float_format="%.4f")
    n_props = d.groupby(["player_id", "stat"]).ngroups
    print(f"edges: {len(d)} quotes across {n_props} player-markets, "
          f"{d.book.nunique()} books -> {CSV}")
    a = agreement(d)
    if a:
        print(f"\nmodel against the market, over all {a['markets']} quoted "
              f"markets: correlation {a['corr']:.3f}, "
              f"mean line minus projection {a['bias']:+.2f}, "
              f"MAE {a['mae']:.2f}")
        if a["corr"] < 0.85 or abs(a["bias"]) > 1.5:
            print("  that is further from the market than it should be -- "
                  "read the tables below as a symptom, not an opportunity.")

    sb = side_balance(d)
    if sb is not None:
        overall = 100 * sb["over"].sum() / sb["n"].sum()
        print(f"\nside the model prefers, per market  "
              f"(over {overall:.0f}% of the time overall):")
        print(sb[["over", "under", "over_pct"]].to_string(
            float_format=lambda x: f"{x:.1f}"))
        if not 40 <= overall <= 60:
            print("  that is a bias, not an edge. A calibrated model facing "
                  "an efficient market should land either side about half "
                  "the time; this one's projections sit systematically to "
                  "one side of the lines, and every disagreement below "
                  "inherits that.")

    cols = ["name", "team", "opp", "stat", "line", "side", "book", "odds",
            "proj", "model", "novig_cons", "edge_cons", "ev", "kelly",
            "n_books"]
    cons = d[d.edge_cons.notna()]
    if not cons.empty:
        ok = cons[cons.suspect == 0]
        print(f"\nagainst the consensus ({MIN_BOOKS_FOR_CONSENSUS}+ books "
              f"on the number), edge under {SUSPECT_EDGE:.0%}:")
        if ok.empty:
            print("  nothing. every multi-book market the model disagrees "
                  "with, it disagrees with by more than the backtest can "
                  "justify -- see below.")
        else:
            t = best_per_prop(ok).nlargest(12, "edge_cons")
            print(t[cols].to_string(index=False,
                                    float_format=lambda x: f"{x:.3f}"))
        sus = cons[cons.suspect == 1]
        if not sus.empty:
            t = best_per_prop(sus).nlargest(8, "edge_cons")
            print(f"\n{sus.groupby(['player_id','stat']).ngroups} markets "
                  f"where the model claims more than {SUSPECT_EDGE:.0%} "
                  "against a multi-book consensus. Read these as a bug "
                  "report on the model, not a bet:")
            print(t[cols[:-4] + ["gap_sd", "edge_cons", "n_books"]].to_string(
                index=False, float_format=lambda x: f"{x:.3f}"))
    else:
        print(f"\nno market has {MIN_BOOKS_FOR_CONSENSUS} books quoting it, "
              "so there is nothing here to measure against a consensus.")

    thin = d[d.edge_cons.isna()]
    if not thin.empty:
        t = best_per_prop(thin).nlargest(8, "ev")
        print(f"\nthin markets, fewer than {MIN_BOOKS_FOR_CONSENSUS} books "
              "-- treat as suspect:")
        print(t[cols[:-4] + ["edge_book", "ev", "n_books"]].to_string(
            index=False, float_format=lambda x: f"{x:.3f}"))
        print("  a thirty-point edge against an efficient market means the "
              "quote is wrong, stale or alone, far more often than it means "
              "the model is right.")


if __name__ == "__main__":
    main()
