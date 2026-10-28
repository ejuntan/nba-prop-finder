"""Invariants worth breaking the build over.

The important one is the leak test. Everything this model claims rests on
features that only ever saw earlier games, and that property is easy to
lose by accident in a single-pass builder. So the test rebuilds the whole
feature set from data truncated at a date and checks that every row before
that date came out byte-for-byte identical. If a future game were leaking
in anywhere, those numbers would move.
"""
import sqlite3
import sys

import numpy as np
import pandas as pd

import features as F
import model as M
import project as P

CUT = "2026-01-15"
fails = []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        fails.append(name)


def test_no_leak(con):
    d, t = F.load(con)
    full, _ = F.build(d, t)
    dt = d[d.date < CUT]
    tt = t[t.date < CUT]
    trunc, _ = F.build(dt, tt)
    a = full[full.date < CUT].reset_index(drop=True)
    b = trunc.reset_index(drop=True)
    check("truncation leaves the same number of rows", len(a) == len(b),
          f"{len(a)} vs {len(b)}")
    if len(a) != len(b):
        return
    cols = [c for c in a.columns
            if c.startswith(("rate_", "proj_", "opp_", "home_"))
            or c in ("proj_min", "pace_mult", "play_rate", "start_rate",
                     "games_seen", "min_seen")]
    worst, where = 0.0, ""
    for c in cols:
        x, y = a[c].to_numpy(float), b[c].to_numpy(float)
        g = float(np.nanmax(np.abs(x - y))) if len(x) else 0.0
        if g > worst:
            worst, where = g, c
    check("features before the cut are unchanged by later games",
          worst < 1e-9, f"largest difference {worst:.2e} ({where})")


def test_first_game(full):
    """A player's first game must be priced off the league, not himself."""
    # A stable sort, or two rows sharing a date come back in any order.
    first = (full.sort_values(["date", "game_id"], kind="stable")
             .groupby("player_id", sort=False).head(1))
    check("a player's first row has no history of his own",
          bool((first.games_seen == 0).all()),
          f"max games_seen {first.games_seen.max():.3f}")
    check("and its minutes projection is empty, not borrowed",
          bool((first.proj_min == 0).all()))


def test_combos(con):
    d, _ = F.load(con)
    bad = 0
    for name, parts in F.COMBOS.items():
        bad += int((d[name] != sum(d[p] for p in parts)).sum())
    check("combo markets equal the sum of their parts", bad == 0, f"{bad} bad")


def test_probabilities():
    cal = M.load()
    worst = 0.0
    for s, c in cal.items():
        for mu in (0.4, 2.0, 7.5, 18.0, 34.0):
            for line in (0.5, 1.5, 4, 9.5, 20, 33.5):
                po, pu, push = M.p_over(mu, c, line)
                worst = max(worst, abs(po + pu + push - 1.0))
                if min(po, pu, push) < -1e-12:
                    worst = 9
    check("over + under + push = 1 everywhere", worst < 1e-9,
          f"largest error {worst:.2e}")
    for oo, uo in ((-110, -110), (-115, -105), (150, -180), (-400, 320)):
        a, b = M.devig(oo, uo)
        check(f"devig {oo}/{uo} sums to 1", abs(a + b - 1) < 1e-9)
        check(f"devig {oo}/{uo} keeps the favourite the favourite",
              (a > b) == (M.american_to_prob(oo) > M.american_to_prob(uo)))


def test_calibration_file():
    cal = M.load()
    missing = [s for s in F.STATS if s not in cal]
    check("every market has fitted constants", not missing, str(missing))
    bad = [s for s, c in cal.items()
           if not (0.5 <= c["b"] <= 1.6) or (c["c"] and not 0.2 <= c["p"] <= 2.2)]
    check("constants are inside their bounds", not bad, str(bad))
    nm = [s for s, c in cal.items()
          if any(np.diff(c["shape"]) < -1e-12)]
    check("the probability correction is monotone", not nm, str(nm))


def test_defense():
    try:
        d = pd.read_pickle(F.DIR + "/data/defense.pkl")
    except FileNotFoundError:
        check("defence table exists", False, "run defense.py")
        return
    for win in ("last10", "last30", "season"):
        t = d.get(win)
        if t is None or t.empty:
            continue
        check(f"{win}: every team is rated once", len(t) == 30, f"{len(t)}")
        check(f"{win}: ranks are a permutation of 1..{len(t)}",
              sorted(t.drtg_rank) == list(range(1, len(t) + 1)))
        check(f"{win}: defensive ratings are in a sane range",
              bool(t.drtg.between(90, 140).all()),
              f"{t.drtg.min():.1f}..{t.drtg.max():.1f}")
        check(f"{win}: pace is in a sane range",
              bool(t.pace.between(85, 115).all()),
              f"{t.pace.min():.1f}..{t.pace.max():.1f}")
        # Points allowed per 100 possessions IS the defensive rating.
        check(f"{win}: points allowed matches the defensive rating",
              bool((t.pts - t.drtg).abs().max() < 1e-9))
        check(f"{win}: the league averages to 1.0 versus itself",
              abs(t.pts_vs_lg.mean() - 1) < 1e-9)


def test_positional_defense():
    d = pd.read_pickle(F.DIR + "/data/defense.pkl")
    import defense as DEF
    for win in ("last10", "last30", "season"):
        tab, ptab = d.get(win), d.get("pos_" + win)
        if tab is None or ptab is None or ptab.empty:
            continue
        check(f"{win}: every team rated at each of {len(DEF.POS_ALL)} buckets",
              len(ptab) == len(tab) * len(DEF.POS_ALL), f"{len(ptab)}")
        # The three positions share one denominator, so they must add back.
        worst, where = 0.0, ""
        for s in ("pts", "reb", "ast", "fg3m"):
            tot = ptab.groupby(level="team")[s].sum()
            gap = (tot - tab[s]).abs().max()
            if gap > worst:
                worst, where = gap, s
        check(f"{win}: positions add back to the team total", worst < 1e-9,
              f"largest gap {worst:.2e} ({where})")
        for pos in DEF.POS:
            r = sorted(ptab.xs(pos, level="pos")["pts_rank"])
            check(f"{win}/{pos}: ranks are a permutation of 1..{len(tab)}",
                  r == list(range(1, len(tab) + 1)))


def test_odds_parsing():
    """The Odds API costs credits per call, so the transform is proved
    against a fixture shaped like its documented response rather than by
    spending quota to find out."""
    import fetch_odds as FO
    sl = pd.read_pickle(F.DIR + "/data/slate.pkl")
    live = sl[sl.out == 0].nlargest(2, "proj_min")
    a, b = live.iloc[0], live.iloc[1]
    ev = {"event_id": "abc123", "game_id": int(a.game_id),
          "commence": "2026-10-27T00:00:00Z"}
    fixture = {"bookmakers": [{"key": "draftkings", "markets": [
        {"key": "player_points", "outcomes": [
            {"name": "Over", "description": a["name"], "point": 27.5,
             "price": -115},
            {"name": "Under", "description": a["name"], "point": 27.5,
             "price": -105},
            # An accented, suffixed spelling the book may use instead.
            {"name": "Over", "description": b["name"].upper(), "point": 19.5,
             "price": 100},
            {"name": "Under", "description": b["name"].upper(), "point": 19.5,
             "price": -122}]},
        {"key": "player_rebounds", "outcomes": [
            {"name": "Over", "description": a["name"], "point": 11.5,
             "price": -130},
            {"name": "Under", "description": a["name"], "point": 11.5,
             "price": 108}]},
        {"key": "player_double_double", "outcomes": [
            {"name": "Yes", "description": a["name"], "point": None,
             "price": -200}]}]}]}
    con = sqlite3.connect(F.DB)
    idx, short = FO.player_index(con, sorted(set(sl.team)))
    con.close()
    rows, misses = FO.parse(ev, fixture, "2026-10-27T00:00:00Z", idx, short)
    check("every over is paired with its under", len(rows) == 3,
          f"{len(rows)} rows")
    check("a market the model does not price is ignored",
          all(r[7] in ("pts", "reb") for r in rows))
    check("both sides of a quote survive",
          all(r[9] is not None and r[10] is not None for r in rows))
    check("the book's spelling is matched to a player id",
          not misses and all(r[6] is not None for r in rows), str(misses))
    check("the line and prices come through intact",
          sorted((r[7], r[8], r[9], r[10]) for r in rows) ==
          sorted([("pts", 27.5, -115, -105), ("pts", 19.5, 100, -122),
                  ("reb", 11.5, -130, 108)]))
    # Name folding, the part most likely to drift.
    check("accents and suffixes fold away",
          FO.norm("Nikola Jokić") == "nikola jokic" and
          FO.norm("Jaren Jackson Jr.") == "jaren jackson" and
          FO.norm("Shai Gilgeous-Alexander") == "shai gilgeousalexander")


def test_edges_join():
    """The line-to-projection join, on a snapshot written to a scratch
    database so the real one is never touched."""
    import fetch_odds as FO
    import edges as E
    import tempfile, os
    sl = pd.read_pickle(F.DIR + "/data/slate.pkl")
    live = sl[(sl.out == 0) & (sl.proj_pts > 12)].nlargest(1, "proj_min")
    if live.empty:
        check("a priceable player exists to test the join with", False)
        return
    p = live.iloc[0]
    line = float(np.floor(p.proj_pts)) + 0.5
    # Three books on the same number: two fair, one a point short on the
    # over. The model should find the third one.
    quotes = [("draftkings", -110, -110), ("fanduel", -112, -108),
              ("soft", 120, -145)]
    stamp = "2026-10-27T00:00:00Z"
    old = FO.ODDS_DB
    tmp = tempfile.mkdtemp()
    FO.ODDS_DB = os.path.join(tmp, "odds.sqlite3")
    try:
        con = FO.connect()
        con.executemany(
            "INSERT OR REPLACE INTO prop_lines VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(stamp, "ev1", int(p.game_id), stamp, bk, p["name"],
              int(p.player_id), "pts", line, oo, uo)
             for bk, oo, uo in quotes]
            # The same player, quoted for a game that is not on tonight's
            # slate: last night's number, which must not be carried over.
            + [(stamp, "ev-old", 1, stamp, "draftkings", p["name"],
                int(p.player_id), "pts", line - 3, -110, -110)])
        con.commit()
        con.close()
        d = E.build()
        check("the join finds the quotes", d is not None and len(d) == 6,
              f"{0 if d is None else len(d)} rows")
        if d is None:
            return
        check("every book is represented", d.book.nunique() == 3)
        check("a quote for another game is not tonight's market",
              float(line - 3) not in set(d.line))
        # A -110/-110 market devigs to exactly 50/50.
        dk = d[(d.book == "draftkings") & (d.side == "over")].iloc[0]
        check("a balanced market devigs to even money",
              abs(dk.novig_book - 0.5) < 1e-9, f"{dk.novig_book:.6f}")
        check("the consensus uses every book quoting the number",
              bool((d.n_books == 3).all()) and d.novig_cons.notna().all())
        # The soft book's over is the longest price, so it must win on EV
        # whenever the model likes the over at all.
        best = E.best_per_prop(d).iloc[0]
        over_model = d[d.side == "over"].model.iloc[0]
        want = "soft" if over_model > 0.5 else None
        if want:
            check("line shopping picks the longest price on the right side",
                  best.book == want and best.side == "over",
                  f"picked {best.book}/{best.side}")
        check("edge against the consensus differs from edge against the book",
              not np.isclose(d.edge_cons, d.edge_book).all())
        t = E.for_tool(d)
        check("the tool export carries one entry per market",
              int(p.player_id) in t and "pts" in t[int(p.player_id)])
        e = t[int(p.player_id)]["pts"]
        check("and it shops the best price on each side",
              e["over_book"] == "soft" and e["over"] == 120,
              f"{e['over_book']} {e['over']}")
    finally:
        FO.ODDS_DB = old


def test_budget():
    """A credit budget is a cap, and zero means zero. Rounding it up to
    one game, as an earlier version did, spent money nobody had agreed
    to spend."""
    import fetch_odds as FO
    evs = [{"event_id": str(i), "home": "AAA", "away": "BBB",
            "commence": f"2026-10-27T2{i}:00:00Z"} for i in range(4)]
    mk = list(FO.MARKETS)
    one = len(mk)                      # credits for a single game
    check("a budget of nothing buys nothing",
          FO.pick_within_budget(evs, mk, 0) == [])
    check("a budget under one game's price buys nothing",
          FO.pick_within_budget(evs, mk, one - 1) == [])
    check("a budget of one game buys one",
          len(FO.pick_within_budget(evs, mk, one)) == 1)
    check("a budget past the slate buys the slate",
          len(FO.pick_within_budget(evs, mk, one * 99)) == len(evs))


def test_bad_day():
    """A date ESPN will not serve must cost that date, not the run.

    ESPN answers the odd calendar day with an empty 200 for hours at a
    time while every other day in the same window is fine. Unattended,
    that used to end the whole chain at its first stage."""
    import tempfile
    from datetime import date as _date

    import fetch as FE
    old_db, old_get, old_today = FE.DB, FE.get, FE.today
    tmp = tempfile.mkdtemp()
    FE.DB = tmp + "/nba.sqlite3"
    bad = _date(2026, 10, 26)
    seen = []

    def fake_get(url, params=None, tries=5):
        day = params["dates"]
        seen.append(day)
        if day == bad.strftime("%Y%m%d"):
            raise RuntimeError("empty body")
        return {"events": []}

    try:
        FE.get = fake_get
        FE.today = lambda: _date(2026, 10, 28)
        FE.stage_games([2027])          # must not raise
        con = FE.connect()
        done = {r[0] for r in con.execute("SELECT day FROM days_done")}
        con.close()
        check("a day that cannot be read does not end the run", True)
        check("and it is not marked done, so the next run retries it",
              bad.isoformat() not in done)
        check("the days around it are stored",
              _date(2026, 10, 25).isoformat() in done
              and _date(2026, 10, 27).isoformat() in done)

        # Everything failing is an outage, and must not pass quietly.
        FE.DB = tmp + "/outage.sqlite3"

        def all_fail(url, params=None, tries=5):
            raise RuntimeError("empty body")

        FE.get = all_fail
        try:
            FE.stage_games([2027])
            check("a total outage is not reported as a good run", False)
        except RuntimeError:
            check("a total outage is not reported as a good run", True)
    finally:
        FE.DB, FE.get, FE.today = old_db, old_get, old_today


def test_serve():
    """The refresh endpoint's refusals.

    The POST that actually starts a run is deliberately not exercised --
    it would spend credits and take two minutes. What is tested is that
    nobody without the page's token can reach it."""
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    import serve as S
    srv = ThreadingHTTPServer(("127.0.0.1", 0), S.H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    def req(path, method="GET", headers=None):
        r = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                   method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(r, timeout=5) as f:
                return f.status, f.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    try:
        code, body = req("/api/status")
        check("the status endpoint answers", code == 200 and b"budget" in body)
        check("a refresh without the token is refused",
              req("/api/refresh", "POST")[0] == 403)
        check("and with the wrong one",
              req("/api/refresh", "POST",
                  {"X-Refresh-Token": "not-the-token"})[0] == 403)
        check("a request claiming another Host is refused",
              req("/api/status", headers={"Host": "rebound.example"})[0] == 403)
        check("a cross-origin preflight gets no permission",
              req("/api/refresh", "OPTIONS")[0] == 405)
        page = req("/")[1].decode()
        check("the served page carries a token the file on disk does not",
              S.MARK not in page and f'const TOKEN = "{S.TOKEN}"' in page)
    finally:
        srv.shutdown()


def test_grading():
    """The grader, on a completed game with a known box score. Real lines
    cannot be graded until tonight's games finish, so the arithmetic is
    proved against a quote invented for a game that already happened."""
    import fetch_odds as FO, grade as G, project as P
    import tempfile, os, sqlite3 as sq
    con = sqlite3.connect(F.DB)
    # A finished game, and a player who actually scored in it.
    row = con.execute("""SELECT p.game_id, p.player_id, p.pts, p.team, g.date
                         FROM player_lines p JOIN games g USING (game_id)
                         WHERE g.state='post' AND p.dnp=0 AND p.pts BETWEEN 18 AND 30
                         ORDER BY g.date DESC LIMIT 1""").fetchone()
    con.close()
    gid, pid, pts, team, day = row
    tmp = tempfile.mkdtemp()
    old_odds, old_log = FO.ODDS_DB, P.LOG
    FO.ODDS_DB = os.path.join(tmp, "odds.sqlite3")
    P.LOG = G.SLATE_LOG = os.path.join(tmp, "slate_log.sqlite3")
    try:
        # A line two points under what he scored: the over won.
        line = float(pts) - 2.5
        c = FO.connect()
        c.executemany(
            "INSERT OR REPLACE INTO prop_lines VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [("2026-01-01T12:00:00Z", "e1", gid, day, "draftkings", "X",
              pid, "pts", line, -110, -110),
             ("2026-01-01T23:00:00Z", "e1", gid, day, "draftkings", "X",
              pid, "pts", line, -110, -110)])
        c.commit(); c.close()
        # A projection comfortably above the line, so the model takes the over.
        lc = sq.connect(P.LOG)
        cols = ["date", "game_id", "player_id", "proj_pts"]
        lc.execute("CREATE TABLE slate_log (date, game_id, player_id, "
                   "proj_pts, logged, PRIMARY KEY (date, game_id, player_id))")
        lc.execute("INSERT INTO slate_log VALUES (?,?,?,?,?)",
                   (day, gid, pid, float(pts) + 1.0, "x"))
        lc.commit(); lc.close()
        d = G.build()
        check("the grader finds the completed prop", d is not None and len(d) == 1,
              f"{0 if d is None else len(d)} rows")
        if d is None:
            return
        r = d.iloc[0]
        check("it reads the actual box score", r.actual == pts,
              f"{r.actual} vs {pts}")
        check("it settles over/under correctly", r.result == "over")
        check("the model takes the side its projection implies",
              r.side == "over", r.side)
        check("a winning bet at -110 pays 0.909",
              abs(r.pnl - 100 / 110) < 1e-9, f"{r.pnl:.4f}")
        check("and it is recorded as won", bool(r.won))
        # A line above what he scored, with a projection above it too: the
        # model takes the over and loses.
        c = FO.connect()
        c.execute("UPDATE prop_lines SET line = ?", (float(pts) + 2.5,))
        c.commit(); c.close()
        lc = sq.connect(P.LOG)
        lc.execute("UPDATE slate_log SET proj_pts = ?", (float(pts) + 6.0,))
        lc.commit(); lc.close()
        d2 = G.build()
        r2 = d2.iloc[0]
        check("a line above the score settles under", r2.result == "under")
        # `is False` would compare identity against a numpy bool and fail.
        check("the model takes the over there, and the bet loses",
              r2.side == "over" and pd.notna(r2.won) and not r2.won
              and r2.pnl == -1.0, f"{r2.side} won={r2.won} pnl={r2.pnl}")
        # A line below the score, projection below it: under, and loses.
        c = FO.connect()
        c.execute("UPDATE prop_lines SET line = ?", (float(pts) - 2.5,))
        c.commit(); c.close()
        lc = sq.connect(P.LOG)
        lc.execute("UPDATE slate_log SET proj_pts = ?", (float(pts) - 6.0,))
        lc.commit(); lc.close()
        r2b = G.build().iloc[0]
        check("and the mirror case: under taken, over landed, bet lost",
              r2b.side == "under" and r2b.result == "over"
              and pd.notna(r2b.won) and not r2b.won,
              f"{r2b.side}/{r2b.result} won={r2b.won}")
        lc = sq.connect(P.LOG)
        lc.execute("UPDATE slate_log SET proj_pts = ?", (float(pts) + 1.0,))
        lc.commit(); lc.close()
        # And a push returns the stake.
        c = FO.connect()
        c.execute("UPDATE prop_lines SET line = ?", (float(pts),))
        c.commit(); c.close()
        r3 = G.build().iloc[0]
        check("an exact hit is a push and returns the stake",
              r3.result == "push" and r3.pnl == 0.0 and pd.isna(r3.won),
              f"{r3.result} pnl={r3.pnl} won={r3.won}")
    finally:
        FO.ODDS_DB, P.LOG = old_odds, old_log
        G.SLATE_LOG = old_log


def test_slate():
    try:
        sl = pd.read_pickle(F.DIR + "/data/slate.pkl")
    except FileNotFoundError:
        check("slate exists", False, "run project.py")
        return
    check("no player priced twice in one game",
          not sl.duplicated(["game_id", "player_id"]).any())
    out = sl[sl.out == 1]
    check("ruled-out players are kept, not deleted",
          bool((sl.out == 1).any()) or bool(sl.play_prob.min() > 0),
          f"{len(out)} on the board unpriced")
    check("and every one of them has zero availability",
          bool((out.play_prob == 0).all()) if len(out) else True)
    check("everyone priced has a real chance of playing",
          bool((sl.loc[sl.out == 0, "play_prob"] > 0).all()))
    check("nobody ruled out is worth less than the minutes floor",
          bool((out.base_min >= P.MIN_PROJ_MIN).all()) if len(out) else True)
    check("a thin record is flagged, not excluded",
          bool((sl.loc[sl.out == 0, "games_seen"] >= M.MIN_GAMES_SEEN).all())
          and bool((sl.loc[sl.games_seen <= M.THIN_GAMES, "thin"] == 1).all()),
          f"{int(sl.thin.sum())} of {len(sl)} flagged thin, "
          f"fewest games {sl.games_seen.min():.1f}")
    check("every player carries a position",
          bool(sl.pos.isin(["G", "F", "C", "?"]).all()),
          f"{(sl.pos == '?').sum()} unknown")
    check("minutes are inside a possible range",
          bool((sl.proj_min.between(0, 48)).all()),
          f"max {sl.proj_min.max():.1f}")
    # The budget counts a ruled-out player as zero expected minutes, so
    # the rest of his team must already have absorbed them.
    exp = sl.assign(e=sl.proj_min * sl.play_prob)
    check("a ruled-out player contributes no expected minutes",
          bool((exp.loc[exp.out == 1, "e"] == 0).all()))
    # The board shows only priceable players, so it sums to less than 240;
    # the budget itself is solved over every candidate and recorded.
    tot = sl.groupby(["game_id", "team"]).team_exp_min.first()
    check("each team's minute budget solves to 240",
          bool(((tot - 240).abs() < 1.0).all()),
          f"range {tot.min():.1f}..{tot.max():.1f}")
    shown = sl.assign(e=sl.proj_min * sl.play_prob) \
              .groupby(["game_id", "team"]).e.sum()
    check("and the board is a subset of it, never more",
          bool((shown <= tot + 1e-6).all()),
          f"board holds {shown.min():.0f}..{shown.max():.0f} of 240")
    for s in ("pts", "reb", "ast", "pra"):
        check(f"{s} projections are finite and positive",
              bool(np.isfinite(sl[f"proj_{s}"]).all() and
                   (sl[f"proj_{s}"] > 0).all()))


def main():
    con = sqlite3.connect(F.DB)
    print("leak")
    test_no_leak(con)
    full = pd.read_pickle(F.FEAT if hasattr(F, "FEAT") else F.OUT)
    print("features")
    test_first_game(full)
    test_combos(con)
    print("pricing")
    test_probabilities()
    test_calibration_file()
    print("odds")
    test_odds_parsing()
    test_edges_join()
    test_budget()
    test_grading()
    print("fetching")
    test_bad_day()
    print("server")
    test_serve()
    print("defence")
    test_defense()
    test_positional_defense()
    print("slate")
    test_slate()
    print(f"\n{'FAILED: ' + ', '.join(fails) if fails else 'all tests passed'}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
