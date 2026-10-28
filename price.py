"""The research bench: one prop at a time, or a whole sheet of them.

    price.py "jokic"                         everything on him tonight
    price.py "jokic" pra 44.5                one prop, model side only
    price.py "jokic" pra 44.5 -115 -105      with the book's two-way price
    price.py "jokic" reb 12.5 --last 20      a longer game log
    price.py edges                           tonight's real lines, ranked
    price.py scan out/lines.csv              a sheet of lines -> ranked edges
    price.py board pra 20                    tonight's biggest projections
    price.py def GS reb                      one team's defence, ranked
    price.py def GS reb C                    ... and only against centres

A price here is conditional on him playing. The availability number is
reported next to it and never multiplied into it, because a prop that
does not get graded is a refund, not a loss.
"""
import re
import sqlite3
import sys
from difflib import get_close_matches

import numpy as np
import pandas as pd

import defense as DEF
import model as M
from features import DB, DIR, STATS
from project import build as build_slate

SLATE = DIR + "/data/slate.pkl"
FEAT = DIR + "/data/features.pkl"
EDGES = DIR + "/out/edges.csv"
MAIN = ["pts", "reb", "ast", "pra", "fg3m", "pr", "pa", "ra", "stocks",
        "tov", "fga", "fta", "stl", "blk"]
DEFENSE = DIR + "/data/defense.pkl"
WINDOW = "last10"
WINDOW_LABEL = {"last10": "last 10 games", "last30": "last 30 games",
                "season": "this season"}


def load_slate():
    try:
        return pd.read_pickle(SLATE)
    except FileNotFoundError:
        d, _ = build_slate()
        return d


def initials(name):
    """SGA, KD, CP3 -- hyphens and apostrophes count as word breaks."""
    return "".join(w[0] for w in re.split(r"[\s.'-]+", name.lower()) if w)


def match(sl, query):
    """Everyone a query could mean, best interpretation first."""
    q = query.strip().lower()
    if not q:
        return sl.iloc[0:0]
    hit = sl[sl.name.str.lower().str.contains(q, regex=False)]
    if len(hit):
        # A surname beats a stray substring: "green" should not be
        # ambiguous because someone's first name contains it.
        last = hit[hit.name.str.lower().str.split().str[-1].str.startswith(q)]
        return last if len(last) else hit
    ini = re.sub(r"[^a-z]", "", q)
    if len(ini) > 1:
        hit = sl[sl.name.map(lambda n: initials(n).startswith(ini))]
        if len(hit):
            return hit
    words = q.split()
    if len(words) > 1:
        hit = sl[sl.name.str.lower().map(
            lambda n: all(w in n for w in words))]
        if len(hit):
            return hit
    return sl.iloc[0:0]


def find(sl, query):
    """Name lookup for the interactive path: one player, or an explanation."""
    q = query.strip().lower()
    hit = match(sl, query)
    if len(hit) == 1:
        return hit.iloc[0]
    if len(hit) > 1:
        print(f"{len(hit)} players match {query!r}:")
        for r in hit.itertuples(index=False):
            print(f"  {r.name}  ({r.team} vs {r.opp})")
        sys.exit(1)
    near = get_close_matches(q, [n.lower() for n in sl.name], n=4, cutoff=0.5)
    if near:
        print(f"no one on tonight's slate matches {query!r}. Did you mean:")
        for n in near:
            print("  " + sl[sl.name.str.lower() == n].iloc[0]["name"])
    else:
        print(f"no one on tonight's slate matches {query!r}")
    sys.exit(1)


def recent(player_id, stat, n=10):
    f = pd.read_pickle(FEAT)
    d = f[(f.player_id == player_id) & (f.dnp == 0)].sort_values("date")
    return d.tail(n)[["date", "opp", "is_home", "min", stat]]


def market_line(player_id, stat):
    """The market's number for this prop, if any has been fetched."""
    try:
        import edges as E
        d = pd.read_pickle(DIR + "/data/edges.pkl")
    except (FileNotFoundError, ImportError):
        return None
    if d is None or d.empty:
        return None
    t = E.for_tool(d).get(int(player_id)) or {}
    return t.get(stat)


def defense_table():
    """Team defence, from the cache if it is there and fresh enough."""
    try:
        return pd.read_pickle(DEFENSE)
    except FileNotFoundError:
        con = sqlite3.connect(DB)
        d = DEF.build(con)
        pd.to_pickle(d, DEFENSE)
        return d


def ordinal(n):
    n = int(n)
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def defense(team, stat, window=WINDOW, pos=None):
    """What this defence concedes, and where that ranks -- overall, and to
    the position of the player in hand."""
    d = defense_table()
    tab = d.get(window)
    if tab is None or team not in tab.index:
        print(f"\n  no defensive record for {team} in this window")
        return
    t, n = tab.loc[team], len(tab)
    soft = "soft" if t[stat + "_rank"] >= n - 7 else (
        "stingy" if t[stat + "_rank"] <= 8 else "middling")
    print(f"\n  {team} defence  ({WINDOW_LABEL.get(window, window)}: "
          f"{int(t['games'])} games, {t['from']} to {t['to']})")
    print(f"    defensive rating {t['drtg']:.1f}  "
          f"{ordinal(t['drtg_rank'])} of {n}   "
          f"pace {t['pace']:.1f}  {ordinal(t['pace_rank'])} fastest")
    print(f"    {M.PRETTY[stat].lower()} allowed {t[stat]:.2f} per 100 "
          f"possessions   {ordinal(t[stat + '_rank'])} of {n} ({soft}), "
          f"{t[stat + '_vs_lg'] - 1:+.1%} vs league")
    ptab = d.get("pos_" + window)
    if pos and ptab is not None and (team, pos) in ptab.index:
        q = ptab.loc[(team, pos)]
        r = int(q[stat + "_rank"])
        lab = DEF.POS_LABEL.get(pos, pos)
        soft_p = "soft" if r >= n - 7 else ("stingy" if r <= 8 else "middling")
        print(f"    ... to {lab}: {q[stat]:.2f} per 100   "
              f"{ordinal(r)} of {n} ({soft_p}), "
              f"{q[stat + '_vs_lg'] - 1:+.1%} vs league")
    print("    rank 1 allows the fewest; the three positions share the "
          "possessions the defence faced, so they add back to the total.")
    print("    the model's own factor is milder than any of this, and is "
          "the one that moves the number.")


def defense_board(team, stat="pts", pos=None, window=WINDOW):
    d = defense_table()
    tab = d.get(window)
    if tab is None:
        return
    pos = (pos or "").upper() or None
    if team.upper() in tab.index:
        return defense(team.upper(), stat, window, pos)
    if pos:
        ptab = d.get("pos_" + window)
        sub = ptab.xs(pos, level="pos")[[stat, stat + "_rank"]]
        print(f"\n{M.PRETTY[stat]} allowed per 100 possessions to "
              f"{DEF.POS_LABEL.get(pos, pos)}, "
              f"{WINDOW_LABEL.get(window, window)} (rank 1 = stingiest)")
        print(sub.sort_values(stat).to_string(
            float_format=lambda x: f"{x:7.2f}"))
        return
    cols = ["games", "drtg", "drtg_rank", "pace", "pace_rank", stat,
            stat + "_rank"]
    print(f"\n{M.PRETTY[stat]} allowed per 100 possessions, "
          f"{WINDOW_LABEL.get(window, window)} (rank 1 = stingiest)")
    print(tab.sort_values(stat)[cols].to_string(
        float_format=lambda x: f"{x:7.2f}"))


def versus(player_id, opp, stat):
    f = pd.read_pickle(FEAT)
    d = f[(f.player_id == player_id) & (f.opp == opp) & (f.dnp == 0)]
    return d.sort_values("date")[["date", "is_home", "min", stat]]


def header(r):
    where = f"at {r.opp}" if not r.is_home else f"vs {r.opp}"
    print(f"\n{r['name']}  ({r.team} {where}, {r.tipoff[:16].replace('T', ' ')} UTC)")
    if r.get("out"):
        print(f"  RULED OUT"
              + (f" -- {r.status}" if pd.notna(r.status) else "")
              + (f", {r.injury}" if pd.notna(r.get("injury")) else ""))
        print(f"  Nothing below is a price. He normally plays "
              f"{r.base_min:.1f} minutes; those are his team's to "
              f"redistribute, and the model has already done so.")
        return
    st = f"  status: {r.status}" if pd.notna(r.status) else ""
    inj = f" ({r.injury})" if pd.notna(r.get("injury")) else ""
    pos = (f", {DEF.POS_LABEL[r['pos']][:-1]}"
           if r.get("pos") in DEF.POS else "")
    thin = ("   THIN RECORD: only %.0f games behind this, the rate leans "
            "on the league mean" % r.games_seen) if r.get("thin") else ""
    print(f"  projected minutes {r.proj_min:.1f}{pos}   availability "
          f"{100 * r.play_prob:.0f}%{st}{inj}{thin}")
    print(f"  game pace {r.pace_mult - 1:+.1%} vs league    "
          f"starts {100 * r.start_rate:.0f}% of games    "
          f"own recent minutes {r.base_min:.1f}, "
          f"after the team's 240-minute budget x{r.min_scale:.2f}")
    if r.play_prob < 1.0 and pd.notna(r.status):
        print("  availability is reported, not priced in: an ungraded prop "
              "is a refund.")


def one(r, stat, line, over_odds=None, under_odds=None, cal=None):
    cal = cal or M.load()
    c = cal[stat]
    mu = r[f"proj_{stat}"]
    po, pu, push = M.p_over(mu, c, line)
    q = M.quantiles(mu, c)
    print(f"\n  {M.PRETTY[stat]}  line {line}")
    print(f"    projection {mu:.2f}   sd {float(M.sd(mu, c)):.2f}   "
          f"model's own even-money line {M.fair_line(mu, c)}")
    print(f"    deciles  10% {q[0]}   25% {q[1]}   median {q[2]}   "
          f"75% {q[3]}   90% {q[4]}")
    pushtxt = f"   push {push:.1%}" if push > 0 else ""
    print(f"    over  {po:6.2%}  fair {M.prob_to_american(po):+d}     "
          f"under {pu:6.2%}  fair {M.prob_to_american(pu):+d}{pushtxt}")
    if over_odds is None:
        return None
    no, nu = M.devig(over_odds, under_odds) if under_odds is not None \
        else (M.american_to_prob(over_odds), None)
    hold = M.hold(over_odds, under_odds) if under_odds is not None else np.nan
    print(f"    book  over {over_odds:+d}"
          + (f"  under {under_odds:+d}   hold {hold:.2%}" if under_odds
             is not None else ""))
    print(f"    no-vig  over {no:.2%}" + (f"   under {nu:.2%}" if nu else ""))
    rows = []
    for side, p, odds, fair in (("over", po, over_odds, no),
                                ("under", pu, under_odds, nu)):
        if odds is None:
            continue
        e = M.ev(p, odds)
        rows.append({"side": side, "model": p, "no_vig": fair,
                     "edge": p - fair, "ev": e, "kelly": M.kelly(p, odds)})
    t = pd.DataFrame(rows)
    best = t.loc[t.ev.idxmax()]
    print(t.to_string(index=False, float_format=lambda x: f"{x:+.4f}"))
    verdict = (f"    -> {best.side.upper()} {line}, edge {best.edge:+.1%}, "
               f"EV {best.ev:+.1%} per unit, Kelly {best.kelly:.1%}"
               if best.ev > 0 else
               f"    -> no bet; the better side ({best.side}) is still "
               f"{best.ev:+.1%}")
    print(verdict)
    return best


def form(r, stat, line=None, n=10, pos=None):
    d = recent(r.player_id, stat, n)
    if d.empty:
        return
    tab = defense_table().get(WINDOW)
    print(f"\n  last {len(d)} games played")
    s = d.copy()
    s["H/A"] = np.where(s.is_home == 1, "H", "A")
    # What each of those defences ranks today -- context for the matchup,
    # not a record of what it ranked on the night.
    if tab is not None:
        ptab = defense_table().get("pos_" + WINDOW)
        if pos and ptab is not None:
            s["oppD"] = [ordinal(ptab.loc[(o, pos), stat + "_rank"])
                         if (o, pos) in ptab.index else "-" for o in s.opp]
        else:
            s["oppD"] = [ordinal(tab.loc[o, stat + "_rank"])
                         if o in tab.index else "-" for o in s.opp]
    if line is not None:
        s["hit"] = np.where(s[stat] > line, "over", "")
    s = s.drop(columns=["is_home"])
    print("    " + s.to_string(index=False).replace("\n", "\n    "))
    if line is not None:
        print(f"    over {line}: {(d[stat] > line).sum()}/{len(d)} "
              f"({(d[stat] > line).mean():.0%})   mean {d[stat].mean():.2f}")
    v = versus(r.player_id, r.opp, stat)
    if not v.empty:
        extra = f", over {line} in {(v[stat] > line).sum()}/{len(v)}" \
            if line is not None else ""
        print(f"  vs {r.opp} (last {len(v)} meetings): "
              f"mean {v[stat].mean():.2f} in {v['min'].mean():.1f} min{extra}")
        print("    careful: a handful of past meetings is a small sample, "
              "and rosters move.")


def scan(path):
    """A sheet of lines -> ranked edges. Columns: name, stat, line,
    over_odds, under_odds (odds optional)."""
    sl, cal = load_slate(), M.load()
    want = pd.read_csv(path)
    want.columns = [c.strip().lower() for c in want.columns]
    out = []
    for w in want.itertuples(index=False):
        hit = match(sl, str(w.name))
        if hit.empty:
            print(f"  skipped: no match for {w.name!r} on tonight's slate")
            continue
        r = hit.iloc[0]
        stat = str(w.stat).strip().lower()
        if stat not in cal:
            print(f"  skipped: unknown stat {w.stat!r}")
            continue
        c, mu, line = cal[stat], r[f"proj_{stat}"], float(w.line)
        po, pu, push = M.p_over(mu, c, line)
        oo = getattr(w, "over_odds", None)
        uo = getattr(w, "under_odds", None)
        if r.get("out"):
            print(f"  skipped: {r['name']} is ruled out")
            continue
        row = {"name": r["name"], "team": r.team, "opp": r.opp, "stat": stat,
               "line": line, "proj": mu, "proj_min": r.proj_min,
               "status": r.status, "play_prob": r.play_prob,
               "p_over": po, "p_under": pu,
               "fair_over": M.prob_to_american(po),
               "fair_under": M.prob_to_american(pu)}
        if pd.notna(oo) and pd.notna(uo):
            no, nu = M.devig(float(oo), float(uo))
            cands = [("over", po, float(oo), no), ("under", pu, float(uo), nu)]
            side, p, odds, fair = max(cands, key=lambda z: M.ev(z[1], z[2]))
            row.update({"over_odds": oo, "under_odds": uo,
                        "hold": M.hold(float(oo), float(uo)), "side": side,
                        "edge": p - fair, "ev": M.ev(p, odds),
                        "kelly": M.kelly(p, odds)})
        else:
            row.update({"side": "over" if po > 0.5 else "under",
                        "edge": np.nan, "ev": np.nan, "kelly": np.nan})
        out.append(row)
    if not out:
        print("nothing priced")
        return
    d = pd.DataFrame(out)
    d = d.sort_values("ev" if d.ev.notna().any() else "p_over",
                      ascending=False)
    d.to_csv(EDGES, index=False, float_format="%.4f")
    cols = ["name", "team", "opp", "stat", "line", "proj", "side", "p_over",
            "fair_over", "fair_under", "edge", "ev", "kelly", "play_prob"]
    print(d[[c for c in cols if c in d]].to_string(
        index=False, float_format=lambda x: f"{x:.3f}"))
    print(f"\n{len(d)} props priced -> {EDGES}")
    if d.ev.notna().any():
        pos = d[d.ev > 0]
        print(f"{len(pos)} with positive EV at the posted price")


def board(stat="pra", n=25):
    sl, cal = load_slate(), M.load()
    d = sl[(sl.out == 0) & (sl.play_prob > 0.5)].nlargest(int(n),
                                                          f"proj_{stat}")
    cols = ["team", "opp", "name", "status", "proj_min", f"proj_{stat}",
            f"line_{stat}"]
    print(f"\ntonight's biggest {M.PRETTY[stat]} projections")
    print(d[cols].to_string(index=False, float_format=lambda x: f"{x:.1f}"))
    print("\nthe line column is the model's own even-money number, "
          "not a book's.")


def main():
    argv = sys.argv[1:]
    last = 10
    if "--last" in argv:
        i = argv.index("--last")
        last = int(argv[i + 1])
        del argv[i:i + 2]
    a = argv
    if not a:
        sys.exit(__doc__)
    if a[0] == "edges":
        import edges as E
        return E.main()
    if a[0] == "def":
        return defense_board(*(a[1:] or ["", "pts"])[:3])
    if a[0] == "scan":
        return scan(a[1] if len(a) > 1 else DIR + "/out/lines.csv")
    if a[0] == "board":
        return board(*(a[1:] or ["pra"]))
    sl, cal = load_slate(), M.load()
    r = find(sl, a[0])
    header(r)
    if len(a) == 1 and r.get("out"):
        form(r, "pts", None, n=last,
             pos=r.get("pos") if r.get("pos") in DEF.POS else None)
        return
    if len(a) == 1:
        for s in MAIN:
            mu = r[f"proj_{s}"]
            line = M.fair_line(mu, cal[s])
            po = M.p_over(mu, cal[s], line)[0]
            print(f"  {M.PRETTY[s]:<14} {mu:6.2f}   even-money line {line:>5}"
                  f"   over there {po:.1%}   sd {float(M.sd(mu, cal[s])):.2f}")
        print("\nadd a stat and a line for the full card, e.g. "
              f'price.py "{r["name"].split()[-1].lower()}" pra 44.5 -115 -105')
        return
    stat = a[1].lower()
    if stat not in cal:
        sys.exit(f"unknown stat {stat!r}; one of: {', '.join(MAIN)}")
    # With no line given, use the market's if one has been fetched, and
    # its prices with it -- a fair-value number with no price attached is
    # the model talking to itself.
    mk = market_line(r.player_id, stat)
    if len(a) > 2:
        line = float(a[2])
        oo = int(a[3]) if len(a) > 3 else None
        uo = int(a[4]) if len(a) > 4 else None
    elif mk:
        line, oo, uo = mk["line"], mk["over"], mk["under"]
        print(f"\n  using the market: {line} at {oo:+d} / {uo:+d} "
              f"({mk['over_book']} / {mk['under_book']}, "
              f"{mk['n']} book{'' if mk['n'] == 1 else 's'} quoting)")
    else:
        line = M.fair_line(r[f"proj_{stat}"], cal[stat])
        oo = uo = None
    pos = r.get("pos") if r.get("pos") in DEF.POS else None
    if not r.get("out"):
        one(r, stat, line, oo, uo, cal)
    defense(r.opp, stat, pos=pos)
    form(r, stat, line, n=last, pos=pos)


if __name__ == "__main__":
    main()
