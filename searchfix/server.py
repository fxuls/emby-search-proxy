"""searchfix: re-ranks Emby search responses.

nginx sends only requests that carry a SearchTerm query arg here; everything
else goes straight to Emby. Of those, two shapes are rewritten:

  GET .../ItemTypes?SearchTerm=...   rows reordered (rank.order_types)
  GET .../Items?SearchTerm=...       bigger candidate pool fetched from Emby,
                                     re-ranked (rank.rerank), trimmed back to
                                     the requested Limit

Sorting and paging:
  * SortBy that is only the client's default name sort (the web client's
    row loader adds SortBy=SortName to every search row) is replaced by
    relevance. Any other SortBy (date, rating, random...) is respected and
    the request passes through untouched.
  * StartIndex pages are served from one re-ranked list: we fetch items
    0..max(pool, start+limit) by Emby relevance, re-rank, then slice, so
    pages never duplicate or skip items.
  * Other endpoints carrying SearchTerm (Persons, Artists, ...) pass through.

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
from people import PeopleCounts

UPSTREAM = os.environ.get("EMBY_UPSTREAM", "http://emby:8096").rstrip("/")
POOL = int(os.environ.get("CANDIDATE_POOL", "150"))
TIMEOUT = float(os.environ.get("UPSTREAM_TIMEOUT", "30"))
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8080"))
LOG_TERMS = os.environ.get("LOG_SEARCH_TERMS", "1") == "1"
# Optional people signal: needs its own Emby API key (requests are otherwise
# made with each user's own credentials). Unset = signal off.
API_KEY = os.environ.get("EMBY_API_KEY", "")
PEOPLE_SNAPSHOT = os.environ.get("PEOPLE_SNAPSHOT", "/data/people_counts.json")
PEOPLE_REFRESH_HOURS = float(os.environ.get("PEOPLE_REFRESH_HOURS", "6"))
PEOPLE = None

HOP_HEADERS = {"host", "connection", "accept-encoding", "content-length",
               "transfer-encoding", "keep-alive", "upgrade", "te", "trailer",
               "proxy-authorization", "proxy-connection"}

_CLIENT_RE = re.compile(r'Client="([^"]*)"')


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + msg, file=sys.stderr, flush=True)


# Name sorts that clients add by default; relevance is a better order for a
# search, so these don't count as an explicit request for an order.
DEFAULT_NAME_SORTS = {
    "sortname",
    "name",
    "seriessortname,parentindexnumber,indexnumber,sortname",
}


def is_default_sort(sortby):
    return sortby.replace(" ", "").lower() in DEFAULT_NAME_SORTS


def classify(method, path, params):
    """Return 'types', 'items' or None for a request; params keys lowercased."""
    if method != "GET" or not params.get("searchterm"):
        return None
    if params.get("sortby") and not is_default_sort(params["sortby"]):
        return None
    p = path.rstrip("/").lower()
    if p.endswith("/itemtypes"):
        return "types"
    if p.endswith("/items"):
        return "items"
    return None


def _int(v):
    try:
        return max(int(v), 0)
    except (TypeError, ValueError):
        return 0


def widen_items_query(query_pairs, params):
    """Rewrite an Items search for re-ranking.

    Returns (query string for Emby, start, limit). Emby is asked for items
    0..max(POOL, start+limit) in its own relevance order (sort and paging
    dropped), with the fields rank.py reads. limit 0 means "no limit": then
    Limit is left off so Emby returns everything, as the client expects.
    """
    drop = ("limit", "fields", "startindex", "sortby", "sortorder")
    new = [(k, v) for k, v in query_pairs if k.lower() not in drop]
    fields = [f for f in params.get("fields", "").split(",") if f]
    for f in ("OriginalTitle", "SortName"):
        if f not in fields:
            fields.append(f)
    start, limit = _int(params.get("startindex")), _int(params.get("limit"))
    if limit:
        new.append(("Limit", str(max(start + limit, POOL))))
    new.append(("Fields", ",".join(fields)))
    return urllib.parse.urlencode(new), start, limit


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
            people = f"people={len(PEOPLE)}" if PEOPLE and PEOPLE.ready else "people=off"
            return self.send(200, [("Content-Type", "text/plain")], f"ok {people}\n".encode())

        parsed = urllib.parse.urlsplit(self.path)
        pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        params = {k.lower(): v for k, v in pairs}
        mode = classify(self.command, parsed.path, params)

        start = limit = 0
        path_qs = self.path
        if mode == "items":
            qs, start, limit = widen_items_query(pairs, params)
            path_qs = parsed.path + "?" + qs

        t0 = time.monotonic()
        try:
            status, headers, body = self.fetch(path_qs)
        except (urllib.error.URLError, OSError) as e:
            log(f"upstream error {e!r} for {parsed.path}")
            return self.send(502, [("Content-Type", "text/plain")], b"emby unreachable\n")
        ms = (time.monotonic() - t0) * 1000

        if mode is None:
            if params.get("searchterm"):
                keys = ",".join(sorted(k for k in params if k not in ("api_key", "x-emby-token")))
                log(f"skip  client={self.client_name()!r} {self.command} {parsed.path} "
                    f"status={status} params={keys} startindex={params.get('startindex', '-')} "
                    f"sortby={params.get('sortby', '-')} types={params.get('includeitemtypes', '-')}")
            return self.send(status, headers, body)
        if status != 200:
            return self.send(status, headers, body)

        try:
            data = json.loads(body)
            items = data.get("Items", [])
            n_in = len(items)
            if mode == "types":
                data["Items"] = rank.order_types(items)
            else:
                lookup = PEOPLE.get if PEOPLE and PEOPLE.ready else None
                items = rank.rerank(items, params["searchterm"], lookup)
                data["Items"] = items[start:start + limit] if limit else items[start:]
            body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
            term = params["searchterm"] if LOG_TERMS else "-"
            log(f"{mode:5} client={self.client_name()!r} term={term!r} "
                f"types={params.get('includeitemtypes', '-')} start={start} "
                f"in={n_in} out={len(data['Items'])} emby_ms={ms:.0f}")
            return self.send(status, headers, body, extra=[("X-Searchfix", mode)])
        except Exception as e:  # never break search: serve what Emby gave us
            log(f"rewrite failed ({e!r}) for {self.path}")
            if mode == "items":
                try:
                    data = json.loads(body)
                    its = data.get("Items", [])
                    data["Items"] = its[start:start + limit] if limit else its[start:]
                    body = json.dumps(data, separators=(",", ":")).encode()
                except Exception:
                    pass
            return self.send(status, headers, body)

    do_GET = do_POST = do_HEAD = do_DELETE = do_PUT = handle_any


def main():
    global PEOPLE
    if API_KEY:
        PEOPLE = PeopleCounts(UPSTREAM, API_KEY, PEOPLE_SNAPSHOT, PEOPLE_REFRESH_HOURS, log).start()
    else:
        log("people: EMBY_API_KEY not set, people signal off")
    ThreadingHTTPServer.daemon_threads = True
    srv = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler)
    log(f"searchfix listening on :{LISTEN_PORT}, upstream {UPSTREAM}, pool {POOL}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
