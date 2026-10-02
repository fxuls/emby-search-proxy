#!/usr/bin/env python3
"""Search-quality evaluation: stock Emby vs searchfix ranking, many queries.

Runs on the compose network so it can reach stock Emby directly:

  docker run --rm --network emby_default -v ~/homelab/emby-search-proxy:/repo:ro \\
    -v /home/mc/docker/emby/searchfix-data:/data:ro -e EMBY_API_KEY=... -e EMBY_USER_ID=... \\
    python:3.12-alpine python /repo/scripts/eval.py [--n 400] [--variants] [--live]

What it does
  1. Samples real targets from the library (movies, shows, collections,
     people from top billing, studios), fixed seed so runs are comparable.
  2. Builds the queries people type: full title, title without leading
     article, first two words, first word, first 4 letters.
  3. For each query fetches Emby's candidate pool once (exactly what searchfix
     fetches), then ranks it as stock Emby does (Emby order) and as searchfix
     does (rank.py, with the people table from the snapshot).
  4. Reports where the target lands in Top Results (first 50): hit@1/@3/@10,
     MRR, per query style and type, plus the worst regressions.
  5. Hand-written expectation checks (CASES below).

--variants  also scores alternative weight settings (VARIANTS below)
--live      also asks the live proxy for a sample and checks it matches rank.py
"""
import argparse
import json
import os
import random
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(REPO, "searchfix"))
import rank  # noqa: E402

EMBY = os.environ.get("EMBY_URL", "http://emby:8096")
PROXY = os.environ.get("PROXY_URL", "http://emby-proxy:8096")
KEY = os.environ["EMBY_API_KEY"]
UID = os.environ["EMBY_USER_ID"]
SNAPSHOT = os.environ.get("PEOPLE_SNAPSHOT", "/data/library_index.json")
TOP = 50          # what the web client asks for in Top Results
POOL = 150        # what searchfix fetches

# Hand-written checks: (query, description, predicate over the ranked list)
def _pos(items, name=None, type_=None, prefix=None):
    for n, i in enumerate(items):
        if type_ and i.get("Type") != type_:
            continue
        nm = i.get("Name", "")
        if (name and nm == name) or (prefix and nm.lower().startswith(prefix)) or (not name and not prefix):
            return n + 1
    return None

def _before(items, a_type, b_type):
    """every a_type item whose match tier <= best b_type tier comes before it (loose)."""
    pa, pb = _pos(items, type_=a_type), _pos(items, type_=b_type)
    return pa is None or pb is None or pa < pb

CASES = [
    ("harry", "Harry Potter film is #1", lambda it: (it[0]["Name"].startswith("Harry Potter"))),
    ("harry", "Dirty Harry (movie) before first person", lambda it: _pos(it, "Dirty Harry", "Movie") < _pos(it, type_="Person")),
    ("harry", "Dirty Harry Collection before first person", lambda it: (_pos(it, "Dirty Harry Collection") or 99) < (_pos(it, type_="Person") or 99)),
    ("harry", "a Harry with 9 titles leads the people", lambda it: [i["Name"] for i in it if i["Type"] == "Person"][0] in ("Harry Shearer", "Harry Groener", "Harry Dean Stanton")),
    ("harry", "studio within top 30", lambda it: (_pos(it, type_="Studio") or 99) <= 30),
    ("la la", "La La Land #1", lambda it: it[0]["Name"] == "La La Land"),
    ("the office", "The Office (US) #1", lambda it: it[0]["Name"] == "The Office (US)"),
    ("office", "a The Office/Office Space title in top 2", lambda it: any(i["Name"] in ("The Office (US)", "Office Space") for i in it[:2])),
    ("star wars", "Star Wars #1", lambda it: it[0]["Name"] == "Star Wars"),
    ("inter", "Interstellar in top 3", lambda it: (_pos(it, "Interstellar") or 99) <= 3),
    ("fox", "Fox and the Hound in top 3", lambda it: (_pos(it, prefix="the fox and the hound") or 99) <= 3),
    ("comedy", "Comedy genre #1", lambda it: it[0]["Name"] == "Comedy" and it[0]["Type"] == "Genre"),
    ("tom hanks", "Tom Hanks in top 3", lambda it: (_pos(it, "Tom Hanks", "Person") or 99) <= 3),
    ("breaking bad", "Breaking Bad #1", lambda it: it[0]["Name"] == "Breaking Bad"),
    ("simpsons", "The Simpsons in top 3", lambda it: (_pos(it, prefix="the simpsons") or 99) <= 3),
    ("christmas", "first item is media (show/movie/collection)", lambda it: it[0]["Type"] in ("Series", "Movie", "BoxSet")),
    ("batman", "a Batman title #1", lambda it: "batman" in it[0]["Name"].lower()),
    ("spider", "a Spider- title #1", lambda it: it[0]["Name"].lower().startswith("spider")),
    ("lord of the rings", "a Lord of the Rings title #1", lambda it: "lord of the rings" in it[0]["Name"].lower()),
    ("pixar", "Pixar studio in top 5", lambda it: (_pos(it, prefix="pixar", type_="Studio") or 99) <= 5),
    ("eddie murphy", "Eddie Murphy (person) #1", lambda it: it[0]["Name"] == "Eddie Murphy" and it[0]["Type"] == "Person"),
    ("gilbert", "studio Gilbert Films before first person", lambda it: (_pos(it, "Gilbert Films") or 99) < (_pos(it, type_="Person") or 99)),
    ("office olympics", "episode Office Olympics in top 3", lambda it: (_pos(it, "Office Olympics", "Episode") or 99) <= 3),
    ("time travel", "a tag is not #1 (tags stay last)", lambda it: it[0]["Type"] != "Tag"),
    ("john", "media before 4th person (no person flood)", lambda it: sum(1 for i in it[:10] if i["Type"] == "Person") <= 6),
    ("the", "top 10 has no tags", lambda it: all(i["Type"] != "Tag" for i in it[:10])),
]

# Typo checks, run against the live proxy only (fuzzy lives in server.py):
# (query, substring expected in the first result's name)
TYPOS = [
    ("harry poter", "Harry Potter"), ("interstelar", "Interstellar"), ("brakng bad", "Breaking Bad"),
    ("the ofice", "The Office"), ("lord of the rngs", "Lord of the Rings"), ("jurasic park", "Jurassic Park"),
    ("tom hnks", "Tom Hanks"), ("goodfelas", "GoodFellas"), ("spiderman", "Spider-Man"),
    ("avengrs endgame", "Avengers: Endgame"), ("simpsns", "Simpsons"), ("godfahter", "Godfather"),
    ("star wrs", "Star Wars"), ("finding nmo", "Finding Nemo"), ("incepton", "Inception"),
    ("gladiater", "Gladiator"), ("matrx", "Matrix"), ("scarlet johansson", "Scarlett Johansson"),
    ("toy stroy", "Toy Story"), ("game of thornes", "Game of Thrones"),
    ("stranger thigns", "Stranger Things"), ("pulp fction", "Pulp Fiction"),
    ("leonardo dicapro", "Leonardo DiCaprio"), ("denzel washingon", "Denzel Washington"),
    ("jurrasic world", "Jurassic World"), ("the dark night", "The Dark Knight"),
    ("frozn", "Frozen"), ("greys anatomy", "Grey"),
]

# Alternative weight settings to compare (applied by monkeypatching rank).
VARIANTS = {
    "current": {},
    "no_people_signal": {"_people": False},
    "bonus 1/doubling cap 3": {"PERSON_BONUS_PER_DOUBLING": 1, "PERSON_BONUS_MAX": 3},
    "bonus 1/doubling cap 4": {"PERSON_BONUS_PER_DOUBLING": 1, "PERSON_BONUS_MAX": 4},
    "bonus 2/doubling cap 6": {"PERSON_BONUS_PER_DOUBLING": 2, "PERSON_BONUS_MAX": 6},
    "C: within, 1/dbl cap 4": {"PERSON_BONUS_MODE": "within", "PERSON_BONUS_PER_DOUBLING": 1, "PERSON_BONUS_MAX": 4},
    "C: within, 2/dbl cap 6": {"PERSON_BONUS_MODE": "within", "PERSON_BONUS_PER_DOUBLING": 2, "PERSON_BONUS_MAX": 6},
    "A + exact-phrase cap 5": {"EXACT_PHRASE_PENALTY_CAP": 5},
    "C(1/4) + exact-phrase 5": {"PERSON_BONUS_MODE": "within", "PERSON_BONUS_PER_DOUBLING": 1,
                                "PERSON_BONUS_MAX": 4, "EXACT_PHRASE_PENALTY_CAP": 5},
    "C(1/4) + exact-phrase 0": {"PERSON_BONUS_MODE": "within", "PERSON_BONUS_PER_DOUBLING": 1,
                                "PERSON_BONUS_MAX": 4, "EXACT_PHRASE_PENALTY_CAP": 0},
    "C(1/4)+phrase5+tag30": {"PERSON_BONUS_MODE": "within", "PERSON_BONUS_PER_DOUBLING": 1,
                             "PERSON_BONUS_MAX": 4, "EXACT_PHRASE_PENALTY_CAP": 5,
                             "TYPE_PENALTY": {**rank.TYPE_PENALTY, "Tag": 30}},
    "no repeat penalty": {"REPEAT_STEP": {}},
    "repeat step 3": {"REPEAT_STEP": {k: 3 for k in rank.REPEAT_STEP}},
}


TEST_HDR = {"X-Searchfix-Test": "1"}   # keeps eval traffic out of the search log


def _open(url, timeout=60):
    return urllib.request.urlopen(urllib.request.Request(url, headers=TEST_HDR), timeout=timeout)


def get(base, path, params, timeout=60):
    q = urllib.parse.urlencode({"api_key": KEY, **params})
    with _open(f"{base}/emby{path}?{q}", timeout=timeout) as r:
        return json.load(r)


def items(base, params):
    return get(base, f"/Users/{UID}/Items", params)["Items"]


def strip_article(s):
    w = s.split()
    return " ".join(w[1:]) if len(w) > 1 and w[0].lower() in ("the", "a", "an") else s


def make_targets(n, seed):
    rnd = random.Random(seed)
    base = {"Recursive": "true", "Fields": "SortName", "EnableImages": "false", "EnableUserData": "false"}
    movies = items(EMBY, {**base, "IncludeItemTypes": "Movie", "Fields": "SortName,People,Studios"})
    shows = items(EMBY, {**base, "IncludeItemTypes": "Series"})
    colls = items(EMBY, {**base, "IncludeItemTypes": "BoxSet"})
    people, studios = {}, {}
    for m in movies:
        for p in (m.get("People") or [])[:3]:          # top billing: people folks search for
            if p.get("Id") and p.get("Type") == "Actor":
                people[p["Id"]] = {"Id": p["Id"], "Name": p["Name"], "Type": "Person"}
        for s in m.get("Studios") or []:
            if s.get("Id"):
                studios[s["Id"]] = {"Id": s["Id"], "Name": s["Name"], "Type": "Studio"}
    share = {"Movie": 0.4, "Series": 0.2, "BoxSet": 0.1, "Person": 0.2, "Studio": 0.1}
    pools = {"Movie": movies, "Series": shows, "BoxSet": colls,
             "Person": list(people.values()), "Studio": list(studios.values())}
    out = []
    for t, frac in share.items():
        pool = pools[t]
        out += rnd.sample(pool, min(len(pool), max(1, int(n * frac))))
    return out


def queries_for(target):
    name = target["Name"].strip()
    bare = strip_article(name)
    words = rank.norm(bare).split()
    qs = {"full": name}
    if bare != name:
        qs["no_article"] = bare
    if len(words) >= 3:
        qs["two_words"] = " ".join(words[:2])
    if len(words) >= 2 and len(words[0]) >= 3:
        qs["first_word"] = words[0]
    if len(rank.norm(bare).replace(" ", "")) > 5:
        qs["prefix4"] = rank.norm(bare)[:4].strip()
    return qs


SPLIT_POOL = True
MEDIA_TYPES = "Series,Movie,BoxSet"


def fetch_pool(q):
    """Same candidate pool searchfix builds (split: titles + everything)."""
    p = {"SearchTerm": q, "Recursive": "true", "Limit": str(POOL),
         "Fields": "OriginalTitle,SortName", "GroupProgramsBySeries": "true"}
    rest = items(EMBY, p)
    if not SPLIT_POOL:
        return rest
    media = items(EMBY, {**p, "IncludeItemTypes": MEDIA_TYPES})
    seen = {i["Id"] for i in media}
    return media + [i for i in rest if i["Id"] not in seen]


def fetch_stock(q):
    return items(EMBY, {"SearchTerm": q, "Recursive": "true", "Limit": str(TOP),
                        "Fields": "OriginalTitle,SortName", "GroupProgramsBySeries": "true"})


def load_people():
    try:
        with open(SNAPSHOT) as f:
            return json.load(f)["counts"]
    except OSError:
        return None


def apply_variant(v, defaults):
    for k, val in defaults.items():
        setattr(rank, k, val)
    for k, val in v.items():
        if not k.startswith("_"):
            setattr(rank, k, val)


def rank_of(lst, tid):
    for n, i in enumerate(lst[:TOP]):
        if str(i.get("Id")) == str(tid):  # studio Ids are ints on movies, strings in search
            return n + 1
    return None


def summarize(rows, key):
    """rows: list of dicts with key -> rank or None"""
    n = len(rows)
    r = [x[key] for x in rows]
    h = lambda k: sum(1 for x in r if x and x <= k) / n * 100
    mrr = sum(1 / x for x in r if x) / n
    return f"@1 {h(1):5.1f}%  @3 {h(3):5.1f}%  @10 {h(10):5.1f}%  MRR {mrr:.3f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=400, help="number of sampled targets")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--variants", action="store_true")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--worst", type=int, default=25)
    ap.add_argument("--single-pool", action="store_true", help="old behaviour: one mixed pool")
    args = ap.parse_args()

    global SPLIT_POOL
    SPLIT_POOL = not args.single_pool
    t0 = time.time()
    counts = load_people()
    targets = make_targets(args.n, args.seed)
    work = [(t, style, q) for t in targets for style, q in queries_for(t).items()]
    uniq = sorted({q for _, _, q in work} | {c[0] for c in CASES})
    with ThreadPoolExecutor(8) as ex:
        pools = dict(zip(uniq, ex.map(fetch_pool, uniq)))
        stock = dict(zip(uniq, ex.map(fetch_stock, uniq)))
    print(f"{len(targets)} targets, {len(work)} queries ({len(uniq)} unique), "
          f"people table: {len(counts) if counts else 'none'}, fetched in {time.time() - t0:.0f}s\n")

    defaults = {k: getattr(rank, k) for k in
                ("PERSON_BONUS_PER_DOUBLING", "PERSON_BONUS_MAX", "REPEAT_STEP", "FREE_REPEATS",
                 "PARTIAL_WORD_PENALTY", "TYPE_PENALTY", "PERSON_BONUS_MODE", "EXACT_PHRASE_PENALTY_CAP")}
    variants = VARIANTS if args.variants else {"current": {}}
    results = {}
    for vname, v in variants.items():
        apply_variant(v, defaults)
        lookup = None if (v.get("_people") is False or counts is None) else counts.get
        ranked = {q: rank.rerank(p, q, lookup) for q, p in pools.items()}
        rows = []
        for t, style, q in work:
            rows.append({"type": t["Type"], "style": style, "q": q, "target": t["Name"],
                         "stock": rank_of(stock[q], t["Id"]), "new": rank_of(ranked[q], t["Id"])})
        case_res = []
        for q, desc, pred in CASES:
            try:
                ok = bool(ranked[q]) and pred(ranked[q])
            except Exception:
                ok = False
            case_res.append((q, desc, ok))
        results[vname] = (rows, case_res, ranked)
    apply_variant({}, defaults)

    rows, case_res, ranked = results["current"]
    print("=== Known-item search: where the target lands in Top Results (first 50)")
    print(f"  {'':22} {'stock Emby':55} searchfix")
    print(f"  {'ALL':22} {summarize(rows, 'stock'):55} {summarize(rows, 'new')}")
    by = defaultdict(list)
    for r in rows:
        by["style " + r["style"]].append(r)
        by["type " + r["type"]].append(r)
    for k in sorted(by):
        print(f"  {k + f' ({len(by[k])})':22} {summarize(by[k], 'stock'):55} {summarize(by[k], 'new')}")

    worse = [r for r in rows if (r["new"] or 999) > (r["stock"] or 999)]
    better = [r for r in rows if (r["new"] or 999) < (r["stock"] or 999)]
    print(f"\n  improved {len(better)}, same {len(rows) - len(worse) - len(better)}, worse {len(worse)}")
    print(f"\n=== Worst regressions (target ranked lower than stock Emby), up to {args.worst}")
    worse.sort(key=lambda r: ((r["new"] or 999) - (r["stock"] or 999)), reverse=True)
    for r in worse[:args.worst]:
        top = ranked[r["q"]][:3]
        print(f"  [{r['type']:7} {r['style']:10}] q={r['q']!r:28} target={r['target'][:30]!r:32} "
              f"stock #{r['stock']} -> #{r['new'] or '>50'}   now top: "
              + " | ".join(f"{i['Type'][:4]}:{i['Name'][:22]}" for i in top))

    print("\n=== Expectation checks")
    for q, desc, ok in case_res:
        print(f"  {'PASS' if ok else 'FAIL'}  {q!r:22} {desc}")
    print(f"  {sum(ok for *_, ok in case_res)}/{len(case_res)} pass")

    if args.variants:
        print("\n=== Weight variants (same queries)")
        for vname, (vrows, vcases, _) in results.items():
            print(f"  {vname:26} {summarize(vrows, 'new')}   checks {sum(ok for *_, ok in vcases)}/{len(vcases)}"
                  f"   failing: {', '.join(d for _, d, ok in vcases if not ok)[:110]}")

    if args.live:
        sample = uniq[:: max(1, len(uniq) // 40)]
        mism = fz = 0
        for q in sample:
            qs = urllib.parse.urlencode({"api_key": KEY, "SearchTerm": q, "Recursive": "true",
                                         "Limit": str(TOP), "GroupProgramsBySeries": "true"})
            with _open(f"{PROXY}/emby/Users/{UID}/Items?{qs}") as r:
                if "fuzzy" in (r.headers.get("X-Searchfix") or ""):
                    fz += 1          # typo matches added in front: not modelled by rank.py
                    continue
                live = json.load(r)["Items"]
            if [i["Id"] for i in live] != [i["Id"] for i in ranked[q][:TOP]]:
                mism += 1
        n = len(sample) - fz
        print(f"\n=== Live proxy vs rank.py: {n - mism}/{n} queries identical ({fz} used fuzzy, skipped)")
        print("\n=== Typo checks (live proxy, first result)")
        ok = 0
        for q, want in TYPOS:
            qs = urllib.parse.urlencode({"api_key": KEY, "SearchTerm": q, "Recursive": "true", "Limit": "10"})
            with _open(f"{PROXY}/emby/Users/{UID}/Items?{qs}") as r:
                live = json.load(r)["Items"]
            hit = bool(live) and want.lower() in live[0]["Name"].lower()
            ok += hit
            print(f"  {'PASS' if hit else 'FAIL'}  {q!r:22} -> {live[0]['Name'] if live else '(nothing)'}")
        print(f"  {ok}/{len(TYPOS)} typos fixed")


if __name__ == "__main__":
    main()
