"""Everything the browser tool needs, in one JSON file.

The page does its own pricing. Shipping a probability per prop would mean
shipping one per line a book might hang, so instead it gets the projected
mean, the fitted dispersion and the isotonic correction table, and
evaluates the negative binomial itself. That way any line typed into it is
priced by the same constants the backtest was scored on.
"""
import json
import sqlite3
from datetime import datetime, timezone

import numpy as np
import pandas as pd

import defense as DEF
import model as M
from features import COMBOS, DB, DIR, STATS

SLATE = DIR + "/data/slate.pkl"
FEAT = DIR + "/data/features.pkl"
OUT = DIR + "/out/tool_data.json"
BOX = ["min", "pts", "reb", "ast", "fg3m", "stl", "blk", "tov", "fga", "fta"]
SHOW = ["pts", "reb", "ast", "pra", "pr", "pa", "ra", "fg3m", "stl", "blk",
        "stocks", "tov", "fga", "fta"]
RECENT = 20          # the page shows the last 5/10/15/20 of these
WINDOWS = [("last10", "Last 10 games"), ("last30", "Last 30 games"),
           ("season", "This season")]


def r2(x, n=3):
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return None
    return round(float(x), n)


def scorecard():
    """Two numbers for the Method page. The page used to carry the whole
    scorecard; it is a thing you read once and then want out of the way, so
    the tables live in out/scorecard.txt and only the headline ships."""
    try:
        grid = pd.read_pickle(DIR + "/data/backtest_grid.pkl")
    except FileNotFoundError:
        return None
    g = grid[(grid.p_over > 0.02) & (grid.p_over < 0.98)]
    base = g.over.mean()
    worst = 0.0
    for _, b in g.groupby(pd.cut(g.p_over, np.linspace(0, 1, 11)),
                          observed=True):
        worst = max(worst, abs(b.over.mean() - b.p_over.mean()))
    return {"n_pairs": int(len(g)),
            "brier": r2(((g.p_over - g.over) ** 2).mean(), 4),
            "brier_base": r2(((base - g.over) ** 2).mean(), 4),
            "worst_decile_gap": r2(worst, 4)}


def defense_block(con):
    """Each team's defence, in the two or three windows worth comparing,
    overall and split by the position of the player who scored it."""
    d = DEF.build(con)
    teams, windows = {}, []
    for key, label in WINDOWS:
        tab = d.get(key)
        if tab is None or tab.empty:
            continue
        windows.append({"key": key, "label": label})
        ptab = d.get("pos_" + key)
        for team, t in tab.iterrows():
            w = teams.setdefault(team, {})
            by_pos = {}
            if ptab is not None and not ptab.empty:
                for pos in DEF.POS:
                    if (team, pos) not in ptab.index:
                        continue
                    q = ptab.loc[(team, pos)]
                    by_pos[pos] = {s: [r2(q[s], 2), int(q[s + "_rank"]),
                                       r2(q[s + "_vs_lg"], 4)]
                                   for s in DEF.SHOW}
            w[key] = {
                "by_pos": by_pos,
                "games": int(t["games"]), "from": t["from"], "to": t["to"],
                "drtg": r2(t["drtg"], 2), "drtg_rank": int(t["drtg_rank"]),
                "ortg": r2(t["ortg"], 2),
                "pace": r2(t["pace"], 2), "pace_rank": int(t["pace_rank"]),
                # [allowed per 100 possessions, rank, vs league]
                "allowed": {s: [r2(t[s], 2), int(t[s + "_rank"]),
                                r2(t[s + "_vs_lg"], 4)] for s in DEF.SHOW},
            }
    return {"windows": windows, "teams": teams,
            "positions": [{"key": p, "label": DEF.POS_LABEL[p]}
                          for p in DEF.POS],
            "season": d["current_season"], "n": d["n_teams"]}


def book_block():
    """Real prices, if any have been fetched. Absent is a normal state --
    the tool works without a key, it just has nothing to compare against."""
    try:
        import edges as E
        d = pd.read_pickle(DIR + "/data/edges.pkl")
    except (FileNotFoundError, ImportError):
        return {}, {}
    if d is None or d.empty:
        return {}, {}
    by_player = E.for_tool(d)
    meta = {"books": sorted(d.book.unique()),
            "props": int(d.groupby(["player_id", "stat"]).ngroups),
            "quotes": int(len(d))}
    return by_player, meta


def main():
    sl = pd.read_pickle(SLATE)
    cal = M.load()
    f = pd.read_pickle(FEAT)
    f = f[f.dnp == 0]
    books, bookmeta = book_block()
    con = sqlite3.connect(DB)
    seasons = pd.read_sql(
        "SELECT season, COUNT(*) n, MIN(date) a, MAX(date) b FROM games "
        "WHERE state='post' AND season_type IN (2,3,5) GROUP BY season", con)

    # One row per game as a bare array under a shared header, not a dict
    # per game: the keys are the same ten strings 2,800 times over, and
    # spelling them out was two thirds of the payload. The page rebuilds
    # the objects once on load.
    rcols = ["d", "o", "h"] + BOX
    recent = {}
    for pid, grp in f[f.player_id.isin(sl.player_id)].groupby("player_id"):
        g = grp.sort_values("date").tail(RECENT)
        # The full date, not just month-day: a twenty-game window reaches
        # back across the season break and into the playoffs, and the page
        # cannot tell June from October without the year.
        recent[int(pid)] = [
            [r.date, r.opp, int(r.is_home)] +
            [int(round(getattr(r, k))) for k in BOX]
            for r in g.itertuples(index=False)]

    players = []
    for r in sl.itertuples(index=False):
        players.append({
            "id": int(r.player_id), "name": r.name, "team": r.team,
            "opp": r.opp, "home": int(r.is_home), "tip": r.tipoff,
            "gid": int(r.game_id),
            "pos": r.pos, "thin": int(r.thin), "out": int(r.out),
            "status": None if pd.isna(r.status) else r.status,
            "injury": None if pd.isna(r.injury) else r.injury,
            "play": r2(r.play_prob), "min": r2(r.proj_min, 1),
            "base_min": r2(r.base_min, 1), "min_scale": r2(r.min_scale),
            "pace": r2(r.pace_mult), "starts": r2(r.start_rate),
            "seen": r2(r.games_seen, 1),
            "book": books.get(int(r.player_id)) or None,
            "mu": {s: r2(getattr(r, f"proj_{s}")) for s in SHOW},
            "oppf": {s: r2(getattr(r, f"opp_{s}")) for s in SHOW},
            "line": {s: r2(getattr(r, f"line_{s}"), 1) for s in SHOW},
        })

    games = []
    for gid, grp in sl.groupby("game_id", sort=False):
        a = grp.iloc[0]
        home = grp[grp.is_home == 1].team.iloc[0] if (grp.is_home == 1).any() \
            else a.team
        away = grp[grp.is_home == 0].team.iloc[0] if (grp.is_home == 0).any() \
            else a.opp
        games.append({"gid": int(gid), "home": home, "away": away,
                      "tip": a.tipoff, "pace": r2(
                          grp[grp.team == home].pace_mult.iloc[0]
                          if (grp.team == home).any() else a.pace_mult)})

    data = {
        "meta": {
            "slate": sl.date.iloc[0],
            "built": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%MZ"),
            "games": int(sl.game_id.nunique()),
            "players": len(players),
            "seasons": [{"season": int(r.season), "games": int(r.n),
                         "from": r.a, "to": r.b}
                        for r in seasons.itertuples(index=False)],
            "source": "ESPN public NBA API",
            "book": bookmeta or None,
        },
        "stats": [{"key": s, "label": M.PRETTY[s],
                   "combo": list(COMBOS[s]) if s in COMBOS else None}
                  for s in SHOW],
        "calib": {s: {"c": r2(cal[s]["c"], 6), "p": r2(cal[s]["p"], 6),
                      "shape": [r2(v, 5) for v in cal[s]["shape"]]}
                  for s in SHOW},
        "games": games, "recent_cols": rcols, "recent": recent,
        "players": players,
        "defense": defense_block(con),
        "scorecard": scorecard(),
    }
    with open(OUT, "w") as fh:
        json.dump(data, fh, separators=(",", ":"))
    import os
    print(f"export: {len(players)} players "
          f"({sum(p['out'] for p in players)} ruled out), {len(games)} games, "
          f"{len(data['defense']['teams'])} team defences -> {OUT} "
          f"({os.path.getsize(OUT) / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
