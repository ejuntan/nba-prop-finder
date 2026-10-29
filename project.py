"""Tonight's slate: a projection and a price for every priceable player.

Minutes are the whole game. A projection is a per-minute rate multiplied
by minutes, so getting the minutes wrong is the only way to be badly
wrong, and the thing that moves minutes most is who is not playing. So:

  * every available player's baseline is his own recent minutes-when-he-
    played, weighted by how likely he is to be available tonight,
  * the ESPN injury report knocks out the ones who are out and discounts
    the questionable,
  * a player ruled out stays on the board, greyed and unpriced, because
    who is missing is the thing that moves everyone else's minutes and a
    board that silently deletes him tells you nothing,
  * and the survivors are adjusted so the team's expected minutes add to
    240, which is what a team actually plays (240.7 on average last
    season, the difference being overtime). A starter ruled out therefore
    lifts everyone behind him, which is where most of the night's genuine
    prop value lives.

The adjustment is not spread evenly. A team whose parts add to 266 is
not wrong about its starting centre by 10%; it is wrong about the three
bench players who each went for twenty minutes once. So the correction is
weighted by role -- a 38-minute starter absorbs almost none of it, a
10-minute reserve absorbs most -- and the weight each player took is
reported, because in October, with two or three games on the board, these
minutes are the shakiest number in the whole model.

Nothing here refits. The rates, the pace and defence factors and the
dispersion constants all come from data/state.pkl and data/calib.json.
"""
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

import model as M
from features import DB, DIR, STATS
from fetch import today as et_today

STATE = DIR + "/data/state.pkl"
OUT = DIR + "/data/slate.pkl"
CSV = DIR + "/out/slate.csv"
LOG = DIR + "/data/slate_log.sqlite3"

# Every slate is appended here before it is overwritten. Grading a line
# later needs the projection that was live when the line was quoted, and
# the causal features cannot reproduce it exactly: they carry no injury
# report and no 240-minute budget, which are the two things that move a
# number most on the night. So the number that was actually shown is the
# number that gets judged.
LOG_COLS = ["date", "game_id", "player_id", "name", "team", "opp", "pos",
            "is_home", "status", "play_prob", "out", "thin", "games_seen",
            "proj_min", "base_min", "min_scale", "pace_mult"]

TEAM_MINUTES = 240.0
MAX_MINUTES = 41.0
ROSTER_WINDOW = 21      # days back to decide who is still on the team
MIN_PROJ_MIN = 8.0      # below this a prop is not worth pricing
# The only history requirement is model.MIN_GAMES_SEEN, which is one game.
# A player who has never played a prior season is priced from what he has
# done, shrunk hard toward the league, and carries `thin` so the number is
# never read as if it had a career behind it.

# How likely the injury report leaves him available. "Out" is the only
# status that is really information; the rest are a book's problem too.
PLAY_PROB = {"out": 0.0, "doubtful": 0.15, "questionable": 0.55,
             "day-to-day": 0.75, "probable": 0.9, "available": 1.0,
             "game-time decision": 0.5}

LINES_HINT = {"pts": 2.0, "reb": 1.0, "ast": 1.0, "pra": 3.0, "fg3m": 0.5,
              "pr": 2.5, "pa": 2.5, "ra": 1.5, "stl": 0.5, "blk": 0.5,
              "tov": 0.5, "stocks": 0.5, "fga": 1.5, "fta": 1.0}


def next_slate(con, when=None):
    """The date to price: the next day with a game still to tip.

    Not simply the next date on or after today. A run late in the
    evening -- a scheduled job that fired on wake, say -- would otherwise
    price a slate whose games had already been played, since those games
    still carry today's Eastern date. What makes a slate the next one is
    that something on it has not started, so that is what is asked.
    """
    if when:
        return when
    # Eastern, to match the dates in the database: at 11pm on the west
    # coast the machine's own date is already tomorrow for half the year.
    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    row = con.execute(
        "SELECT MIN(date) FROM games WHERE date >= ? AND season_type IN (2,3,5)"
        "  AND state NOT IN ('post','in') AND tipoff > ?",
        (et_today().isoformat(), now)).fetchone()
    if row and row[0]:
        return row[0]
    # Nothing left to tip that we know of: fall back to the old answer
    # rather than returning nothing, and let the caller report an empty
    # slate if that is what it is.
    row = con.execute(
        "SELECT MIN(date) FROM games WHERE date >= ? AND season_type IN (2,3,5)",
        (et_today().isoformat(),)).fetchone()
    return row[0] or et_today().isoformat()


def games_on(con, day):
    return pd.read_sql(
        "SELECT game_id, date, tipoff, home, away, state FROM games "
        "WHERE date = ? AND season_type IN (2,3,5) ORDER BY tipoff",
        con, params=(day,))


def rosters(con, day, teams):
    """Who has played for each team lately, and when he last played."""
    start = (pd.Timestamp(day) - pd.Timedelta(days=ROSTER_WINDOW)).date()
    q = f"""
      SELECT p.team, p.player_id, p.name, MAX(g.date) AS last_date,
             COUNT(*) AS games, AVG(p.min) AS avg_min,
             COALESCE(x.pos, '?') AS pos
      FROM player_lines p JOIN games g USING (game_id)
      LEFT JOIN positions x ON x.player_id = p.player_id
      WHERE g.date BETWEEN ? AND ? AND p.dnp = 0 AND p.min >= 4
        AND p.team IN ({','.join('?' * len(teams))})
      GROUP BY p.team, p.player_id
    """
    return pd.read_sql(q, con, params=[start.isoformat(), day] + list(teams))


def injuries(con):
    d = pd.read_sql("SELECT team, player_id, name, status, detail FROM injuries",
                    con)
    d["key"] = d.status.fillna("").str.strip().str.lower()
    return d


def role_weight(m):
    """How much of the team's minutes correction this player absorbs.
    A heavy-minute starter's role is known; a reserve's is not."""
    return float(np.clip(1.0 - min(m / 38.0, 1.0), 0.05, 1.0))


def budget(cand, target=TEAM_MINUTES, rounds=4):
    """Adjust minutes so expected team minutes hit the target, putting the
    correction where the uncertainty is. Returns (minutes by id, alpha)."""
    m = {c[0].player_id: c[1]["proj_min"] for c in cand}
    pp = {c[0].player_id: c[3] for c in cand}
    w = {k: role_weight(v) for k, v in m.items()}
    alpha = 0.0
    for _ in range(rounds):
        exp = sum(m[k] * pp[k] for k in m)
        denom = sum(m[k] * pp[k] * w[k] for k in m)
        if denom <= 1e-9:
            break
        a = float(np.clip((target - exp) / denom, -0.9, 3.0))
        if abs(a) < 1e-4:
            break
        alpha += a
        for k in m:
            m[k] = float(np.clip(m[k] * (1 + a * w[k]), 0.5, MAX_MINUTES))
    return m, alpha


def build(day=None, when=None):
    con = sqlite3.connect(DB)
    st = pd.read_pickle(STATE)
    cal = M.load()
    day = next_slate(con, day or when)
    g = games_on(con, day)
    if g.empty:
        print(f"no games on {day}")
        return None, day
    teams = sorted(set(g.home) | set(g.away))
    ros = rosters(con, day, teams)
    inj = injuries(con)
    inj_by = {(r.team, r.player_id): r for r in inj.itertuples(index=False)}
    players, home_f = st["players"], st["home"]
    tstate = st["teams"]

    rows = []
    for gm in g.itertuples(index=False):
        for team, opp, is_home in ((gm.home, gm.away, 1), (gm.away, gm.home, 0)):
            tm, op = tstate.get(team), tstate.get(opp)
            pace = 1.0
            if tm and op and tm["lg_pace_per_min"] > 0:
                pace = (0.5 * (tm["pace_per_min"] + op["pace_per_min"]) /
                        tm["lg_pace_per_min"])
            side = ros[ros.team == team]
            cand = []
            for r in side.itertuples(index=False):
                s = players.get(r.player_id)
                if not s or s["proj_min"] <= 0:
                    continue
                ij = inj_by.get((team, r.player_id))
                status = (ij.status if ij is not None else None)
                pp = PLAY_PROB.get((ij.key if ij is not None else ""),
                                   None)
                if pp is None:
                    pp = min(max(s["play_rate"], 0.35), 1.0)
                cand.append((r, s, status, pp,
                             ij.detail if ij is not None else None))
            mins_by, alpha = budget(cand)
            # Over every candidate, not just the ones that survive the
            # filters below -- the bench players who are dropped from the
            # board still use up minutes.
            team_exp = sum(mins_by[c[0].player_id] * c[3] for c in cand)
            for r, s, status, pp, detail in cand:
                mins = mins_by[r.player_id]
                row = {
                    "date": day, "game_id": gm.game_id, "tipoff": gm.tipoff,
                    "team": team, "opp": opp, "is_home": is_home,
                    "player_id": r.player_id, "name": r.name, "pos": r.pos,
                    "status": status, "injury": detail, "play_prob": pp,
                    "games_seen": s["games_seen"], "start_rate": s["start_rate"],
                    "thin": int(s["games_seen"] <= M.THIN_GAMES),
                    "base_min": s["proj_min"], "proj_min": mins,
                    "min_scale": mins / max(s["proj_min"], 1e-9),
                    "min_alpha": alpha, "team_exp_min": team_exp,
                    "n_candidates": len(cand), "pace_mult": pace,
                    "recent_games": r.games, "recent_avg_min": r.avg_min,
                    "last_played": r.last_date,
                }
                for stat in STATS:
                    oppf = (op["allowed"][stat][0] / op["allowed"][stat][1]
                            if op and op["allowed"][stat][1] > 0 else 1.0)
                    hf = home_f[stat][0 if is_home else 1]
                    raw = s[f"rate_{stat}"] * mins * pace * oppf * hf
                    mu = float(M.mean(raw, cal[stat]))
                    row[f"opp_{stat}"] = oppf
                    row[f"proj_{stat}"] = mu
                    row[f"line_{stat}"] = M.fair_line(mu, cal[stat])
                rows.append(row)
    d = pd.DataFrame(rows)
    d["out"] = (d.play_prob <= 0).astype(int)
    # Someone ruled out is kept for context, not for pricing, and only if
    # his absence is worth noting -- a ten-minute reserve being out is not
    # news. His expected minutes are already zero in the budget above, so
    # the players who inherit them are projected correctly either way.
    priceable = ((d.proj_min >= MIN_PROJ_MIN) &
                 (d.games_seen >= M.MIN_GAMES_SEEN) & (d.out == 0))
    noted = (d.out == 1) & (d.base_min >= MIN_PROJ_MIN)
    d = d[priceable | noted].reset_index(drop=True)
    d = d.sort_values(["tipoff", "team", "proj_pts"],
                      ascending=[True, True, False]).reset_index(drop=True)
    con.close()
    return d, day


def log_slate(d, stats):
    """Append tonight's projections, keyed by date and player."""
    con = sqlite3.connect(LOG)
    cols = LOG_COLS + [f"proj_{s}" for s in stats]
    decl = ", ".join(f'"{c}"' for c in cols)
    con.execute(f"""CREATE TABLE IF NOT EXISTS slate_log (
        {', '.join(f'"{c}"' for c in cols)},
        logged TEXT, PRIMARY KEY (date, game_id, player_id))""")
    con.execute("CREATE INDEX IF NOT EXISTS i_sl_date ON slate_log(date)")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [tuple(r) + (now,) for r in d[cols].itertuples(index=False)]
    con.executemany(
        f"INSERT OR REPLACE INTO slate_log ({decl}, logged) "
        f"VALUES ({','.join('?' * (len(cols) + 1))})", rows)
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM slate_log").fetchone()[0]
    days = con.execute("SELECT COUNT(DISTINCT date) FROM slate_log").fetchone()[0]
    con.close()
    return len(rows), n, days


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    d, day = build(args[0] if args else None)
    if d is None or d.empty:
        return
    d.to_pickle(OUT)
    cols = (["date", "team", "opp", "name", "status", "play_prob", "proj_min",
             "pace_mult"] +
            [c for s in ("pts", "reb", "ast", "pra", "fg3m")
             for c in (f"proj_{s}", f"line_{s}")])
    d[cols].to_csv(CSV, index=False, float_format="%.2f")
    wrote, total, days = log_slate(d, STATS)
    print(f"logged {wrote} projections ({total} rows over {days} days "
          f"-> data/slate_log.sqlite3)")
    thin = int(d[d.out == 0].thin.sum())
    print(f"slate {day}: {d.game_id.nunique()} games, "
          f"{int((d.out == 0).sum())} priceable players "
          f"({thin} on a thin record), {int(d.out.sum())} ruled out")
    print(f"  -> {OUT}\n  -> {CSV}")
    out = d.loc[(d.out == 0) & (d.play_prob > 0.5)].nlargest(12, "proj_pra")
    show = out[["team", "opp", "name", "status", "proj_min", "proj_pts",
                "proj_reb", "proj_ast", "proj_pra", "line_pra"]]
    print("\ntop projected Pts+Reb+Ast:")
    print(show.to_string(index=False, float_format=lambda x: f"{x:.1f}"))
    sidelined = d[d.out == 1]
    if not sidelined.empty:
        print(f"\nruled out ({len(sidelined)}), kept on the board unpriced:")
        print(sidelined.nlargest(10, "base_min")[
            ["team", "opp", "name", "pos", "status", "base_min"]]
            .to_string(index=False, float_format=lambda x: f"{x:.1f}"))
    doubt = d[(d.out == 0) & (d.play_prob < 0.7)]
    if not doubt.empty:
        print(f"{len(doubt)} more are questionable and priced at a "
              "discounted availability")


if __name__ == "__main__":
    main()
