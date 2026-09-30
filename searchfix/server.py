"""searchfix: re-ranks Emby search responses.

nginx sends only requests that carry a SearchTerm query arg here; everything
else goes straight to Emby. Of those, two shapes are rewritten:

  GET .../ItemTypes?SearchTerm=...   rows reordered (rank.order_types)
  GET .../Items?SearchTerm=...       bigger candidate pool fetched from Emby,
                                     re-ranked (rank.rerank), trimmed back to
                                     the requested Limit

Left untouched on purpose:
  * requests with SortBy: the client asked for a specific order
  * requests with StartIndex: paged lists; re-ranking page 1 only would
    duplicate/skip items across pages
  * anything else carrying SearchTerm (Persons, Artists, ...)

Any failure while rewriting returns Emby's response unmodified. If Emby itself
is unreachable we answer 502, and nginx is configured to retry the request
directly against Emby.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import rank

UPSTREAM = os.environ.get("EMBY_UPSTREAM", "http://emby:8096").rstrip("/")
POOL = int(os.environ.get("CANDIDATE_POOL", "150"))
TIMEOUT = float(os.environ.get("UPSTREAM_TIMEOUT", "30"))
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8080"))
LOG_TERMS = os.environ.get("LOG_SEARCH_TERMS", "1") == "1"

HOP_HEADERS = {"host", "connection", "accept-encoding", "content-length",
               "transfer-encoding", "keep-alive", "upgrade", "te", "trailer",
               "proxy-authorization", "proxy-connection"}

_CLIENT_RE = re.compile(r'Client="([^"]*)"')


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + msg, file=sys.stderr, flush=True)


def classify(method, path, params):
    """Return 'types', 'items' or None for a request; params keys lowercased."""
    if method != "GET" or not params.get("searchterm"):
        return None
    if "sortby" in params or "startindex" in params:
        return None
    p = path.rstrip("/").lower()
    if p.endswith("/itemtypes"):
        return "types"
    if p.endswith("/items"):
        return "items"
    return None


def widen_items_query(query_pairs, params):
    """Items query with Limit raised to POOL and the fields rank.py reads."""
    new = [(k, v) for k, v in query_pairs if k.lower() not in ("limit", "fields")]
    fields = [f for f in params.get("fields", "").split(",") if f]
    for f in ("OriginalTitle", "SortName"):
        if f not in fields:
            fields.append(f)
    try:
        limit = int(params.get("limit") or 0)
    except ValueError:
        limit = 0
    new += [("Limit", str(max(limit, POOL))), ("Fields", ",".join(fields))]
    return urllib.parse.urlencode(new), limit


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "searchfix"

    def log_message(self, fmt, *args):
        pass

    def client_name(self):
        for h in ("X-Emby-Authorization", "Authorization"):
            m = _CLIENT_RE.search(self.headers.get(h, ""))
            if m:
                return m.group(1)
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        for k, v in q.items():
            if k.lower() == "x-emby-client":
                return v[0]
        return "?"

    def fetch(self, path_qs):
        body = None
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            body = self.rfile.read(n)
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in HOP_HEADERS}
        req = urllib.request.Request(UPSTREAM + path_qs, data=body,
                                     headers=headers, method=self.command)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.status, list(r.headers.items()), r.read()
        except urllib.error.HTTPError as e:
            return e.code, list(e.headers.items()), e.read()

    def send(self, status, headers, body, extra=()):
        self.send_response(status)
        for k, v in headers:
            if k.lower() in HOP_HEADERS or k.lower() in ("date", "server"):
                continue
            self.send_header(k, v)
        for k, v in extra:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def handle_any(self):
        if self.path == "/health":
            return self.send(200, [("Content-Type", "text/plain")], b"ok\n")

        parsed = urllib.parse.urlsplit(self.path)
        pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        params = {k.lower(): v for k, v in pairs}
        mode = classify(self.command, parsed.path, params)

        limit = 0
        path_qs = self.path
        if mode == "items":
            qs, limit = widen_items_query(pairs, params)
            path_qs = parsed.path + "?" + qs

        t0 = time.monotonic()
        try:
            status, headers, body = self.fetch(path_qs)
        except (urllib.error.URLError, OSError) as e:
            log(f"upstream error {e!r} for {parsed.path}")
            return self.send(502, [("Content-Type", "text/plain")], b"emby unreachable\n")
        ms = (time.monotonic() - t0) * 1000

        if status != 200 or mode is None:
            return self.send(status, headers, body)

        try:
            data = json.loads(body)
            items = data.get("Items", [])
            n_in = len(items)
            if mode == "types":
                data["Items"] = rank.order_types(items)
            else:
                items = rank.rerank(items, params["searchterm"])
                data["Items"] = items[:limit] if limit else items
            body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
            term = params["searchterm"] if LOG_TERMS else "-"
            log(f"{mode:5} client={self.client_name()!r} term={term!r} "
                f"in={n_in} out={len(data['Items'])} emby_ms={ms:.0f}")
            return self.send(status, headers, body, extra=[("X-Searchfix", mode)])
        except Exception as e:  # never break search: serve what Emby gave us
            log(f"rewrite failed ({e!r}) for {self.path}")
            if mode == "items" and limit:
                try:
                    data = json.loads(body)
                    data["Items"] = data.get("Items", [])[:limit]
                    body = json.dumps(data, separators=(",", ":")).encode()
                except Exception:
                    pass
            return self.send(status, headers, body)

    do_GET = do_POST = do_HEAD = do_DELETE = do_PUT = handle_any


def main():
    ThreadingHTTPServer.daemon_threads = True
    srv = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler)
    log(f"searchfix listening on :{LISTEN_PORT}, upstream {UPSTREAM}, pool {POOL}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
