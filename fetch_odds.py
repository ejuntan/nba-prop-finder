"""Real player-prop lines, from The Odds API.

The model has been honest but unmeasured: calibration says its
probabilities mean what they say, and nothing in this repository could
tell you whether they beat a book, because ESPN publishes game lines and
not player props. This closes that gap.

The key is read from the environment, or from a gitignored `.env` beside
this file, and is never printed or passed on a command line:

    export ODDS_API_KEY=...        # in ~/.zprofile
    echo 'ODDS_API_KEY=...' > .env && chmod 600 .env    # or here

`.env` is in .gitignore. It is a convenience, not a secret store: anything
that can read the file can read the key, so a key that has been exposed
somewhere else should be rotated rather than moved here.

**Credits.** The event-odds endpoint bills one credit per market per
region per event -- measured, not guessed -- so five markets costs five
credits a game. A full NBA night averages 7.5 games, and the season runs
166 of them, which is about 1,120 credits a month against a free tier of
500. The budget decides the sampling, not the other way round.

`--budget N` caps a run at N credits and chooses which games to spend
them on by which teams have been sampled least, so coverage spreads
across the league over weeks instead of piling onto whoever happens to
play early. Three games a night at five markets is 450 a month and fits.
`--markets`, `--all-markets` and `--max-events` are the manual overrides,
and every run prints what the account has left.

**Every fetch is kept, timestamped.** That is the point: the difference
between the first line a book hangs and the last one before tip is closing
line value, and CLV is the only measurement that settles whether this
model is worth betting. One snapshot could not tell you; a season of them
can.
"""
import os
import sqlite3
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

from features import DB, DIR
from fetch import today as et_today

ODDS_DB = DIR + "/data/odds.sqlite3"
ROOT = "https://api.the-odds-api.com/v4"
BASE = ROOT + "/sports/basketball_nba"
UA = "nba-props/1.0 (personal research)"
REGIONS = "us"

# Their market key -> the stat key this repository uses. Only markets the
# model actually prices are listed; anything else would cost credits for a
# number nothing downstream can use.
MARKETS = {
    "player_points": "pts",
    "player_rebounds": "reb",
    "player_assists": "ast",
    "player_threes": "fg3m",
    "player_points_rebounds_assists": "pra",
}
EXTRA = {
    "player_points_rebounds": "pr",
    "player_points_assists": "pa",
    "player_rebounds_assists": "ra",
    "player_blocks": "blk",
    "player_steals": "stl",
    "player_turnovers": "tov",
    "player_blocks_steals": "stocks",
    "player_field_goals": "fgm",
}
ALL_MARKETS = {**MARKETS, **EXTRA}

SCHEMA = """
CREATE TABLE IF NOT EXISTS prop_lines (
  fetched    TEXT,     -- UTC timestamp of the snapshot
  event_id   TEXT,     -- the book's event id
  game_id    INTEGER,  -- joined to games.game_id, null if unmatched
  commence   TEXT,
  book       TEXT,
  player     TEXT,     -- as the book spells it
  player_id  INTEGER,  -- joined to this database, null if unmatched
  stat       TEXT,
  line       REAL,
  over_odds  INTEGER,
  under_odds INTEGER,
  PRIMARY KEY (fetched, event_id, book, player, stat, line)
);
CREATE INDEX IF NOT EXISTS i_pl_game ON prop_lines(game_id, stat);
CREATE INDEX IF NOT EXISTS i_pl_when ON prop_lines(fetched);

CREATE TABLE IF NOT EXISTS unmatched (
  fetched TEXT, player TEXT, event_id TEXT, PRIMARY KEY (fetched, player)
);

CREATE TABLE IF NOT EXISTS quota (
  fetched TEXT PRIMARY KEY, used INTEGER, remaining INTEGER, cost INTEGER
);
"""


def _dotenv():
    """KEY=value lines from a gitignored .env, if one is there."""
    try:
        with open(DIR + "/.env") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln or ln.startswith("#") or "=" not in ln:
                    continue
                k, _, v = ln.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))
    except FileNotFoundError:
        pass


def key():
    _dotenv()
    k = os.environ.get("ODDS_API_KEY", "").strip()
    if not k:
        sys.exit(
            "ODDS_API_KEY is not set.\n"
            "  Either:\n"
            "    echo 'export ODDS_API_KEY=your-key' >> ~/.zprofile\n"
            "  or, beside this file:\n"
            "    echo 'ODDS_API_KEY=your-key' > .env && chmod 600 .env")
    return k


def connect():
    con = sqlite3.connect(ODDS_DB, timeout=60)
    con.executescript(SCHEMA)
    return con


_last_headers = {}


def _redact(msg):
    """The key travels as a query parameter, so it lands in the URL of
    every error requests raises. Never let it reach a log or a traceback."""
    k = os.environ.get("ODDS_API_KEY", "")
    return str(msg).replace(k, "<key>") if k else str(msg)


def get(path, params, tries=3, root=False):
    """GET, remembering the quota headers the API sends back."""
    p = dict(params)
    p["apiKey"] = key()
    url = (ROOT if root else BASE) + path
    for a in range(tries):
        try:
            r = requests.get(url, params=p, timeout=40,
                             headers={"User-Agent": UA})
            if r.status_code == 401:
                sys.exit("The Odds API rejected the key in ODDS_API_KEY.")
            if r.status_code == 422:
                return None          # no such market for this event
            if r.status_code == 429:
                time.sleep(2 * (a + 1))
                continue
            r.raise_for_status()
            for h in ("x-requests-used", "x-requests-remaining",
                      "x-requests-last"):
                if h in r.headers:
                    _last_headers[h] = r.headers[h]
            return r.json()
        except requests.HTTPError as e:
            raise SystemExit(f"The Odds API returned an error: "
                             f"{_redact(e)}") from None
        except Exception as e:
            if a == tries - 1:
                raise SystemExit(f"Could not reach The Odds API: "
                                 f"{_redact(e)}") from None
            time.sleep(1.5 * (a + 1))


# ------------------------------------------------------------- matching

def norm(name):
    """Fold a name to something two sources can agree on."""
    s = unicodedata.normalize("NFKD", str(name or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    for junk in (" jr.", " jr", " sr.", " sr", " iii", " ii", " iv"):
        if s.endswith(junk):
            s = s[: -len(junk)]
    return "".join(c for c in s if c.isalnum() or c == " ").strip()


def player_index(con_nba, teams):
    """Everyone who has played for these clubs lately, by folded name."""
    q = f"""SELECT DISTINCT p.player_id, p.name, p.team
            FROM player_lines p JOIN games g USING (game_id)
            WHERE g.date >= date('now', '-60 day')
              AND p.team IN ({','.join('?' * len(teams))})"""
    d = pd.read_sql(q, con_nba, params=list(teams))
    idx, short = {}, {}
    for r in d.itertuples(index=False):
        n = norm(r.name)
        idx.setdefault(n, r.player_id)
        parts = n.split()
        if len(parts) >= 2:
            short.setdefault(parts[0][0] + " " + parts[-1], r.player_id)
    return idx, short


def match_player(name, idx, short):
    n = norm(name)
    if n in idx:
        return idx[n]
    parts = n.split()
    if len(parts) >= 2:
        k = parts[0][0] + " " + parts[-1]
        if k in short:
            return short[k]
    return None


def team_map(con_nba):
    return {r[1]: r[0] for r in con_nba.execute("SELECT team, display FROM teams")}


# --------------------------------------------------------------- fetch

def events(day):
    """The book's events for this slate, matched to our game ids."""
    j = get("/events", {}) or []
    con_nba = sqlite3.connect(DB)
    tmap = team_map(con_nba)
    games = pd.read_sql(
        "SELECT game_id, date, home, away FROM games WHERE date = ?",
        con_nba, params=(day,))
    by_pair = {(r.home, r.away): r.game_id for r in games.itertuples(index=False)}
    out = []
    for e in j:
        home, away = tmap.get(e.get("home_team")), tmap.get(e.get("away_team"))
        gid = by_pair.get((home, away))
        if gid is None:
            continue
        out.append({"event_id": e["id"], "game_id": int(gid),
                    "commence": e.get("commence_time"),
                    "home": home, "away": away})
    con_nba.close()
    return out, games


def event_odds(ev, markets):
    j = get(f"/events/{ev['event_id']}/odds",
            {"regions": REGIONS, "markets": ",".join(markets),
             "oddsFormat": "american"})
    return ev, j


def parse(ev, j, stamp, idx, short):
    """One event's response -> rows, pairing each over with its under."""
    rows, misses = [], set()
    for bk in (j or {}).get("bookmakers", []):
        book = bk.get("key")
        for mk in bk.get("markets", []):
            stat = ALL_MARKETS.get(mk.get("key"))
            if not stat:
                continue
            # Over and under arrive as separate outcomes on the same point.
            pair = {}
            for o in mk.get("outcomes", []):
                who, side = o.get("description"), (o.get("name") or "").lower()
                pt = o.get("point")
                if who is None or pt is None:
                    continue
                slot = pair.setdefault((who, float(pt)), {})
                slot[side] = o.get("price")
            for (who, pt), sides in pair.items():
                pid = match_player(who, idx, short)
                if pid is None:
                    misses.add(who)
                rows.append((stamp, ev["event_id"], ev["game_id"],
                             ev["commence"], book, who, pid, stat, pt,
                             sides.get("over"), sides.get("under")))
    return rows, misses


def team_counts():
    """How many times each club has been sampled, for the rotation."""
    con = connect()
    rows = con.execute("""SELECT game_id, COUNT(DISTINCT fetched) n
                          FROM prop_lines GROUP BY game_id""").fetchall()
    con.close()
    seen = {}
    if not rows:
        return seen
    nba = sqlite3.connect(DB)
    gmap = dict(nba.execute(
        "SELECT game_id, home || ',' || away FROM games"))
    nba.close()
    for gid, n in rows:
        for t in (gmap.get(gid) or "").split(","):
            if t:
                seen[t] = seen.get(t, 0) + n
    return seen


def pick_within_budget(evs, markets, budget):
    """The games to spend on: least-sampled clubs first, so the league
    gets covered rather than whichever teams tip earliest."""
    per_event = max(len(markets), 1)
    # A budget that cannot pay for one game buys nothing. Rounding it up to
    # one game would spend money the caller said it did not have, and zero
    # is how the refresh button asks for a run with no market data at all.
    if int(budget) < per_event:
        print(f"odds: budget {budget} credits does not cover a game at "
              f"{per_event}; fetching no lines")
        return []
    k = int(budget) // per_event
    if k >= len(evs):
        return evs
    seen = team_counts()
    evs = sorted(evs, key=lambda e: (seen.get(e["home"], 0)
                                     + seen.get(e["away"], 0),
                                     e["commence"]))
    chosen = evs[:k]
    print(f"odds: budget {budget} credits allows {k} of {len(evs)} games; "
          "picking the least-sampled clubs -- "
          + ", ".join(f"{e['away']}@{e['home']}" for e in chosen))
    return chosen


def run(day=None, markets=None, max_events=None, workers=4,
        only_missing=False, budget=None):
    con_nba = sqlite3.connect(DB)
    day = day or next_day(con_nba)
    evs, games = events(day)
    if not evs:
        print(f"odds: the book lists no matching events for {day}")
        return
    if only_missing:
        have = fetched_events()
        before = len(evs)
        evs = [e for e in evs if e["event_id"] not in have]
        print(f"odds: {before - len(evs)} of {before} events already quoted "
              "in the last 12 hours, skipping those")
        if not evs:
            print("odds: nothing left to fetch")
            return
    markets = markets or list(MARKETS)
    # None means no cap; 0 means spend nothing, which is not the same thing.
    if budget is not None:
        evs = pick_within_budget(evs, markets, budget)
        if not evs:
            return
    if max_events:
        evs = evs[: int(max_events)]
    teams = sorted(set(games.home) | set(games.away))
    idx, short = player_index(con_nba, teams)
    con_nba.close()

    cost = len(evs) * len(markets)
    print(f"odds: {len(evs)} events x {len(markets)} markets "
          f"= {cost} credits")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    con = connect()
    rows, misses = [], set()
    with ThreadPoolExecutor(workers) as ex:
        for ev, j in ex.map(lambda e: event_odds(e, markets), evs):
            r, m = parse(ev, j, stamp, idx, short)
            rows += r
            misses |= m
    con.executemany(
        "INSERT OR REPLACE INTO prop_lines VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        rows)
    con.executemany("INSERT OR REPLACE INTO unmatched VALUES (?,?,?)",
                    [(stamp, m, "") for m in misses])
    used = int(_last_headers.get("x-requests-used", 0) or 0)
    left = int(_last_headers.get("x-requests-remaining", 0) or 0)
    con.execute("INSERT OR REPLACE INTO quota VALUES (?,?,?,?)",
                (stamp, used, left, cost))
    con.commit()
    books = len({r[4] for r in rows})
    priced = len({(r[6], r[7]) for r in rows if r[6]})
    print(f"odds: {len(rows)} quotes from {books} books, "
          f"{priced} player-markets matched to this database")
    if misses:
        print(f"      {len(misses)} names not matched: "
              + ", ".join(sorted(misses)[:6])
              + (" ..." if len(misses) > 6 else ""))
    print(f"      credits used {used}, remaining {left}")
    con.close()


def next_day(con):
    r = con.execute(
        "SELECT MIN(date) FROM games WHERE date >= ? AND season_type IN (2,3,5)",
        (et_today().isoformat(),)).fetchone()
    return r[0] or et_today().isoformat()


def latest(day=None):
    """The freshest quotes, taken per event rather than per run.

    One global timestamp would be wrong as soon as a run covers only some
    of the slate -- fetching the late games later is the cheap way to work,
    and a board built on MAX(fetched) would then show only those. So each
    event contributes its own most recent snapshot."""
    con = connect()
    d = pd.read_sql("""
        SELECT p.* FROM prop_lines p
        JOIN (SELECT event_id, MAX(fetched) AS f
              FROM prop_lines GROUP BY event_id) m
          ON m.event_id = p.event_id AND m.f = p.fetched
    """, con)
    con.close()
    return d


def fetched_events(within_hours=12):
    """Events already quoted recently, so a run can skip them."""
    con = connect()
    cutoff = (datetime.now(timezone.utc)
              - timedelta(hours=within_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = {r[0] for r in con.execute(
        "SELECT DISTINCT event_id FROM prop_lines WHERE fetched >= ?",
        (cutoff,))}
    con.close()
    return rows


def quota():
    """What the account has left. The sports list is not billed, so this
    costs nothing and is worth doing before a run that does."""
    get("/sports", {}, root=True)
    used = _last_headers.get("x-requests-used", "?")
    left = _last_headers.get("x-requests-remaining", "?")
    print(f"quota: {used} used, {left} remaining")
    return left


def main():
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    if a and a[0] == "quota":
        return quota()
    flags = {x.split("=")[0]: x.split("=")[-1]
             for x in sys.argv[1:] if x.startswith("--")}
    if "--all-markets" in flags:
        mk = list(ALL_MARKETS)
    elif "--markets" in flags:
        mk = [m for m in flags["--markets"].split(",") if m in ALL_MARKETS]
    else:
        mk = list(MARKETS)
    run(day=a[0] if a else None, markets=mk,
        max_events=flags.get("--max-events"),
        only_missing="--only-missing" in flags,
        budget=int(flags["--budget"]) if "--budget" in flags else None)


if __name__ == "__main__":
    main()
