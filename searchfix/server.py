"""searchfix: re-ranks Emby search responses, and keeps HSC top-list copies
on their home rows (toplist.py).

nginx sends three kinds of request here; everything else goes straight to Emby:
  GET .../Users/{u}/Sections/{id}/Items   home rows: HSC copies -> real titles
  GET .../Items/{id}/Images/...?tag=hsc…  numbered top-list posters
  anything carrying a SearchTerm query arg, of which two shapes are rewritten:

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

import expand
import fuzzy
import rank
import toplist
from rank import drop_hsc_copies  # noqa: F401  (re-exported for tests)
from library import LibraryIndex
from searchlog import SearchLog, UserNames, parse_auth, user_id_from

UPSTREAM = os.environ.get("EMBY_UPSTREAM", "http://emby:8096").rstrip("/")
POOL = int(os.environ.get("CANDIDATE_POOL", "150"))
TIMEOUT = float(os.environ.get("UPSTREAM_TIMEOUT", "30"))
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8080"))
LOG_TERMS = os.environ.get("LOG_SEARCH_TERMS", "1") == "1"
# Mixed lists (no IncludeItemTypes) get a second candidate fetch limited to
# these types, merged in front. Emby's own order can fill the whole pool with
# people ("john": 147 of 150 were people), so titles never got ranked at all.
MEDIA_TYPES = os.environ.get("MEDIA_POOL_TYPES", "Series,Movie,BoxSet")
# Optional people signal: needs its own Emby API key (requests are otherwise
# made with each user's own credentials). Unset = signal off.
API_KEY = os.environ.get("EMBY_API_KEY", "")
PEOPLE_SNAPSHOT = os.environ.get("PEOPLE_SNAPSHOT", "/data/library_index.json")
PEOPLE_REFRESH_HOURS = float(os.environ.get("PEOPLE_REFRESH_HOURS", "6"))
SEARCH_DB = os.environ.get("SEARCH_LOG_DB", "/data/searches.db")
FUZZY_ENABLED = os.environ.get("FUZZY", "1") == "1"
PEOPLE = None      # LibraryIndex: person title counts + names
FUZZY = None       # FuzzyIndex, rebuilt whenever PEOPLE rebuilds
EXPANDER = None    # expand.Expander (rare misspelled word -> also search the common word)
SEARCHLOG = None
# HSC top-list rows (toplist.py): needs EMBY_API_KEY to map copies to originals.
TOPLIST_ENABLED = os.environ.get("TOPLIST_ROWS", "1") == "1"
TOPLIST_REFRESH_MINUTES = float(os.environ.get("TOPLIST_REFRESH_MINUTES", "10"))
COPIES = None      # toplist.CopyMap
SECTION_TTL = 300
_SECTIONS = {}     # user id -> (monotonic time, {section id: parent library id})

# Presentation params copied from the client's request when fetching fuzzy
# matches by Id, so they come back in the same shape as the rest of the list.
ID_FETCH_KEEP = {"fields", "imagetypelimit", "enableimagetypes", "enableuserdata",
                 "enableimages", "api_key", "userid"}

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
    for f in ("OriginalTitle", "SortName", "Path", "ProviderIds", "ProductionYear"):
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

    @staticmethod
    def with_term(path_qs, new_term):
        """path_qs with its SearchTerm value replaced."""
        u = urllib.parse.urlsplit(path_qs)
        pairs = [(k, new_term if k.lower() == "searchterm" else v)
                 for k, v in urllib.parse.parse_qsl(u.query, keep_blank_values=True)]
        return u.path + "?" + urllib.parse.urlencode(pairs)

    def expansion(self, path, params, pairs):
        """Corrected term to search as well, or None (see expand.py)."""
        if EXPANDER is None:
            return None
        uid = user_id_from(path, params)
        if not uid:
            return None
        auth = [(k, v) for k, v in pairs if k.lower() == "api_key" or k.lower().startswith("x-emby-")]

        def fetch_mixed():
            qs = urllib.parse.urlencode(auth + [
                ("SearchTerm", params["searchterm"]), ("Recursive", "true"),
                ("Limit", str(POOL)), ("Fields", "OriginalTitle,SortName")])
            st, _, b = self.fetch(f"/emby/Users/{uid}/Items?{qs}")
            return json.loads(b).get("Items", []) if st == 200 else []

        try:
            return EXPANDER.decide(params["searchterm"], fetch_mixed, key=uid)
        except Exception as e:
            log(f"expand: failed ({e!r})")
            return None

    def record_search(self, path, params, results=None, top=None, fuzzy=False, ms=None,
                      corrected=None):
        if SEARCHLOG is None or self.headers.get("X-Searchfix-Test"):
            return  # smoke/eval traffic stays out of the household search log
        try:
            auth = parse_auth(self.headers, params)
            SEARCHLOG.record(
                term=params.get("searchterm", ""), user_id=user_id_from(path, params),
                ip=self.headers.get("X-Real-IP") or self.client_address[0],
                results=results, top_type=(top or {}).get("Type"), top_name=(top or {}).get("Name"),
                fuzzy=fuzzy, corrected=corrected,
                emby_ms=int(ms) if ms is not None else None, **auth)
        except Exception as e:
            log(f"searchlog: record failed ({e!r})")

    def fuzzy_head(self, items, term, path, pairs):
        """Typo matches for term, moved/fetched to the front. Returns (head, rest)."""
        try:
            cands = FUZZY.search(term)
        except Exception as e:
            log(f"fuzzy: search failed ({e!r})")
            return [], items
        if not cands:
            return [], items
        by_id = {i.get("Id"): i for i in items}
        missing = [(iid, type_) for _, iid, type_, _ in cands if iid not in by_id]
        # Only fetch for a known user, with the user's own credentials, so Emby's
        # per-user access rules and parental controls decide what comes back.
        # Emby only returns people by Id when asked for IncludeItemTypes=Person,
        # and drops them from a mixed-type Ids query, so people get their own fetch.
        if missing and "/users/" in path.lower():
            keep = [(k, v) for k, v in pairs
                    if k.lower() in ID_FETCH_KEEP or k.lower().startswith("x-emby-")]
            fields = [f for f in dict(keep).get("Fields", "").split(",") if f]
            for f in ("Path", "ProviderIds", "ProductionYear"):
                if f not in fields:
                    fields.append(f)
            keep = [(k, v) for k, v in keep if k.lower() != "fields"] + [("Fields", ",".join(fields))]
            groups = [([i for i, t in missing if t != "Person"], []),
                      ([i for i, t in missing if t == "Person"],
                       [("IncludeItemTypes", "Person"), ("Recursive", "true")])]
            for ids, extra in groups:
                if not ids:
                    continue
                qs = urllib.parse.urlencode(keep + extra + [("Ids", ",".join(ids))])
                try:
                    st, _, b = self.fetch(f"{path}?{qs}")
                    if st == 200:
                        for i in json.loads(b).get("Items", []):
                            by_id.setdefault(i.get("Id"), i)
                except Exception as e:
                    log(f"fuzzy: id fetch failed ({e!r})")
        head_ids = [iid for _, iid, _, _ in cands if iid in by_id]
        seen = set(head_ids)
        return [by_id[i] for i in head_ids], [i for i in items if i.get("Id") not in seen]

    def section_parent(self, prefix, uid, sid, pairs):
        """The library a home section shows (its ParentItem), "" if none, None if unknown."""
        now = time.monotonic()
        hit = _SECTIONS.get(uid)
        if hit is None or now - hit[0] > SECTION_TTL or sid not in hit[1]:
            auth = [(k, v) for k, v in pairs if k.lower() == "api_key" or k.lower().startswith("x-emby-")]
            st, _, b = self.fetch(f"{prefix}/Users/{uid}/HomeSections?{urllib.parse.urlencode(auth)}")
            if st == 200:
                d = json.loads(b)
                rows = d if isinstance(d, list) else d.get("Items", [])
                hit = (now, {s.get("Id"): (s.get("ParentItem") or {}).get("Id") or s.get("ParentId") or ""
                             for s in rows})
                _SECTIONS[uid] = hit
        return hit[1].get(sid) if hit else None

    def handle_section(self, prefix, uid, sid, pairs):
        """A home row: HSC copies become their real titles (toplist.py)."""
        try:
            status, headers, body = self.fetch(self.path)
        except (urllib.error.URLError, OSError) as e:
            log(f"upstream error {e!r} for section {sid}")
            return self.send(502, [("Content-Type", "text/plain")], b"emby unreachable\n")
        if status != 200:
            return self.send(status, headers, body)
        try:
            data = json.loads(body)
            items = data.get("Items") or []
            ids = [COPIES.original(i.get("Id")) for i in items]
            wanted = sorted({i for i in ids if i})
            if not wanted:
                return self.send(status, headers, body)
            parent = self.section_parent(prefix, uid, sid, pairs)
            numbered = (parent in COPIES.libraries) if parent is not None else all(ids)
            keep = [(k, v) for k, v in pairs
                    if k.lower() in ID_FETCH_KEEP or k.lower().startswith("x-emby-")]
            qs = urllib.parse.urlencode(keep + [("Ids", ",".join(wanted))])
            st, _, b = self.fetch(f"{prefix}/Users/{uid}/Items?{qs}")
            originals = {i.get("Id"): i for i in json.loads(b).get("Items", [])} if st == 200 else {}
            data["Items"] = toplist.swap_items(items, COPIES.original, originals, numbered)
            log(f"row   client={self.client_name()!r} section={sid} numbered={numbered} "
                f"copies={len(wanted)} swapped={len(originals)}")
            out = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
            return self.send(status, headers, out, extra=[("X-Searchfix", "row")])
        except Exception as e:  # never break the home screen: serve what Emby gave us
            log(f"row rewrite failed ({e!r}) for section {sid}")
            return self.send(status, headers, body)

    def handle_hsc_image(self, path, copy_pairs, plain_pairs):
        """A numbered top-list poster: served from the copy. If HSC has rebuilt the
        copy since the row was loaded, the real title's own image instead."""
        try:
            status, headers, body = self.fetch(path + "?" + urllib.parse.urlencode(copy_pairs))
            if status >= 400:   # Emby answers 500 for an image of a deleted item
                own = urllib.parse.urlsplit(self.path).path
                status, headers, body = self.fetch(own + "?" + urllib.parse.urlencode(plain_pairs))
        except (urllib.error.URLError, OSError) as e:
            log(f"upstream error {e!r} for image {path}")
            return self.send(502, [("Content-Type", "text/plain")], b"emby unreachable\n")
        return self.send(status, headers, body)

    def handle_any(self):
        if self.path == "/health":
            people = f"people={len(PEOPLE)}" if PEOPLE and PEOPLE.ready else "people=off"
            fz = f"fuzzy={len(FUZZY)}" if FUZZY is not None else "fuzzy=off"
            sl = "searchlog=on" if SEARCHLOG else "searchlog=off"
            tl = f"toplist={len(COPIES)}" if COPIES and COPIES.ready else "toplist=off"
            return self.send(200, [("Content-Type", "text/plain")], f"ok {people} {fz} {sl} {tl}\n".encode())

        parsed = urllib.parse.urlsplit(self.path)
        pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        params = {k.lower(): v for k, v in pairs}

        img = toplist.image_target(self.command, parsed.path, pairs)
        if img:
            return self.handle_hsc_image(*img)
        sec = toplist.section_request(self.command, parsed.path)
        if sec and COPIES is not None and COPIES.ready:
            return self.handle_section(*sec, pairs)
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
        media_body = None
        if mode == "items" and status == 200 and MEDIA_TYPES and "includeitemtypes" not in params:
            try:
                st, _, media_body = self.fetch(path_qs + "&" + urllib.parse.urlencode(
                    {"IncludeItemTypes": MEDIA_TYPES}))
                if st != 200:
                    media_body = None
            except (urllib.error.URLError, OSError):
                media_body = None  # the main pool alone is still fine
        corrected = None
        extra_bodies = []   # responses for the corrected term, merged after the originals
        if mode is not None and status == 200:
            corrected = self.expansion(parsed.path, params, pairs)
            if corrected:
                alt_qs = [self.with_term(path_qs, corrected)]
                if media_body is not None:
                    alt_qs.append(self.with_term(path_qs + "&" + urllib.parse.urlencode(
                        {"IncludeItemTypes": MEDIA_TYPES}), corrected))
                for q in alt_qs:
                    try:
                        st, _, b = self.fetch(q)
                        if st == 200:
                            extra_bodies.append(b)
                    except (urllib.error.URLError, OSError):
                        pass
        ms = (time.monotonic() - t0) * 1000

        if mode is None:
            if params.get("searchterm"):
                self.record_search(parsed.path, params)
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
            if media_body is not None:
                media = json.loads(media_body).get("Items", [])
                seen = {i.get("Id") for i in media}
                items = media + [i for i in items if i.get("Id") not in seen]
            if extra_bodies:
                key = (lambda i: i.get("Name")) if mode == "types" else (lambda i: i.get("Id"))
                seen = {key(i) for i in items}
                for b in extra_bodies:
                    for i in json.loads(b).get("Items", []):
                        if key(i) not in seen:
                            seen.add(key(i))
                            items.append(i)
            n_in = len(items)
            used_fuzzy = False
            if mode == "types":
                data["Items"] = rank.order_types(items)
            else:
                lookup = PEOPLE.get if PEOPLE and PEOPLE.ready else None
                head = []
                if (FUZZY is not None and "includeitemtypes" not in params
                        and not fuzzy.has_whole_query_match(items, params["searchterm"])
                        and not (corrected and fuzzy.has_whole_query_match(items, corrected))):
                    head, items = self.fuzzy_head(items, params["searchterm"], parsed.path, pairs)
                    used_fuzzy = bool(head)
                items = rank.drop_hsc_copies(head + rank.rerank(items, params["searchterm"], lookup, corrected))
                data["Items"] = items[start:start + limit] if limit else items[start:]
            body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
            term = params["searchterm"] if LOG_TERMS else "-"
            log(f"{mode:5} client={self.client_name()!r} term={term!r} "
                f"types={params.get('includeitemtypes', '-')} start={start} "
                f"in={n_in} out={len(data['Items'])} emby_ms={ms:.0f}"
                + (" fuzzy" if used_fuzzy else "") + (f" +{corrected!r}" if corrected else ""))
            mixed = mode == "items" and "includeitemtypes" not in params and start == 0
            top = data["Items"][0] if (mixed and data["Items"]) else {}
            self.record_search(parsed.path, params, results=n_in if mixed else None,
                               top=top, fuzzy=used_fuzzy, ms=ms, corrected=corrected)
            return self.send(status, headers, body,
                             extra=[("X-Searchfix", mode + (" fuzzy" if used_fuzzy else "")
                                     + (" expanded" if corrected else ""))])
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


def _rebuild_fuzzy(lib):
    global FUZZY, EXPANDER
    t0 = time.monotonic()
    idx = fuzzy.FuzzyIndex(lib.names)
    ex = expand.Expander(expand.Corrector(lib.names))
    FUZZY, EXPANDER = idx, ex  # atomic swap; in-flight searches keep the old ones
    log(f"fuzzy: indexed {len(idx)} names, {len(ex.corrector.freq)} words "
        f"in {time.monotonic() - t0:.1f}s")


def main():
    global PEOPLE, SEARCHLOG, COPIES
    if API_KEY:
        PEOPLE = LibraryIndex(UPSTREAM, API_KEY, PEOPLE_SNAPSHOT, PEOPLE_REFRESH_HOURS, log)
        if FUZZY_ENABLED:
            PEOPLE.on_build.append(_rebuild_fuzzy)
        PEOPLE.start()
    else:
        log("library: EMBY_API_KEY not set, people signal and fuzzy search off")
    if API_KEY and TOPLIST_ENABLED:
        COPIES = toplist.CopyMap(UPSTREAM, API_KEY, TOPLIST_REFRESH_MINUTES, log).start()
    try:
        SEARCHLOG = SearchLog(SEARCH_DB, UserNames(UPSTREAM, API_KEY), log).start()
    except Exception as e:
        log(f"searchlog: disabled ({e!r})")
    ThreadingHTTPServer.daemon_threads = True
    srv = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler)
    log(f"searchfix listening on :{LISTEN_PORT}, upstream {UPSTREAM}, pool {POOL}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
