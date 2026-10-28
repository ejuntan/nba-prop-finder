"""Team defence, in the form people actually read it.

The model's own opponent adjustment is an exponentially weighted rate per
possession, shrunk toward the league -- good for projecting, unreadable on
a page. This builds the conventional view beside it: defensive rating,
pace, and what a team allows per 100 possessions in each market, each with
its rank among the thirty.

Two windows, because in October they say different things. The current
season is what a book's narrative is built on and is three games deep; the
last ten games is a real sample and crosses the season break. Both are
reported with their game count so the thin one is obvious.

It also splits what a defence concedes by the position of the player
scoring it -- guards, forwards, centres, as ESPN files them. A team that
is middling overall can be the league's softest touch against centres, and
that is the number that matters when the prop in hand belongs to one.

The denominator is the same possessions the team faced either way, so the
three positional rates for a stat add up to the team total for it. They
are ranked within position: 4th against guards means fourth-stingiest
against guards, not fourth overall.

Rank 1 always means stingiest -- allows the fewest. So a high rank is good
news for an over.
"""
import sqlite3
import sys

import numpy as np
import pandas as pd

from features import COMBOS, DB, DIR, STATS, real_teams

OUT = DIR + "/data/defense.pkl"
WINDOW = 10
POS = ["G", "F", "C"]
# A player ESPN has no position for gets his own bucket rather than being
# dropped: the three shown positions have to add back to the team total,
# and silently losing a row would break that by exactly as much as he
# scored. Nothing ranks or displays this bucket.
# Whatever the three positions do not account for goes in a fourth bucket
# rather than being lost. Two things land there: the odd player ESPN has
# no listed position for, and the 24 team-games in three seasons where its
# own totals row disagrees with the athlete list it ships beside it (a
# missing player, or a stale total -- it happens both ways). Carrying the
# residual keeps "the positions add back to the team total" true by
# construction, which is what makes the split safe to read. Nothing ranks
# or displays this bucket.
POS_ALL = POS + ["?"]
POS_LABEL = {"G": "guards", "F": "forwards", "C": "centres",
             "?": "unattributed"}
BASE_COLS = ["pts", "reb", "oreb", "dreb", "ast", "stl", "blk", "tov",
             "fga", "fta", "fgm", "fg3m"]
# Shown on the page in this order; the rest are still computed.
SHOW = ["pts", "reb", "ast", "fg3m", "stl", "blk", "tov", "fga", "fta",
        "pra", "pr", "pa", "ra", "stocks"]


def team_games(con, seasons=(2025, 2026, 2027)):
    """One row per team-game: what the team did, and what it allowed."""
    keep = real_teams(con, seasons)
    inq = ",".join("?" * len(keep))
    cols = ["pts", "reb", "oreb", "dreb", "ast", "stl", "blk", "tov",
            "fga", "fg3a", "fg3m", "fta", "fgm", "minutes"]
    t = pd.read_sql(f"""
        SELECT t.game_id, t.team, t.opp, t.is_home,
               {', '.join('t.' + c for c in cols)}, g.date, g.season
        FROM team_lines t JOIN games g USING (game_id)
        WHERE g.season IN ({','.join('?' * len(seasons))})
          AND g.season_type IN (2, 3, 5)
          AND t.team IN ({inq}) AND t.opp IN ({inq})
        ORDER BY g.date, t.game_id
    """, con, params=list(seasons) + keep + keep)
    for name, parts in COMBOS.items():
        t[name] = sum(t[p] for p in parts)
    t["poss"] = t.fga - t.oreb + t.tov + 0.44 * t.fta
    # Pace is possessions per 48 minutes of team time; overtime inflates
    # the raw count, so divide it back out.
    t["pace"] = t.poss * 240.0 / t.minutes.clip(lower=1)
    # What this team allowed = the other side's line in the same game.
    other = t.set_index(["game_id", "team"])
    opp = t.merge(
        other[["poss"] + STATS].add_prefix("a_").reset_index()
        .rename(columns={"team": "opp"}),
        on=["game_id", "opp"], how="left")
    return opp


def player_games(con, seasons=(2025, 2026, 2027)):
    """Opposing player lines, tagged with the position that produced them."""
    keep = real_teams(con, seasons)
    inq = ",".join("?" * len(keep))
    d = pd.read_sql(f"""
        SELECT p.game_id, p.team, p.opp, p.min,
               {', '.join('p.' + c for c in BASE_COLS)},
               COALESCE(x.pos, '?') AS pos, g.date, g.season
        FROM player_lines p
        JOIN games g USING (game_id)
        LEFT JOIN positions x ON x.player_id = p.player_id
        WHERE g.season IN ({','.join('?' * len(seasons))})
          AND g.season_type IN (2, 3, 5)
          AND p.dnp = 0
          AND p.team IN ({inq}) AND p.opp IN ({inq})
    """, con, params=list(seasons) + keep + keep)
    for name, parts in COMBOS.items():
        d[name] = sum(d[p] for p in parts)
    # The row belongs to the defence it was scored against. Keep every
    # row: a player who logged under a minute and still took a rebound is
    # in the team's total, so he has to be in the split too.
    d = d.rename(columns={"team": "scorer_team", "opp": "team"})
    d.loc[~d.pos.isin(POS), "pos"] = "?"
    return d


def window_pos(dp, poss, teamtab, n=None, season=None):
    """Per-100-possession rates allowed to each position, ranked within it.

    Possessions come from the team table, not from summing player rows:
    the denominator has to be the trips the defence actually faced, or a
    team that concedes to one position more than another would look like
    it faced more possessions against it."""
    out = {}
    for team, g in dp.groupby("team", sort=False):
        g = g.sort_values(["date", "game_id"])
        gids = poss.get(team)
        if gids is None:
            continue
        order = [x for x in gids["order"]]
        if season is not None:
            order = [x for x in order if gids["season"][x] == season]
        if n is not None:
            order = order[-n:]
        keep_ids = set(order)
        ap = sum(gids["a_poss"][x] for x in order)
        if ap <= 0:
            continue
        gg = g[g.game_id.isin(keep_ids)]
        for pos in POS:
            sub = gg[gg.pos == pos]
            row = {"team": team, "pos": pos, "games": len(order),
                   "minutes": sub["min"].sum()}
            for s in STATS:
                row[s] = 100 * sub[s].sum() / ap
            out[(team, pos)] = row
        # The residual, so the three shown positions plus this equal the
        # team figure exactly.
        if team in teamtab.index:
            res = {"team": team, "pos": "?", "games": len(order),
                   "minutes": gg[~gg.pos.isin(POS)]["min"].sum()}
            for s in STATS:
                res[s] = teamtab.loc[team, s] - sum(
                    out[(team, q)][s] for q in POS)
            out[(team, "?")] = res
    t = pd.DataFrame(out.values())
    if t.empty:
        return t
    t = t.set_index(["team", "pos"])
    for c in STATS:
        t[c + "_rank"] = t.groupby("pos")[c].rank(method="min").astype(int)
        t[c + "_vs_lg"] = t[c] / t.groupby("pos")[c].transform("mean")
    return t


def poss_index(d):
    """Per team: its games in order, each with the possessions it faced."""
    out = {}
    for team, g in d.groupby("team", sort=False):
        g = g.sort_values(["date", "game_id"])
        out[team] = {"order": list(g.game_id),
                     "a_poss": dict(zip(g.game_id, g.a_poss)),
                     "season": dict(zip(g.game_id, g.season))}
    return out


def window_stats(d, n=None, season=None):
    """Per-100-possession rates over a team's last n games, or a season."""
    rows = []
    for team, g in d.groupby("team", sort=False):
        g = g.sort_values(["date", "game_id"])
        if season is not None:
            g = g[g.season == season]
        if n is not None:
            g = g.tail(n)
        if g.empty:
            continue
        ap = max(g.a_poss.sum(), 1e-9)
        own = max(g.poss.sum(), 1e-9)
        r = {"team": team, "games": len(g),
             "from": g.date.iloc[0], "to": g.date.iloc[-1],
             "drtg": 100 * g.a_pts.sum() / ap,
             "ortg": 100 * g.pts.sum() / own,
             "pace": g.pace.mean()}
        for s in STATS:
            r[s] = 100 * g["a_" + s].sum() / ap
        rows.append(r)
    t = pd.DataFrame(rows).set_index("team")
    # Rank 1 = allows the fewest. Defensive rating follows the same rule,
    # so rank 1 is the best defence either way.
    for c in ["drtg"] + STATS:
        t[c + "_rank"] = t[c].rank(method="min").astype(int)
    t["pace_rank"] = t["pace"].rank(method="min", ascending=False).astype(int)
    for c in ["drtg", "pace"] + STATS:
        t[c + "_vs_lg"] = t[c] / t[c].mean()
    return t


def build(con, seasons=(2025, 2026, 2027)):
    d = team_games(con, seasons)
    dp = player_games(con, seasons)
    season = int(d.season.max())
    px = poss_index(d)
    t10 = window_stats(d, n=WINDOW)
    tsn = window_stats(d, season=season)
    t30 = window_stats(d, n=30)
    return {"last10": t10, "season": tsn, "last30": t30,
            "pos_last10": window_pos(dp, px, t10, n=WINDOW),
            "pos_season": window_pos(dp, px, tsn, season=season),
            "pos_last30": window_pos(dp, px, t30, n=30),
            "current_season": season,
            "positions": POS,
            "n_teams": d.team.nunique()}


def summary(tab, team, stat, label):
    t = tab.loc[team]
    n = int(t["games"])
    return {"window": label, "games": n, "from": t["from"], "to": t["to"],
            "drtg": float(t["drtg"]), "drtg_rank": int(t["drtg_rank"]),
            "pace": float(t["pace"]), "pace_rank": int(t["pace_rank"]),
            "allowed": float(t[stat]), "rank": int(t[stat + "_rank"]),
            "vs_lg": float(t[stat + "_vs_lg"])}


def main():
    con = sqlite3.connect(DB)
    out = build(con)
    pd.to_pickle(out, OUT)
    l10 = out["last10"]
    print(f"defense: {out['n_teams']} teams, "
          f"last {WINDOW} games and season {out['current_season']} "
          f"-> {OUT}")
    stat = sys.argv[1] if len(sys.argv) > 1 else "pts"
    t = l10.sort_values(stat)[["games", "drtg", "drtg_rank", "pace",
                               "pace_rank", stat, stat + "_rank"]]
    print(f"\nlast {WINDOW} games, {stat} allowed per 100 possessions "
          f"(rank 1 = stingiest)")
    print(t.to_string(float_format=lambda x: f"{x:7.2f}"))
    pos = out["pos_last10"]
    for p in POS:
        sub = pos.xs(p, level="pos")[[stat, stat + "_rank"]].sort_values(stat)
        print(f"\nvs {POS_LABEL[p]}: softest five for {stat}")
        print(sub.tail(5).iloc[::-1].to_string(
            float_format=lambda x: f"{x:7.2f}"))


if __name__ == "__main__":
    main()
