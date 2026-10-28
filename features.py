"""Leak-free per-game features for the prop model.

Everything here is built in one chronological pass with exponentially
weighted state, so a row's features only ever see games that finished
before it. That is what makes the backtest honest: no refit, no
look-ahead, and the same code path produces tonight's projections.

The projection factorises into pieces that can be inspected one at a time:

    mean = player rate per minute
         x projected minutes
         x game pace          (possessions per 48, vs league)
         x opponent defence   (allowed per possession, vs league)
         x home/away

Pace and defence are kept separate on purpose. A team that concedes a lot
of points because it plays fast is not the same as a team that concedes a
lot per possession, and a prop model that cannot tell them apart will
chase the wrong teams all season.
"""
import math
import sqlite3
import sys

import numpy as np
import pandas as pd

DIR = __file__.rsplit("/", 1)[0]
DB = DIR + "/data/nba.sqlite3"
OUT = DIR + "/data/features.pkl"

# Stats the model prices. The four combos are fitted as stats in their own
# right rather than summed from marginals -- a sum of three negative
# binomials needs a correlation structure, and points-rebounds-assists
# modelled directly gets the variance right without inventing one.
BASE = ["pts", "reb", "ast", "fg3m", "stl", "blk", "tov", "fga", "fta",
        "oreb", "dreb", "fgm"]
COMBOS = {"pra": ("pts", "reb", "ast"), "pr": ("pts", "reb"),
          "pa": ("pts", "ast"), "ra": ("reb", "ast"), "stocks": ("stl", "blk")}
STATS = BASE + list(COMBOS)

# Swept by tune.py on 2024-25 alone, one axis at a time. K_RATE wanted to
# go to ~5, i.e. almost no shrinkage, but 5 and 20 scored within 0.0004 of
# each other and 20 keeps a thin sample from producing a silly rate, so the
# safer end of a flat optimum wins. SEASON_CARRY cannot be tuned here --
# the scoring season is the first in the data, so no season break falls
# inside it -- and it meets the test seasons untuned.
# Playoff minutes are not regular-season minutes: rotations shorten and
# starters play more (+1.3 minutes in 2024-25, +2.4 in 2025-26, measured
# on players projected for twenty or more). With a three-game half-life on
# the minutes series, the last games before a season break are playoff
# games and they dominate the following October -- which showed up as a
# minutes bias of +2.5 over the first three games of 2025-26 and +1.8 over
# the first three of 2026-27, worth about a point of points each time, and
# invisible in a season-wide average because it washes out by game four.
#
# So a postseason game enters the minutes series scaled to its
# regular-season equivalent, by a league ratio measured from the games
# already played. The scaling is structural, not fitted: projecting a
# October rotation from June basketball is a category error whatever the
# number turns out to be.
SCALE_POSTSEASON_MINUTES = True

HL_MIN = 3.0      # minutes move fast: role changes, blowouts, rest
HL_RATE = 50.0    # per-minute skill moves slowly
HL_DEF = 15.0
HL_PACE = 10.0
# How much of last season survives the break. tune.py cannot reach this --
# its scoring season is the first in the data, so no boundary falls inside
# it -- so it was left at a guess of 0.55 until the market disagreed with
# the model by about a point on every points line and sent me looking.
#
# Swept on the 2024-25 -> 2025-26 boundary and checked on 2025-26 ->
# 2026-27, which it never saw: 0.15 is better than 0.55 on the fitted
# boundary (MAE 4.40 vs 4.45), on the held-out one (4.93 vs 5.01), on the
# held-out bias (+1.05 vs +1.30) and on the rest of the season too (4.74
# vs 4.76). Dropping last season altogether removes the bias almost
# entirely but costs the most accuracy of any setting, which is the trade
# this number exists to make.
SEASON_CARRY = 0.15
K_RATE = 20.0     # prior strength, in minutes, on a player's per-minute rate
K_DEF = 14.0      # prior strength, in games, on an opponent factor
K_PACE = 10.0
MIN_PLAYED = 4.0  # below this the line is garbage time, not a sample


def half_life(hl):
    return 0.5 ** (1.0 / hl)


class Ewma:
    """Exponentially weighted sums, so ratios of two of them are stable."""

    def __init__(self, decay):
        self.d = decay
        self.w = 0.0
        self.s = {}

    def push(self, vals, weight=1.0):
        self.w = self.w * self.d + weight
        for k, v in vals.items():
            self.s[k] = self.s.get(k, 0.0) * self.d + v * weight

    def decay_all(self, f):
        self.w *= f
        for k in self.s:
            self.s[k] *= f

    def get(self, k):
        return self.s.get(k, 0.0)


# ESPN files the All-Star game and its side events under the regular
# season, with rosters named WORLD, STARS, CHK and so on. A 40-point
# exhibition in 20 minutes of no defence would sit in a player's rate for
# a dozen games afterwards, so any side that is not a real franchise --
# fewer than this many games across the whole span -- is dropped along
# with the games it played.
MIN_TEAM_GAMES = 50


def real_teams(con, seasons):
    q = (f"""SELECT team FROM team_lines t JOIN games g USING (game_id)
             WHERE g.season IN ({','.join('?' * len(seasons))})
               AND g.season_type IN (2, 3, 5)
             GROUP BY team HAVING COUNT(*) >= {MIN_TEAM_GAMES}""")
    return [r[0] for r in con.execute(q, list(seasons))]


def load(con, seasons=(2025, 2026, 2027)):
    keep = real_teams(con, seasons)
    inq = ",".join("?" * len(keep))
    q = f"""
      SELECT p.game_id, p.player_id, p.name, p.team, p.opp, p.is_home,
             p.starter, p.dnp, p.min, {', '.join('p.' + c for c in BASE)},
             g.date, g.season, g.season_type
      FROM player_lines p JOIN games g USING (game_id)
      WHERE g.season IN ({','.join('?' * len(seasons))})
        AND g.season_type IN (2, 3, 5)
        AND p.team IN ({inq}) AND p.opp IN ({inq})
      ORDER BY g.date, p.game_id, p.team
    """
    d = pd.read_sql(q, con, params=list(seasons) + keep + keep)
    for name, parts in COMBOS.items():
        d[name] = sum(d[p] for p in parts)
    t = pd.read_sql(f"""
      SELECT t.game_id, t.team, t.opp, {', '.join('t.' + c for c in
            ('pts', 'reb', 'oreb', 'dreb', 'ast', 'stl', 'blk', 'tov',
             'fga', 'fg3a', 'fg3m', 'fta', 'fgm', 'minutes'))},
             g.date, g.season
      FROM team_lines t JOIN games g USING (game_id)
      WHERE g.season IN ({','.join('?' * len(seasons))})
        AND g.season_type IN (2, 3, 5)
        AND t.team IN ({inq}) AND t.opp IN ({inq})
      ORDER BY g.date, t.game_id
    """, con, params=list(seasons) + keep + keep)
    for name, parts in COMBOS.items():
        t[name] = sum(t[p] for p in parts)
    return d, t


def possessions(r):
    """The standard estimate. Rebounds of your own miss do not end a trip."""
    return r["fga"] - r["oreb"] + r["tov"] + 0.44 * r["fta"]


def team_state(t):
    """Per-game, per-team: pace and per-possession allowed rates, from the
    games already played. Returns a dict keyed (game_id, team)."""
    t = t.copy()
    t["poss"] = possessions(t)
    # Each team's own offence, and what it allowed (= the other side's row).
    opp = t.set_index(["game_id", "team"])
    out = {}
    pace = {}            # team -> Ewma over poss per 48 and minutes
    deff = {}            # team -> Ewma over allowed stat and allowed poss
    lg_pace = Ewma(half_life(60))
    lg_def = Ewma(half_life(60))
    season = {}
    for gid, grp in t.groupby("game_id", sort=False):
        rows = list(grp.itertuples(index=False))
        if len(rows) != 2:
            continue
        for i, r in enumerate(rows):
            o = rows[1 - i]
            tm = r.team
            for store, hl in ((pace, HL_PACE), (deff, HL_DEF)):
                if tm not in store:
                    store[tm] = Ewma(half_life(hl))
            if season.get(tm) not in (None, r.season):
                pace[tm].decay_all(SEASON_CARRY)
                deff[tm].decay_all(SEASON_CARRY)
            season[tm] = r.season
            # --- read state before this game is folded in (leak-free)
            mins = max(r.minutes, 1.0)
            lgp = (lg_pace.get("poss") / max(lg_pace.get("min"), 1e-9)) or 0.0
            own_n, def_n = pace[tm].w, deff[tm].w
            own_pace = (pace[tm].get("poss") / max(pace[tm].get("min"), 1e-9)
                        if own_n > 0 else lgp)
            own_pace = _shrink(own_pace, lgp, own_n, K_PACE)
            allowed = {}
            ap = deff[tm].get("poss")
            for s in STATS:
                lg_rate = lg_def.get(s) / max(lg_def.get("poss"), 1e-9)
                rate = (deff[tm].get(s) / ap) if ap > 0 else lg_rate
                allowed[s] = (_shrink(rate, lg_rate, def_n, K_DEF), lg_rate)
            out[(gid, tm)] = {"pace_per_min": own_pace, "lg_pace_per_min": lgp,
                              "allowed": allowed, "def_n": def_n,
                              "pace_n": own_n}
            # --- now fold it in
            pace[tm].push({"poss": r.poss, "min": mins})  # noqa: E501
            lg_pace.push({"poss": r.poss, "min": mins})
            o_poss = max(getattr(o, "poss"), 1.0)
            deff[tm].push({"poss": o_poss,
                           **{s: getattr(o, s) for s in STATS}})
            lg_def.push({"poss": o_poss, **{s: getattr(o, s) for s in STATS}})
    # The same read, once more, after every game: the state tonight's
    # matchups are priced from.
    final = {}
    lgp = lg_pace.get("poss") / max(lg_pace.get("min"), 1e-9)
    for tm in pace:
        own = (pace[tm].get("poss") / max(pace[tm].get("min"), 1e-9)
               if pace[tm].w > 0 else lgp)
        ap = deff[tm].get("poss")
        allowed = {}
        for s in STATS:
            lg_rate = lg_def.get(s) / max(lg_def.get("poss"), 1e-9)
            rate = (deff[tm].get(s) / ap) if ap > 0 else lg_rate
            allowed[s] = (_shrink(rate, lg_rate, deff[tm].w, K_DEF), lg_rate)
        final[tm] = {"pace_per_min": _shrink(own, lgp, pace[tm].w, K_PACE),
                     "lg_pace_per_min": lgp, "allowed": allowed,
                     "def_n": deff[tm].w, "pace_n": pace[tm].w}
    return out, final


def _shrink(x, prior, n, k):
    w = n / (n + k) if n > 0 else 0.0
    return w * x + (1 - w) * prior


def build(d, t):
    ts, team_final = team_state(t)
    hl_m, hl_r = half_life(HL_MIN), half_life(HL_RATE)
    pm = {}      # player -> Ewma of availability over every team game
    pmp = {}     # player -> Ewma of minutes over the games he played
    pr = {}      # player -> Ewma of counting stats and minutes
    lg = Ewma(half_life(400))     # league per-minute rates
    lg_home = Ewma(half_life(400))
    last_date, pseason = {}, {}
    # League minutes per played game, regular season and postseason, so a
    # playoff game can be put on a regular-season footing.
    lg_mins = Ewma(half_life(2000))
    rows = []
    for r in d.itertuples(index=False):
        pid = r.player_id
        if pid not in pm:
            pm[pid] = Ewma(hl_m)
            pmp[pid] = Ewma(hl_m)
            pr[pid] = Ewma(hl_r)
        if pseason.get(pid) not in (None, r.season):
            pm[pid].decay_all(SEASON_CARRY)
            pmp[pid].decay_all(SEASON_CARRY)
            pr[pid].decay_all(SEASON_CARRY)
        pseason[pid] = r.season

        gp = pr[pid].w
        tot_min = pr[pid].get("min")
        # Minutes conditional on playing: a prop is graded only if he
        # suits up, so the DNP zeros belong in the play rate, not here.
        proj_min = (pmp[pid].get("min") / pmp[pid].w) if pmp[pid].w > 0 else 0.0
        start_rate = (pmp[pid].get("start") / pmp[pid].w) if pmp[pid].w > 0 else 0.0
        play_rate = (pm[pid].get("played") / pm[pid].w) if pm[pid].w > 0 else 0.0
        lg_min = max(lg.get("min"), 1e-9)

        tm = ts.get((r.game_id, r.team))
        op = ts.get((r.game_id, r.opp))
        feat = {
            "game_id": r.game_id, "date": r.date, "season": r.season,
            "season_type": r.season_type, "player_id": pid, "name": r.name,
            "team": r.team, "opp": r.opp, "is_home": r.is_home,
            "starter": r.starter, "dnp": r.dnp, "min": r.min,
            "games_seen": gp, "min_seen": tot_min,
            "proj_min": proj_min, "start_rate": start_rate,
            "play_rate": play_rate,
            "rest_days": _rest(last_date, pid, r.date),
        }
        # Pace: the two teams meet in the middle, relative to the league.
        if tm and op and tm["lg_pace_per_min"] > 0:
            exp_pace = 0.5 * (tm["pace_per_min"] + op["pace_per_min"])
            feat["pace_mult"] = exp_pace / tm["lg_pace_per_min"]
        else:
            feat["pace_mult"] = 1.0
        feat["opp_def_n"] = op["def_n"] if op else 0.0

        for s in STATS:
            lg_rate = lg.get(s) / lg_min
            raw = pr[pid].get(s) / max(tot_min, 1e-9) if tot_min > 0 else lg_rate
            # Empirical Bayes in minutes: a player with 220 minutes of
            # history is halfway between himself and the league.
            rate = ((pr[pid].get(s) + K_RATE * lg_rate) /
                    (tot_min + K_RATE)) if tot_min >= 0 else lg_rate
            feat[f"rate_{s}"] = rate
            feat[f"raw_rate_{s}"] = raw
            if op:
                mine, lgr = op["allowed"][s]
                feat[f"opp_{s}"] = (mine / lgr) if lgr > 0 else 1.0
            else:
                feat[f"opp_{s}"] = 1.0
            hw = lg_home.get(f"{s}_home"), lg_home.get("min_home")
            aw = lg_home.get(f"{s}_away"), lg_home.get("min_away")
            if hw[1] > 0 and aw[1] > 0:
                hr, ar = hw[0] / hw[1], aw[0] / aw[1]
                mid = 0.5 * (hr + ar)
                feat[f"home_{s}"] = ((hr if r.is_home else ar) / mid
                                     if mid > 0 else 1.0)
            else:
                feat[f"home_{s}"] = 1.0
            feat[s] = getattr(r, s)
        rows.append(feat)

        # Fold the game in. DNPs update the minutes series (a zero), never
        # the rate series -- a rate needs a denominator.
        if r.dnp or r.min < MIN_PLAYED:
            pm[pid].push({"played": 0.0})
        else:
            pm[pid].push({"played": 1.0})
            m_adj = r.min
            if SCALE_POSTSEASON_MINUTES and r.season_type in (3, 5):
                reg_n, post_n = lg_mins.get("reg_n"), lg_mins.get("post_n")
                if reg_n > 50 and post_n > 50:
                    reg = lg_mins.get("reg") / reg_n
                    post = lg_mins.get("post") / post_n
                    if post > 0:
                        m_adj = r.min * (reg / post)
            pmp[pid].push({"min": m_adj, "start": float(r.starter)})
            vals = {s: getattr(r, s) for s in STATS}
            pr[pid].push({"min": r.min, **vals})
            lg.push({"min": r.min, **vals})
            side_t = "post" if r.season_type in (3, 5) else "reg"
            lg_mins.push({side_t: r.min, side_t + "_n": 1.0})
            side = "home" if r.is_home else "away"
            lg_home.push({f"min_{side}": r.min,
                          **{f"{s}_{side}": getattr(r, s) for s in STATS}})
        last_date[pid] = r.date

    f = pd.DataFrame(rows)
    for s in STATS:
        f[f"proj_{s}"] = (f[f"rate_{s}"] * f["proj_min"] * f["pace_mult"] *
                          f[f"opp_{s}"] * f[f"home_{s}"])

    # Final per-player state, for tonight. Same numbers the last row of
    # the frame was built from, read one game later.
    lg_min = max(lg.get("min"), 1e-9)
    players = {}
    for pid in pr:
        tot = pr[pid].get("min")
        st = {"games_seen": pr[pid].w, "min_seen": tot,
              "proj_min": (pmp[pid].get("min") / pmp[pid].w
                           if pmp[pid].w > 0 else 0.0),
              "start_rate": (pmp[pid].get("start") / pmp[pid].w
                             if pmp[pid].w > 0 else 0.0),
              "play_rate": (pm[pid].get("played") / pm[pid].w
                            if pm[pid].w > 0 else 0.0),
              "last_date": last_date.get(pid)}
        for s in STATS:
            lg_rate = lg.get(s) / lg_min
            st[f"rate_{s}"] = (pr[pid].get(s) + K_RATE * lg_rate) / (tot + K_RATE)
        players[pid] = st
    home_f = {}
    for s in STATS:
        hw = lg_home.get(f"{s}_home"), lg_home.get("min_home")
        aw = lg_home.get(f"{s}_away"), lg_home.get("min_away")
        if hw[1] > 0 and aw[1] > 0:
            hr, ar = hw[0] / hw[1], aw[0] / aw[1]
            mid = 0.5 * (hr + ar)
            home_f[s] = (hr / mid, ar / mid) if mid > 0 else (1.0, 1.0)
        else:
            home_f[s] = (1.0, 1.0)
    return f, {"players": players, "teams": team_final, "home": home_f}


def _rest(last, pid, today):
    p = last.get(pid)
    if not p:
        return 99
    a = pd.Timestamp(today) - pd.Timestamp(p)
    return min(int(a.days), 99)


def main():
    con = sqlite3.connect(DB)
    d, t = load(con)
    print(f"loaded {len(d)} player-game lines, {len(t)} team lines")
    f, state = build(d, t)
    f.to_pickle(OUT)
    pd.to_pickle(state, DIR + "/data/state.pkl")
    played = f[(f.dnp == 0) & (f["min"] >= MIN_PLAYED)]
    print(f"features: {len(f)} rows -> {OUT}  ({len(played)} played)")
    print(f"state: {len(state['players'])} players, "
          f"{len(state['teams'])} teams -> data/state.pkl")
    pr = played[played.games_seen >= 10]
    for s in ("pts", "reb", "ast", "fg3m", "pra"):
        e = pr[f"proj_{s}"] - pr[s]
        print(f"  {s:6s} n={len(pr)}  mean {pr[s].mean():5.2f}  "
              f"proj {pr[f'proj_{s}'].mean():5.2f}  "
              f"MAE {e.abs().mean():5.2f}  bias {e.mean():+.3f}")


if __name__ == "__main__":
    main()
