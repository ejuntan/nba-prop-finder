"""ESPN's public NBA API -> SQLite.

stats.nba.com and cdn.nba.com both answer 403 from this machine, so the
source here is the same one the NHL model uses for prices: ESPN's public
site API. It carries everything a prop model needs -- minutes, points,
rebounds, assists, threes, steals, blocks, turnovers and shot attempts,
per player per game -- in one request per game.

Stages, all resumable:
  games      scoreboard per calendar day -> the schedule and final scores
  boxes      summary per event -> player and team box score lines
  teams      the thirty clubs, so a sportsbook's "Denver Nuggets" can be
             matched to this database's DEN
  positions  one lightweight call per player -> guard / forward / centre
  injuries   the report hanging off each upcoming event

Re-running costs only the new games: anything in boxes_done is skipped.
"""
import json
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter

DIR = __file__.rsplit("/", 1)[0]
DB = DIR + "/data/nba.sqlite3"
SB = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
SUM = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary"
ATH = ("https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/"
       "athletes/{}")
TEAMS = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# Season year is ESPN's: 2027 means the 2026-27 season. A season's games
# fall between October 1 of the prior calendar year and June 30.
SEASONS = [2025, 2026, 2027]
# A game's date is the US Eastern date it tips on, not the UTC one. A
# 10pm Eastern tip is already tomorrow in UTC, so storing the UTC date
# splits a single night's slate across two days -- five games filed under
# one date and the rest under the next, with last night's late games
# sitting on the same date as tonight's early one. Every date in this
# database is therefore Eastern, and so is "today".
TZ = ZoneInfo("America/New_York")
WORKERS = 10
# The scoreboard endpoint is the touchy one: ten days of it at once comes
# back as empty 200s, repeatably, while three at a time does not. The box
# scores take the full width happily, so only this stage is throttled.
DAY_WORKERS = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
  game_id     INTEGER PRIMARY KEY,
  date        TEXT,      -- US Eastern game date, YYYY-MM-DD
  tipoff      TEXT,      -- ISO timestamp, UTC
  season      INTEGER,
  season_type INTEGER,   -- 1 pre, 2 regular, 3 post, 4 play-in
  home        TEXT, away TEXT,
  home_id     INTEGER, away_id INTEGER,
  home_score  INTEGER, away_score INTEGER,
  state       TEXT,      -- pre / in / post
  neutral     INTEGER
);
CREATE INDEX IF NOT EXISTS i_g_date   ON games(date);
CREATE INDEX IF NOT EXISTS i_g_season ON games(season, season_type);

CREATE TABLE IF NOT EXISTS player_lines (
  game_id INTEGER, player_id INTEGER,
  name TEXT, team TEXT, opp TEXT, is_home INTEGER,
  starter INTEGER, dnp INTEGER, dnp_reason TEXT,
  min REAL, pts INTEGER, reb INTEGER, oreb INTEGER, dreb INTEGER,
  ast INTEGER, stl INTEGER, blk INTEGER, tov INTEGER, pf INTEGER,
  fgm INTEGER, fga INTEGER, fg3m INTEGER, fg3a INTEGER,
  ftm INTEGER, fta INTEGER, plus_minus INTEGER,
  PRIMARY KEY (game_id, player_id)
);
CREATE INDEX IF NOT EXISTS i_pl_player ON player_lines(player_id);
CREATE INDEX IF NOT EXISTS i_pl_team   ON player_lines(team);

CREATE TABLE IF NOT EXISTS team_lines (
  game_id INTEGER, team TEXT, opp TEXT, is_home INTEGER,
  pts INTEGER, reb INTEGER, oreb INTEGER, dreb INTEGER, ast INTEGER,
  stl INTEGER, blk INTEGER, tov INTEGER, pf INTEGER,
  fgm INTEGER, fga INTEGER, fg3m INTEGER, fg3a INTEGER,
  ftm INTEGER, fta INTEGER, minutes REAL,
  PRIMARY KEY (game_id, team)
);

CREATE TABLE IF NOT EXISTS boxes_done (game_id INTEGER PRIMARY KEY, ok INTEGER);
CREATE TABLE IF NOT EXISTS days_done  (day TEXT PRIMARY KEY, n INTEGER,
                                      at TEXT);

CREATE TABLE IF NOT EXISTS teams (
  team TEXT PRIMARY KEY, team_id INTEGER, display TEXT,
  location TEXT, nickname TEXT
);

CREATE TABLE IF NOT EXISTS positions (
  player_id INTEGER PRIMARY KEY, name TEXT, pos TEXT, pos_name TEXT
);

CREATE TABLE IF NOT EXISTS injuries (
  team TEXT, player_id INTEGER, name TEXT, status TEXT, detail TEXT,
  fetched TEXT, PRIMARY KEY (team, player_id)
);
"""

_local = threading.local()


def _session():
    s = getattr(_local, "s", None)
    if s is None:
        s = requests.Session()
        s.headers["User-Agent"] = UA
        s.mount("https://", HTTPAdapter(pool_connections=WORKERS * 2,
                                        pool_maxsize=WORKERS * 2))
        _local.s = s
    return s


def get(url, params=None, tries=5):
    """GET with backoff. None means the endpoint says there is nothing.

    A body that is not JSON counts as a failure like any other: under load
    ESPN answers 200 with an HTML error page rather than a status code, so
    the decode is what fails. The backoff doubles, because the usual cause
    is a burst of our own making -- several full runs in a few minutes --
    and the cure is to wait, not to try harder.
    """
    wait = 2.0
    for a in range(tries):
        try:
            r = _session().get(url, params=params, timeout=40)
            if r.status_code in (400, 404):
                return None
            r.raise_for_status()
            if not r.content:
                # A 200 with nothing in it. Not an error status, not JSON.
                raise ValueError("empty body")
            return r.json()
        except Exception as e:
            # Throw the session away before retrying. An empty 200 is
            # usually a pooled connection ESPN has given up on, and every
            # retry down the same socket comes back exactly as empty --
            # which is how five tries in a row can all fail while the very
            # next request on a fresh connection succeeds.
            _local.s = None
            if a == tries - 1:
                raise RuntimeError(
                    f"{url} failed {tries} times in a row; last was "
                    f"{type(e).__name__}. An empty 200 means ESPN will "
                    "not serve this one request -- sometimes a single "
                    "date, for hours.") from e
            time.sleep(wait)
            wait = min(wait * 2, 20.0)


def connect():
    con = sqlite3.connect(DB, timeout=60)
    con.executescript(SCHEMA)
    # Databases built before the freshness check have no days_done.at.
    # Add the column rather than make anyone recrawl three seasons.
    if "at" not in [r[1] for r in con.execute("PRAGMA table_info(days_done)")]:
        con.execute("ALTER TABLE days_done ADD COLUMN at TEXT")
        con.commit()
    con.execute("PRAGMA journal_mode=WAL")
    return con


# ---------------------------------------------------------------- games

# How far ahead to crawl. The schedule is published well in advance, and
# ESPN groups its scoreboard by its own notion of the date, so a game that
# tips late Eastern is returned by the *next* day's query -- stopping at
# today leaves tonight's late games stale.
LOOKAHEAD = 8


def season_days(season):
    """Every calendar day a game of this season could fall on. The range
    runs a day wide at each end, since ESPN groups its scoreboard by its
    own date and a game can land either side of the Eastern boundary."""
    d = date(season - 1, 9, 29)
    end = min(date(season, 7, 1), today() + timedelta(days=LOOKAHEAD))
    while d <= end:
        yield d
        d += timedelta(days=1)


def scrape_day(day):
    """One calendar day's games, or None if ESPN would not serve it.

    None is not "no games": it means the day could not be read, so the
    caller leaves it out of days_done and tries again next run. ESPN
    serves the odd date as an empty 200 for hours at a time -- 2026-10-26
    did it while every other day in the same window answered -- and a
    whole chain that stops because one past day is briefly unavailable is
    worse than a board built from the other twelve."""
    try:
        j = get(SB, {"dates": day.strftime("%Y%m%d")})
    except Exception as e:
        print(f"  {day}: {type(e).__name__} -- left for the next run",
              flush=True)
        return None
    rows = []
    for e in (j or {}).get("events", []):
        c = e["competitions"][0]
        sides = {x["homeAway"]: x for x in c["competitors"]}
        if "home" not in sides or "away" not in sides:
            continue
        h, a = sides["home"], sides["away"]
        ha, aa = _abb(h["team"]), _abb(a["team"])
        if not ha or not aa:
            continue  # All-Star and exhibition sides have no abbreviation
        rows.append((
            int(e["id"]), game_date(e["date"]), e["date"],
            e.get("season", {}).get("year"), e.get("season", {}).get("type"),
            ha, aa,
            int(h["team"]["id"]), int(a["team"]["id"]),
            _int(h.get("score")), _int(a.get("score")),
            e["status"]["type"]["state"], int(bool(c.get("neutralSite"))),
        ))
    return rows


def game_date(iso):
    """The Eastern calendar date this tipoff belongs to."""
    t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return t.astimezone(TZ).date().isoformat()


def today():
    return datetime.now(tz=TZ).date()


def _abb(team):
    """ESPN omits the abbreviation on non-club sides (All-Star rosters)."""
    return team.get("abbreviation") or team.get("shortDisplayName") or None


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# A day crawled this recently is not crawled again. The refetch window
# is thirteen days wide and goes out in parallel, so a chain run several
# times an hour -- a scheduled job, a click, an auto-refresh -- is a burst
# ESPN answers by throttling, and a schedule does not change in minutes.
FRESH_MINUTES = 30


def stage_games(seasons, refetch_days=4, fresh_minutes=FRESH_MINUTES):
    """Schedule and finals. Days already crawled are skipped, except the
    last few -- a game's state and score change after the day is first seen
    -- and except nothing at all if it was crawled in the last half hour."""
    con = connect()
    rows = con.execute("SELECT day, at FROM days_done").fetchall()
    done = {r[0] for r in rows}
    ago = (datetime.now(tz=timezone.utc)
           - timedelta(minutes=fresh_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fresh = {d for d, at in rows if at and at >= ago}
    cutoff = today() - timedelta(days=refetch_days)
    todo = []
    for s in seasons:
        for d in season_days(s):
            iso = d.isoformat()
            if iso in fresh:
                continue
            if iso not in done or d >= cutoff:
                todo.append(d)
    if fresh and not todo:
        print(f"games: every day in the window was crawled in the last "
              f"{fresh_minutes} minutes; nothing to do")
    else:
        print(f"games: {len(todo)} days to crawl"
              + (f" ({len(fresh)} still fresh)" if fresh else ""))
    n = 0
    failed = []
    with ThreadPoolExecutor(DAY_WORKERS) as ex:
        for day, rows in zip(todo, ex.map(scrape_day, todo)):
            if rows is None:
                failed.append(day)
                continue
            con.executemany(
                "INSERT OR REPLACE INTO games VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows)
            con.execute("INSERT OR REPLACE INTO days_done VALUES (?,?,?)",
                        (day.isoformat(), len(rows),
                         datetime.now(tz=timezone.utc)
                         .strftime("%Y-%m-%dT%H:%M:%SZ")))
            n += len(rows)
            if n and not len(rows) % 97:
                con.commit()
    con.commit()
    tot = con.execute("SELECT COUNT(*) FROM games").fetchone()[0]
    fin = con.execute(
        "SELECT COUNT(*) FROM games WHERE state='post'").fetchone()[0]
    print(f"games: {n} rows written, {tot} in table, {fin} final")
    if failed:
        print(f"games: {len(failed)} of {len(todo)} days could not be read "
              "and were left for the next run: "
              + ", ".join(d.isoformat() for d in failed[:6]))
        # A few bad days is ESPN being ESPN. Most of the window failing is
        # an outage or a broken client, and must not pass as a good run.
        if len(failed) > max(3, len(todo) // 2):
            raise RuntimeError(
                f"{len(failed)} of {len(todo)} days failed -- that is an "
                "outage, not a bad date; nothing downstream should run on "
                "this")
    con.close()


# ---------------------------------------------------------------- boxes

# ESPN labels each stat column; read by label, never by position.
SMAP = {
    "minutes": "min", "points": "pts", "rebounds": "reb",
    "offensiveRebounds": "oreb", "defensiveRebounds": "dreb",
    "assists": "ast", "steals": "stl", "blocks": "blk",
    "turnovers": "tov", "fouls": "pf", "plusMinus": "plus_minus",
}
SPLIT = {
    "fieldGoalsMade-fieldGoalsAttempted": ("fgm", "fga"),
    "threePointFieldGoalsMade-threePointFieldGoalsAttempted": ("fg3m", "fg3a"),
    "freeThrowsMade-freeThrowsAttempted": ("ftm", "fta"),
}
COUNTS = ("pts", "reb", "oreb", "dreb", "ast", "stl", "blk", "tov", "pf",
          "fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "plus_minus")
PCOLS = ("game_id", "player_id", "name", "team", "opp", "is_home", "starter",
         "dnp", "dnp_reason", "min") + COUNTS
TCOLS = ("game_id", "team", "opp", "is_home", "pts", "reb", "oreb", "dreb",
         "ast", "stl", "blk", "tov", "pf", "fgm", "fga", "fg3m", "fg3a",
         "ftm", "fta", "minutes")


def _num(v):
    if v in (None, "", "-", "--"):
        return None
    try:
        return int(v)
    except ValueError:
        try:
            return float(v)
        except ValueError:
            return None


def parse_stats(keys, stats):
    """One athlete's or team's stat array -> a dict of model columns."""
    out = {}
    for k, v in zip(keys, stats):
        if k in SMAP:
            out[SMAP[k]] = _num(v)
        elif k in SPLIT:
            made, att = SPLIT[k]
            a, b = (v.split("-", 1) + [""])[:2] if isinstance(v, str) else ("", "")
            out[made], out[att] = _num(a), _num(b)
    return out


def parse_box(game_id, j):
    """summary JSON -> (player rows, team rows). Empty if ESPN has no box."""
    bs = (j or {}).get("boxscore") or {}
    blocks = bs.get("players") or []
    if len(blocks) != 2:
        return [], []
    abb = [b["team"]["abbreviation"] for b in blocks]
    # homeAway lives on the header, not inside the boxscore block.
    comp = ((j.get("header") or {}).get("competitions") or [{}])[0]
    home_abb = None
    for c in comp.get("competitors", []):
        if c.get("homeAway") == "home":
            home_abb = (c.get("team") or {}).get("abbreviation")
    prows, trows = [], []
    for i, b in enumerate(blocks):
        team, opp = abb[i], abb[1 - i]
        is_home = int(team == home_abb) if home_abb else int(i == 0)
        groups = b.get("statistics") or []
        if not groups:
            continue
        g = groups[0]
        keys = g.get("keys") or []
        for a in g.get("athletes") or []:
            ath = a.get("athlete") or {}
            pid = _int(ath.get("id"))
            if pid is None:
                continue
            stats = a.get("stats") or []
            dnp = int(bool(a.get("didNotPlay")) or not stats)
            d = parse_stats(keys, stats)
            mins = d.get("min")
            if mins is None and not dnp:
                dnp = 1
            prows.append((
                game_id, pid, ath.get("displayName"), team, opp, is_home,
                int(bool(a.get("starter"))), dnp, a.get("reason"),
                float(mins) if mins is not None else 0.0,
            ) + tuple((d.get(c) or 0) if not dnp else 0 for c in COUNTS))
        tot = g.get("totals")
        if tot:
            d = parse_stats(keys, tot)
            trows.append((game_id, team, opp, is_home) +
                         tuple(d.get(c) or 0 for c in TCOLS[4:-1]) +
                         (sum(r[9] for r in prows if r[3] == team),))
    return prows, trows


def scrape_box(game_id):
    j = get(SUM, {"event": str(game_id)})
    return game_id, parse_box(game_id, j)


def stage_boxes(seasons, limit=None):
    con = connect()
    done = {r[0] for r in con.execute("SELECT game_id FROM boxes_done WHERE ok=1")}
    q = ("SELECT game_id FROM games WHERE state='post' AND season_type IN (2,3,5) "
         f"AND season IN ({','.join('?' * len(seasons))}) ORDER BY date")
    todo = [r[0] for r in con.execute(q, seasons) if r[0] not in done]
    if limit:
        todo = todo[:limit]
    print(f"boxes: {len(todo)} games to fetch ({len(done)} already stored)")
    t0, n, bad = time.time(), 0, 0
    with ThreadPoolExecutor(WORKERS) as ex:
        for i, (gid, (prows, trows)) in enumerate(
                ex.map(scrape_box, todo), start=1):
            if not prows:
                bad += 1
            con.executemany(
                f"INSERT OR REPLACE INTO player_lines VALUES ({','.join('?' * len(PCOLS))})",
                prows)
            con.executemany(
                f"INSERT OR REPLACE INTO team_lines VALUES ({','.join('?' * len(TCOLS))})",
                trows)
            con.execute("INSERT OR REPLACE INTO boxes_done VALUES (?,?)",
                        (gid, int(bool(prows))))
            n += len(prows)
            if i % 100 == 0:
                con.commit()
                r = i / max(time.time() - t0, 1e-9)
                print(f"  {i}/{len(todo)}  {r:.1f} games/s  "
                      f"{(len(todo) - i) / max(r, 1e-9) / 60:.1f} min left")
    con.commit()
    tot = con.execute("SELECT COUNT(*) FROM player_lines").fetchone()[0]
    print(f"boxes: {n} player rows written ({bad} games had no box), "
          f"{tot} rows in table")
    con.close()


# ------------------------------------------------------------- injuries

def stage_teams():
    """The club list, keyed by the abbreviation everything else here uses.
    A sportsbook names a side "Denver Nuggets"; this is the join."""
    con = connect()
    j = get(TEAMS)
    rows = []
    for t in (((j or {}).get("sports") or [{}])[0].get("leagues") or
              [{}])[0].get("teams") or []:
        x = t.get("team") or {}
        ab = _abb(x)
        if ab:
            rows.append((ab, _int(x.get("id")), x.get("displayName"),
                         x.get("location"), x.get("name")))
    con.executemany("INSERT OR REPLACE INTO teams VALUES (?,?,?,?,?)", rows)
    con.commit()
    print(f"teams: {len(rows)} clubs stored")
    con.close()


# ESPN files every NBA player under one of three broad positions, which is
# also the useful grain for defence-versus-position: five slots would cut
# each sample in half for no gain, since the listed difference between a
# shooting guard and a point guard is mostly nominal.
POS_GROUPS = {"G": "Guard", "F": "Forward", "C": "Centre"}


def scrape_pos(pid):
    j = get(ATH.format(pid))
    p = (j or {}).get("position") or {}
    ab = p.get("abbreviation")
    # A leaf position (PG, SF) rolls up to its parent letter.
    if ab and ab not in POS_GROUPS:
        ab = ab[-1] if ab[-1] in POS_GROUPS else None
    return pid, ab, p.get("name"), (j or {}).get("displayName")


def stage_positions(refresh=False):
    """One call per player. Positions do not change, so this is a one-off
    for everybody already in the box scores and a trickle after that."""
    con = connect()
    have = set() if refresh else {
        r[0] for r in con.execute("SELECT player_id FROM positions WHERE pos IS NOT NULL")}
    todo = [r[0] for r in con.execute(
        "SELECT DISTINCT player_id FROM player_lines") if r[0] not in have]
    print(f"positions: {len(todo)} players to look up ({len(have)} known)")
    rows, miss = [], 0
    with ThreadPoolExecutor(WORKERS) as ex:
        for pid, ab, nm, disp in ex.map(scrape_pos, todo):
            if ab is None:
                miss += 1
            rows.append((pid, disp, ab, nm))
    con.executemany("INSERT OR REPLACE INTO positions VALUES (?,?,?,?)", rows)
    con.commit()
    tally = dict(con.execute(
        "SELECT pos, COUNT(*) FROM positions GROUP BY pos"))
    print(f"positions: {len(rows)} written, {miss} without one;  " +
          "  ".join(f"{k or 'unknown'} {v}"
                    for k, v in sorted(tally.items(), key=lambda x: x[0] or "")))
    con.close()


def stage_injuries():
    """Who is out tonight. ESPN hangs the report off each upcoming event."""
    con = connect()
    gids = [r[0] for r in con.execute(
        "SELECT game_id FROM games WHERE date >= ? AND state='pre' "
        "ORDER BY date LIMIT 30", (today().isoformat(),))]
    if not gids:
        print("injuries: no upcoming games in the schedule")
        return
    rows, seen = [], set()
    now = datetime.now().isoformat(timespec="seconds")
    with ThreadPoolExecutor(WORKERS) as ex:
        for j in ex.map(lambda g: get(SUM, {"event": str(g)}), gids):
            for blk in (j or {}).get("injuries", []):
                team = (blk.get("team") or {}).get("abbreviation")
                for it in blk.get("injuries", []):
                    ath = it.get("athlete") or {}
                    pid = _int(ath.get("id"))
                    if pid is None or (team, pid) in seen:
                        continue
                    seen.add((team, pid))
                    det = it.get("details") or {}
                    rows.append((team, pid, ath.get("displayName"),
                                 it.get("status"),
                                 det.get("type") or it.get("longComment"), now))
    con.execute("DELETE FROM injuries")
    con.executemany("INSERT OR REPLACE INTO injuries VALUES (?,?,?,?,?,?)", rows)
    con.commit()
    print(f"injuries: {len(rows)} entries across {len(gids)} upcoming games")
    con.close()


def main():
    stage = sys.argv[1] if len(sys.argv) > 1 else "all"
    seasons = [int(a) for a in sys.argv[2:] if a.isdigit()] or SEASONS
    if stage in ("games", "all"):
        stage_games(seasons)
    if stage in ("boxes", "all"):
        stage_boxes(seasons)
    if stage in ("teams", "all"):
        stage_teams()
    if stage in ("positions", "all"):
        stage_positions()
    if stage in ("injuries", "all"):
        stage_injuries()
    if stage not in ("games", "boxes", "teams", "positions", "injuries",
                     "all"):
        sys.exit(f"unknown stage {stage!r}; use games, boxes, teams, "
                 "positions, injuries or all")


if __name__ == "__main__":
    main()
