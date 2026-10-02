"""Query expansion for rare misspelled words ("hary" -> also search "harry").

Complements fuzzy.py. fuzzy.py handles queries where *nothing* matches the
whole query; this handles queries where a few obscure things do match (three
people named "Hary…"), so the user never sees the hundreds of "Harry" results
they meant.

Safety rules (a plain corrector "fixed" 92 of 1,101 real queries, e.g.
"Rick and Morty" -> "rick and marty", 2026-10-01):
  1. Never replace the query: also search the corrected term and rank both
     together, with matches only to the corrected term one tier lower
     (rank.ALT_TERM_PENALTY). A real match for what was typed always wins.
  2. Only expand when the original query matches at most FEW titles/people as
     a whole (decided once per term and cached; a search page fires several
     requests for the same term).
     ...and never when any title/person name IS the query or starts with it
     as a whole word ("Diamonds Are Forever", "Armie Hammer"): the user found
     what they typed.
  3. A word is only corrected if it is rare in the library (< MIN_FREQ names),
     and is either a real library word ("hary": Sébastien Hary) or not the
     start of any library word. A non-word that starts a longer word ("skan"
     -> Skandar, "wari" -> Waris) is someone still typing. The replacement has
     the same first letter, is at most 2 letters longer/shorter, has
     similarity >= MIN_RATIO and appears at least FACTOR x more often.
"""
import difflib
import threading
import time
from collections import Counter, defaultdict

import rank

MIN_LEN = 4
MIN_FREQ = 10
FACTOR = 10
MIN_RATIO = 0.8
FEW = 5
CACHE_SECONDS = 60


class Corrector:
    def __init__(self, names):
        """names: iterable of (id, type, name) as in LibraryIndex.names."""
        self.freq = Counter()
        for _, _, name in names:
            self.freq.update(set(rank.norm(name).split()))
        self.by_first = defaultdict(list)
        for w, f in self.freq.items():
            if len(w) >= 3:
                self.by_first[w[0]].append((w, f))

    def correct_word(self, w):
        f = self.freq.get(w, 0)
        if len(w) < MIN_LEN or f >= MIN_FREQ or not w.isalpha():
            return None
        bucket = self.by_first.get(w[0], ())
        if f == 0 and any(x != w and x.startswith(w) for x, _ in bucket):
            return None  # still typing: "harr" -> harry, "skan" -> skandar
        need = max(MIN_FREQ, FACTOR * max(f, 1))
        best = None
        for x, fx in bucket:
            if fx < need or abs(len(x) - len(w)) > 2:
                continue
            r = difflib.SequenceMatcher(None, w, x).ratio()
            if r >= MIN_RATIO and (best is None or (r, fx) > best[1:]):
                best = (x, r, fx)
        return best[0] if best else None

    def correct(self, term):
        """Corrected query string, or None if no word needed correcting."""
        words = rank.norm(term).split()
        out, changed = [], False
        for w in words:
            c = self.correct_word(w)
            out.append(c or w)
            changed |= c is not None
        return " ".join(out) if changed else None


def whole_matches(items, term):
    """(how many titles/people/collections/studios match the whole term,
        whether any of them is a strong match: name == term or starts with it
        as whole words)."""
    q = rank.norm(term)
    q_bare = rank._ARTICLE.sub("", q)
    n, strong = 0, False
    for it in items:
        if it.get("Type") not in ("Series", "Movie", "BoxSet", "Person", "Studio"):
            continue
        best = None
        for field in ("Name", "OriginalTitle", "SortName"):
            name = rank.norm(it.get(field))
            for t in (rank.match_tier(name, q), rank.match_tier(name, q_bare)):
                if t is not None and (best is None or t < best):
                    best = t
        if best is not None:
            n += 1
            strong |= best <= rank.PREFIX
    return n, strong


class Expander:
    """Decides, per term, whether to also search a corrected term."""

    def __init__(self, corrector):
        self.corrector = corrector
        self.cache = {}  # (user key, term) -> (time, corrected or None)
        self.lock = threading.Lock()

    def decide(self, term, fetch_mixed, key=""):
        """Corrected term to add, or None.

        fetch_mixed(): returns Emby's mixed result list for the ORIGINAL term
        (only called when a correction exists, at most once per CACHE_SECONDS).
        """
        k = (key, rank.norm(term))
        now = time.time()
        with self.lock:
            hit = self.cache.get(k)
            if hit and now - hit[0] < CACHE_SECONDS:
                return hit[1]
        corrected = self.corrector.correct(term)
        if corrected is not None:
            try:
                n, strong = whole_matches(fetch_mixed(), term)
                if strong or n > FEW:
                    corrected = None
            except Exception:
                corrected = None
        with self.lock:
            if len(self.cache) > 2000:
                self.cache.clear()
            self.cache[k] = (now, corrected)
        return corrected
