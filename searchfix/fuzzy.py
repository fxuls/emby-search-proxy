"""Typo-tolerant title/person lookup ("harry poter", "interstelar").

Used only when Emby's own results contain nothing whose name matches the whole
query (see server.py), so ordinary searches pay nothing.

Two stages:
  1. candidates: letter trigrams of the query (spaces removed) looked up in an
     inverted index; the CANDIDATES items sharing the most trigrams survive,
     ties broken by trigram-set similarity (names close in length to the query
     first) and then index order. Deterministic: an earlier version broke
     ties by Counter insertion order, which follows Python's per-process
     string-hash seed, so "matrx" found The Matrix in some processes only.
  2. score: each query word is compared with the most similar word of the
     name (difflib ratio), averaged over query words, with a small nudge
     toward shorter names. Joined words ("spiderman") are also compared with
     the whole name without spaces.

Measured on the library 2026-10-01: 72.5k names, index build ~1 s, ~70 MB;
lookups median 12–16 ms, worst ~30 ms; 30/30 typo test queries put the
intended title first, identically under different PYTHONHASHSEEDs.
"""
import difflib
import heapq
from collections import Counter, defaultdict

import rank

STOP = {"the", "a", "an", "of", "and"}
CANDIDATES = 150
COMMON_GRAM = 5000      # trigrams in more names than this carry no signal
MIN_SCORE = 0.82        # below this a "match" is more likely noise than a typo
TYPE_NUDGE = {"Series": 0.0, "Movie": 0.0, "BoxSet": 0.02, "Person": 0.04}


def _grams(s):
    s = f" {s} "
    return {s[i:i + 3] for i in range(len(s) - 2)}


def _word_score(qn, name):
    qwords = [w for w in qn.split() if w not in STOP] or qn.split()
    nwords = name.split()
    joined = name.replace(" ", "")
    total = 0.0
    for w in qwords:
        best = max((difflib.SequenceMatcher(None, w, x).ratio() for x in nwords), default=0.0)
        if len(w) > 5:  # "spiderman" vs "spider man"
            j = difflib.SequenceMatcher(None, w, joined).ratio()
            best = max(best, j * (0.95 if w in joined else 0.8))
        total += best
    extra_words = max(0, len([x for x in nwords if x not in STOP]) - len(qwords))
    return total / len(qwords) - 0.02 * extra_words


class FuzzyIndex:
    def __init__(self, names):
        """names: iterable of (id, type, name)."""
        self.items = []
        self.index = defaultdict(list)
        for iid, type_, name in names:
            n = rank.norm(name)
            if not n:
                continue
            k = len(self.items)
            # also index the name without a leading article: "matrx" should meet
            # "matrix", not be outvoted by names sharing its first letters
            grams = _grams(n.replace(" ", "")) | _grams(rank._ARTICLE.sub("", n).replace(" ", ""))
            self.items.append((iid, type_, name, n, len(grams)))
            for g in grams:
                self.index[g].append(k)

    def __len__(self):
        return len(self.items)

    def search(self, query, limit=8, min_score=MIN_SCORE):
        """[(score, id, type, name)] best first, score >= min_score."""
        qn = rank.norm(query)
        if len(qn.replace(" ", "")) < 4:
            return []
        qgrams = _grams(qn.replace(" ", ""))
        hits = Counter()
        for g in sorted(qgrams):
            lst = self.index.get(g, ())
            if len(lst) <= COMMON_GRAM:
                hits.update(lst)
        nq = len(qgrams)

        def key(kv):
            k, h = kv
            jaccard = h / (nq + self.items[k][4] - h)
            return (-h, -jaccard, k)

        scored = []
        for k, _ in heapq.nsmallest(CANDIDATES, hits.items(), key=key):
            iid, type_, name, n, _ = self.items[k]
            s = _word_score(qn, n) - TYPE_NUDGE.get(type_, 0.04)
            if s >= min_score:
                scored.append((s, iid, type_, name))
        scored.sort(key=lambda r: -r[0])
        return scored[:limit]


def has_whole_query_match(items, term):
    """True if any title/person/collection name matches the whole query."""
    q = rank.norm(term)
    q_bare = rank._ARTICLE.sub("", q)
    for it in items:
        if it.get("Type") not in ("Series", "Movie", "BoxSet", "Person"):
            continue
        for field in ("Name", "OriginalTitle", "SortName"):
            n = rank.norm(it.get(field))
            if rank.match_tier(n, q) is not None or rank.match_tier(n, q_bare) is not None:
                return True
    return False
