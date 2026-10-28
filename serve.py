"""Serve the board on localhost, so its Refresh button has something to call.

`tool/index.html` is a static file with the night's numbers baked into it,
which is what makes it portable -- and also what makes a Refresh button
impossible on its own. Refreshing means running the chain in `daily.sh`
(fetch -> features -> backtest -> project -> export -> build), which takes
a couple of minutes. This is the smallest server that lets the page start
that chain and watch it run:

    ./.venv/bin/python serve.py        # then http://127.0.0.1:8765

    GET  /                 the page, with this process's token injected
    GET  /api/status       slate, build time, credits left, last run
    POST /api/refresh      start a run, unless one is already going
    GET  /api/log?from=N   that run's output from byte N, for the stream

Three things are deliberate, because a refresh can spend money:

  * it binds to 127.0.0.1, so nothing off this machine can reach it;
  * the POST needs a token minted at startup and injected into the page as
    it is served, so a page in another tab cannot quietly burn the odds
    budget -- and the token rides in a custom header, which forces a CORS
    preflight that this server refuses, so a cross-site form post cannot
    reach the endpoint at all;
  * it checks the Host header, because a DNS-rebinding page that resolves
    to 127.0.0.1 would otherwise be able to read the token out of the page.

The default refresh spends nothing: ODDS_BUDGET=0 reprices tonight against
the lines already stored. Buying new lines is a separate, labelled click.
"""
import json
import os
import secrets
import sqlite3
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DIR = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(DIR, "tool", "index.html")
DATA = os.path.join(DIR, "out", "tool_data.json")
ODDS = os.path.join(DIR, "data", "odds.sqlite3")
MARK = "/*TOKEN*/null/*TOKEN*/"

HOST = "127.0.0.1"
PORT = int(os.environ.get("PORT", "8765"))
TOKEN = secrets.token_urlsafe(18)
# What "+ lines" costs when the page asks for it. Five markets a game, so
# 15 credits is three games -- the most a free tier can afford nightly.
BUDGET = int(os.environ.get("ODDS_BUDGET", "15"))
OK_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
TAIL = 60000          # bytes of a run's output kept for the page


class Run:
    """The one refresh that may be in flight, with its output so far."""

    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.out = bytearray()
        self.started = None
        self.finished = None
        self.code = None
        self.budget = 0

    @property
    def running(self):
        p = self.proc
        return p is not None and p.poll() is None

    def start(self, budget):
        with self.lock:
            if self.running:
                return False
            # PYTHONUNBUFFERED, or every stage's output would arrive in
            # 4 KB bursts and the progress stream would lie about where
            # the run had got to.
            env = dict(os.environ, ODDS_BUDGET=str(int(budget)),
                       PYTHONUNBUFFERED="1")
            self.out = bytearray()
            self.code = None
            self.finished = None
            self.started = time.time()
            self.budget = int(budget)
            self.proc = subprocess.Popen(
                [os.path.join(DIR, "daily.sh")], cwd=DIR, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            threading.Thread(target=self._drain, args=(self.proc,),
                             daemon=True).start()
            return True

    def _drain(self, p):
        for line in iter(p.stdout.readline, b""):
            with self.lock:
                self.out += line
                if len(self.out) > TAIL * 2:
                    del self.out[:-TAIL]
        p.stdout.close()
        code = p.wait()
        with self.lock:
            self.code, self.finished = code, time.time()

    def log(self, frm):
        with self.lock:
            n = len(self.out)
            frm = max(0, min(int(frm), n))
            return {"at": n, "text": bytes(self.out[frm:]).decode(
                        "utf-8", "replace"),
                    "running": self.running, "code": self.code,
                    "budget": self.budget,
                    "elapsed": round((self.finished or time.time())
                                     - self.started, 1) if self.started else 0}


RUN = Run()


def meta():
    """What the page currently on disk was built from."""
    try:
        with open(DATA) as fh:
            m = json.load(fh)["meta"]
    except Exception:
        return {}
    return {k: m.get(k) for k in ("slate", "built", "games", "players")}


def credits():
    """The remaining-credit count the last fetch was told, and when.

    It is a reading, not a live number: it ages, and it is wrong by
    whatever anything else spent since. Shown with its timestamp for
    exactly that reason."""
    try:
        con = sqlite3.connect(f"file:{ODDS}?mode=ro", uri=True)
        r = con.execute("SELECT fetched, remaining FROM quota "
                        "ORDER BY fetched DESC LIMIT 1").fetchone()
        con.close()
    except Exception:
        return None
    return {"at": r[0], "remaining": r[1]} if r else None


class H(BaseHTTPRequestHandler):
    server_version = "nba-props"
    protocol_version = "HTTP/1.1"

    def _host_ok(self):
        return (self.headers.get("Host") or "").split(":")[0] in OK_HOSTS

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # The page carries its data inline, so a cached copy is a stale
        # board -- which is the one thing a Refresh button must not leave
        # behind.
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj), "application/json")

    def do_GET(self):
        if not self._host_ok():
            return self._json(403, {"error": "unexpected Host header"})
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            try:
                with open(PAGE) as fh:
                    page = fh.read()
            except FileNotFoundError:
                return self._send(404, "no tool/index.html yet -- run "
                                       "./daily.sh once", "text/plain")
            return self._send(200, page.replace(MARK, json.dumps(TOKEN)),
                              "text/html; charset=utf-8")
        if u.path == "/api/status":
            return self._json(200, {"meta": meta(), "credits": credits(),
                                    "budget": BUDGET,
                                    "run": RUN.log(10 ** 12)})
        if u.path == "/api/log":
            return self._json(200, RUN.log((q.get("from") or ["0"])[0]))
        return self._json(404, {"error": "no such path"})

    def do_POST(self):
        if not self._host_ok():
            return self._json(403, {"error": "unexpected Host header"})
        if urlparse(self.path).path != "/api/refresh":
            return self._json(404, {"error": "no such path"})
        if not secrets.compare_digest(
                self.headers.get("X-Refresh-Token") or "", TOKEN):
            return self._json(403, {"error": "bad or missing token -- open "
                                             "the page from this server"})
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            body = {}
        budget = BUDGET if body.get("lines") else 0
        if not RUN.start(budget):
            return self._json(409, {"error": "a refresh is already running"})
        print(f"refresh: started, odds budget {budget} credits", flush=True)
        return self._json(202, {"started": True, "budget": budget})

    def do_OPTIONS(self):
        # No CORS headers, on purpose: the preflight for the token header
        # fails, so no other origin can reach the POST.
        self._json(405, {"error": "same-origin only"})

    def log_message(self, fmt, *a):
        pass


def main():
    for port in range(PORT, PORT + 8):
        try:
            srv = ThreadingHTTPServer((HOST, port), H)
            break
        except OSError as e:
            if e.errno not in (48, 98):
                raise
            print(f"port {port} is busy, trying {port + 1}")
    else:
        raise SystemExit("no free port in range")
    m = meta()
    print(f"board:  http://{HOST}:{port}/")
    print(f"slate:  {m.get('slate', '?')}  built {m.get('built', '?')}")
    c = credits()
    if c:
        print(f"odds:   {c['remaining']} credits left as of {c['at']}; "
              f"'+ lines' spends {BUDGET}")
    print("Refresh runs ./daily.sh. Ctrl-C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
