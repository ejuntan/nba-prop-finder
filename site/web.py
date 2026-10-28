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
import gzip
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DIR = os.path.dirname(os.path.abspath(__file__))
TPL = os.path.join(DIR, "template.html")
DATA = os.path.join(DIR, "data.json")
PORT = int(os.environ.get("PORT", "8000"))

_page = None          # the spliced page, built once and kept


def page():
    """Template plus data, assembled once per process.

    The two are stored apart on purpose: the template is 50 KB and
    changes when the tool changes, the data is half a megabyte and
    changes every night. Committing the assembled page nightly would put
    a 650 KB blob in the history every day for no reason."""
    global _page
    if _page is None:
        with open(TPL) as fh:
            tpl = fh.read()
        with open(DATA) as fh:
            data = fh.read()
        json.loads(data)                  # refuse to serve a broken board
        head, _, rest = tpl.partition("/*DATA*/")
        _, _, tail = rest.partition("/*DATA*/")
        _page = (head + data + tail).replace('/*MODE*/"local"/*MODE*/',
                                             '"live"')
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

    def _send(self, code, body, ctype, cache="no-cache"):
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
        for k, v in hdrs.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            try:
                return self._send(200, page(), "text/html; charset=utf-8")
            except FileNotFoundError:
                return self._send(503, "no board has been published yet",
                                  "text/plain")
        if path == "/healthz":
            return self._send(200, "ok", "text/plain")
        if path == "/api/meta":
            return self._send(200, json.dumps(meta()), "application/json")
        self._send(404, "not found", "text/plain")

    do_HEAD = do_GET

    def log_message(self, fmt, *a):
        pass


if __name__ == "__main__":
    m = meta()
    print(f"serving {m.get('slate', '?')} (built {m.get('built', '?')}) "
          f"on 0.0.0.0:{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
