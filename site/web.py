"""The public board: one page, read-only, no key and no pipeline.

What is deployed here is a copy, not the tool. The model, the ESPN crawl
and the odds fetch all run on the machine that has the data and the API
key; this service holds the two files they produce and splices them into
a page. So:

  * nothing here can spend a credit, because nothing here holds a key;
  * nothing here can be made to run a job, because there are no writes;
  * and if the machine that feeds it is off, the site keeps serving the
    last board it was given, stamped with when that was.

Render's free plan decided the shape. It has no cron, no background
worker, no persistent disk, and its free Postgres expires after thirty
days -- so a design that ran the chain up here would quietly stop a month
later. Instead the chain runs where it already works and pushes two
files; a push is a deploy.

    python web.py           # PORT, or 8000

Routes: /  the board, /healthz  for the platform, /api/meta  what this
copy was built from.
"""
import base64
import gzip
import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DIR = os.path.dirname(os.path.abspath(__file__))
TPL = os.path.join(DIR, "template.html")
DATA = os.path.join(DIR, "data.json")
PORT = int(os.environ.get("PORT", "8000"))
# Unset means the board is public, which is what The Odds API's terms
# allow: displaying their data in a website is permitted, reselling it as
# a feed is not. Set it in the Render dashboard to put the whole site
# behind one password -- any username, so there is no account to manage.
PASSWORD = os.environ.get("SITE_PASSWORD", "")

_page = None          # the spliced page
_stamp = None         # the file state it was spliced from


def _mtimes():
    return tuple(os.path.getmtime(f) for f in (TPL, DATA))


def page():
    """Template plus data, assembled from whatever is on disk now.

    The two are stored apart on purpose: the template is 90 KB and
    changes when the tool changes, the data is a quarter of a megabyte
    and changes every night. Committing the assembled page nightly would
    put a blob in the history every day for no reason.

    The splice is cached, but on the files' modification times rather
    than for the life of the process -- so a board written underneath a
    running server is served by the next request, and a refresh always
    shows what is actually there."""
    global _page, _stamp
    now = _mtimes()
    if _page is None or now != _stamp:
        with open(TPL) as fh:
            tpl = fh.read()
        with open(DATA) as fh:
            data = fh.read()
        json.loads(data)                  # refuse to serve a broken board
        head, _, rest = tpl.partition("/*DATA*/")
        _, _, tail = rest.partition("/*DATA*/")
        _page = (head + data + tail).replace('/*MODE*/"local"/*MODE*/',
                                             '"live"')
        _stamp = now
    return _page


def meta():
    try:
        with open(DATA) as fh:
            return json.load(fh).get("meta", {})
    except Exception:
        return {}


class H(BaseHTTPRequestHandler):
    server_version = "nba-props"
    protocol_version = "HTTP/1.1"

    def _send(self, code, body, ctype, cache="no-cache", etag=None):
        if isinstance(body, str):
            body = body.encode()
        hdrs = {}
        # The page is two thirds numbers and compresses to about a tenth
        # of itself; on a free instance that is the difference between a
        # fast first paint and a slow one.
        if len(body) > 1400 and "gzip" in (
                self.headers.get("Accept-Encoding") or ""):
            body = gzip.compress(body, 6)
            hdrs["Content-Encoding"] = "gzip"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if etag:
            self.send_header("ETag", etag)
        for k, v in hdrs.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _allowed(self):
        """Basic auth, only when a password has been set.

        Compared with compare_digest so the check does not leak the
        password's length through its timing, and the whole site is
        behind it -- there is nothing here worth protecting selectively."""
        if not PASSWORD:
            return True
        hdr = self.headers.get("Authorization") or ""
        if hdr.startswith("Basic "):
            try:
                raw = base64.b64decode(hdr[6:]).decode("utf-8", "replace")
            except Exception:
                raw = ""
            if hmac.compare_digest(raw.partition(":")[2], PASSWORD):
                return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="board"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_GET(self):
        path = self.path.split("?")[0]
        # The platform's health check must answer before the password, or
        # a protected site reads as a failed deploy and never goes live.
        if path == "/healthz":
            return self._send(200, "ok", "text/plain")
        if not self._allowed():
            return
        if path in ("/", "/index.html"):
            try:
                body = page()
            except FileNotFoundError:
                return self._send(503, "no board has been published yet",
                                  "text/plain")
            # An ETag over the file state, so a reload that finds nothing
            # changed costs a 304 instead of a quarter of a megabyte --
            # and a reload that does find a new board always gets it.
            tag = '"%x-%x"' % (int(_stamp[0]), int(_stamp[1]))
            if self.headers.get("If-None-Match") == tag:
                self.send_response(304)
                self.send_header("ETag", tag)
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            return self._send(200, body, "text/html; charset=utf-8",
                              etag=tag)
        if path == "/api/meta":
            return self._send(200, json.dumps(meta()), "application/json")
        self._send(404, "not found", "text/plain")

    do_HEAD = do_GET

    def log_message(self, fmt, *a):
        pass


if __name__ == "__main__":
    m = meta()
    print(f"serving {m.get('slate', '?')} (built {m.get('built', '?')}) "
          f"on 0.0.0.0:{PORT}"
          f"{' -- password protected' if PASSWORD else ' -- public'}",
          flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
