#!/usr/bin/env python3
"""Self-search sweep: does every show, movie and collection come first when you
search for its own title?

    EMBY_API_KEY=... scripts/selfsearch.py [BASE_URL] [--user NAME] [--json FILE]

BASE_URL is the proxy (default http://localhost:8096). Read-only.

1. Lists the library once (shows, movies, collections, episodes, people,
   studios; names only). Plain listings, no SearchTerm, so they go straight
   to Emby.
2. Offline: for each title, the query is the title as people type it (no
   "(US)"/"(2005)" qualifier), plus the same without apostrophes. The pool is
   every library name containing the query (roughly what Emby returns), ranked
   with this checkout's rank.py. A "loss" is anything ranked above the target
   that isn't the same title (remakes, HSC copies and "The X" vs "X" tie).
3. Live: each offline loss is searched once through the proxy as an admin
   user (with X-Searchfix-Test, so it stays out of the search log). Only a
   loss the live proxy also shows counts.

Exit code: 0 clean, 1 confirmed losses, 2 setup error. The offline pass is a
superset, so a clean live result means the whole library passed. Measured
2026-11-06: 2,900 titles, 3,023 queries, ~25 s offline. Before 1.8.6 it found
exactly one real loss (Hell's Kitchen (US) under two episodes); after, none.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "searchfix"))
import rank  # noqa: E402

TARGET_TYPES = ("Series", "Movie", "BoxSet")
# How people type a title: without the "(US)"/"(2005)" qualifier. Defined here, not
# borrowed from rank.py, so the test doesn't share the code it is testing.
QUALIFIER = re.compile(r"\s*\((?:[A-Z]{2,3}|\d{4})\)\s*$")
HDR = {"X-Searchfix-Test": "1"}


def get(base, key, path, params=None, timeout=120):
    q = urllib.parse.urlencode({"api_key": key, **(params or {})})
    req = urllib.request.Request(f"{base}/emby{path}?{q}", headers=HDR)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def list_all(base, key, path, params):
    out, start = [], 0
    while True:
        d = get(base, key, path, {**params, "StartIndex": start, "Limit": 20000})
        got = d.get("Items", [])
        out += got
        start += len(got)
        if not got or start >= d.get("TotalRecordCount", 0):
            return out


def load_library(base, key):
    lite = {"Recursive": "true", "EnableImages": "false", "EnableUserData": "false"}
    lib = []
    for t in TARGET_TYPES + ("Episode",):
        lib += list_all(base, key, "/Items", {**lite, "IncludeItemTypes": t,
                                             "Fields": "OriginalTitle,SortName"})
    for path in ("/Persons", "/Studios"):
        lib += list_all(base, key, path, lite)
    return [{k: i.get(k) for k in ("Id", "Type", "Name", "OriginalTitle", "SortName", "SeriesName")}
            for i in lib]


def title_key(item):
    """Items with the same key are the same title for this test."""
    return rank._ARTICLE.sub("", rank.norm(QUALIFIER.sub("", item.get("Name") or "")))


def queries_for(item):
    q = QUALIFIER.sub("", item["Name"]).strip()
    out = {"as typed": q}
    plain = q.replace("'", "").replace("’", "")
    if plain != q:
        out["no apostrophe"] = plain
    return out


def ahead_of(ranked, target):
    """Items ranked above target that aren't the same title."""
    key = title_key(target)
    out = []
    for i in ranked:
        if i.get("Id") == target.get("Id"):
            return out
        if not (i.get("Type") in TARGET_TYPES and title_key(i) == key):
            out.append(i)
    return out


def offline(lib):
    names = [(rank.norm(i.get("Name")), i) for i in lib]
    losses, n = [], 0
    for t in lib:
        if t["Type"] not in TARGET_TYPES or not t.get("Name"):
            continue
        for style, q in queries_for(t).items():
            nq = rank.norm(q)
            if not nq:
                continue
            n += 1
            pool = [i for nm, i in names if nq in nm or i is t]
            beat = ahead_of(rank.rerank(pool, q), t)
            if beat:
                losses.append((t, style, q, beat[0]))
    return n, losses


def pick_user(base, key, want):
    users = get(base, key, "/Users")
    for u in users:
        if want and want in (u.get("Name"), u.get("Id")):
            return u["Id"]
    if want:
        sys.exit(f"no Emby user {want!r}")
    admins = [u for u in users if (u.get("Policy") or {}).get("IsAdministrator")]
    if not admins:
        sys.exit("no admin user to search as (pass --user)")
    return admins[0]["Id"]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("base", nargs="?", default="http://localhost:8096")
    ap.add_argument("--user", help="Emby user name or Id to search as (default: first admin)")
    ap.add_argument("--json", help="also write the result here")
    ap.add_argument("--offline-only", action="store_true", help="skip the live confirmation")
    args = ap.parse_args()
    key = os.environ.get("EMBY_API_KEY")
    if not key:
        print("set EMBY_API_KEY", file=sys.stderr)
        return 2
    base = args.base.rstrip("/")
    t0 = time.time()
    try:
        lib = load_library(base, key)
        uid = None if args.offline_only else pick_user(base, key, args.user)
    except OSError as e:
        print(f"library fetch failed: {e!r}", file=sys.stderr)
        return 2
    targets = sum(1 for i in lib if i["Type"] in TARGET_TYPES)
    n, losses = offline(lib)
    print(f"{targets} titles, {n} queries, {len(lib)} names; offline losses: {len(losses)} "
          f"({time.time() - t0:.0f}s)")

    confirmed = []
    for t, style, q, beat in losses:
        if args.offline_only:
            confirmed.append((t, style, q, beat, None))
            continue
        try:
            live = get(base, key, f"/Users/{uid}/Items",
                       {"SearchTerm": q, "Recursive": "true", "Limit": "16"})["Items"]
        except OSError as e:
            print(f"  live check failed for {q!r}: {e!r}", file=sys.stderr)
            continue
        live_beat = ahead_of(live, t)
        found = any(i.get("Id") == t["Id"] for i in live)
        if live_beat or not found:
            confirmed.append((t, style, q, (live_beat or [None])[0], found))
        time.sleep(0.2)   # Emby search is local, but keep it gentle

    label = "offline" if args.offline_only else "confirmed live"
    print(f"{label} losses: {len(confirmed)}")
    for t, style, q, beat, found in confirmed:
        who = f"{beat['Type']} {beat['Name']!r}" + (f" ({beat['SeriesName']})" if beat.get("SeriesName") else "") \
            if beat else "(not in the first 16 at all)" if found is False else "?"
        print(f"  {t['Type']:7} {t['Name']!r:40} q={q!r} [{style}]  beaten by {who}")
    if args.json:
        with open(args.json, "w") as f:
            json.dump({"at": int(time.time()), "titles": targets, "queries": n,
                       "offline_losses": len(losses), "mode": label,
                       "losses": [{"title": t["Name"], "type": t["Type"], "query": q, "style": style,
                                   "beaten_by": beat and {"type": beat["Type"], "name": beat["Name"],
                                                          "series": beat.get("SeriesName")}}
                                  for t, style, q, beat, _ in confirmed]}, f, indent=1)
    return 1 if confirmed else 0


if __name__ == "__main__":
    sys.exit(main())
