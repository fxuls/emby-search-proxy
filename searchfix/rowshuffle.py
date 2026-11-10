"""Daily rating jitter for home rows sorted by rating.

A home row whose section is sorted by CommunityRating shows the same top titles
in the same order every day. Most people never scroll past the first page
(12 cards on most apps), so the rest of the row is never seen. This module
gives such rows a different order each day while higher-rated titles still lean
toward the front:

    score = CommunityRating + SIGMA * z

z is a standard-normal number derived from a hash of (row, day, item id). So:
  * the order is the same for everyone, all day (each page a slice of one list);
  * each title's nudge doesn't depend on the other titles, so a title added
    mid-day doesn't reshuffle the rest of the row;
  * SIGMA is in rating points: with 0.4 a title regularly beats one rated
    ~0.4 higher, rarely ~0.8 higher, practically never 1.5 higher.
  * SIGMA 0 gives exactly Emby's own order (the sort is stable).

"Row" = the section's query (its TagIds/GenreIds/StudioIds), not its id. Each
user has their own copy of a section with its own id, and every copy of a row
gets the same order.

The day starts at ROLLOVER_HOUR local time (container TZ), not at midnight, so
a row doesn't change in the middle of an evening.

Pure functions only; the HTTP side lives in server.py (handle_row_shuffle).
"""
import datetime
import hashlib
import math
import threading
import time


def norm_sort(sortby):
    return (sortby or "").replace(" ", "").lower()


def row_key(section):
    """A stable name for what a section shows, shared by every user's copy of it."""
    q = section.get("Query") or {}
    parts = []
    for k in ("TagIds", "GenreIds", "StudioIds"):
        v = q.get(k) or []
        if v:
            parts.append(k + "=" + ",".join(sorted(str(x) for x in v)))
    return ";".join(parts)


def eligible(section, sortbys):
    """True if this home section is a rating-sorted row we should reorder.

    sortbys: set of normalised SortBy values that qualify (e.g. {"communityrating,sortname"}).
    """
    if not section or section.get("SectionType") != "items":
        return False
    if norm_sort(section.get("SortBy")) not in sortbys:
        return False
    if (section.get("SortOrder") or "").lower() != "descending":
        return False
    return bool(row_key(section))


def day_key(now=None, rollover_hour=4):
    """The local date a moment belongs to, with days starting at rollover_hour."""
    now = now or datetime.datetime.now()
    return (now - datetime.timedelta(hours=rollover_hour)).date().isoformat()


def z(row, day, item_id):
    """Standard-normal number, fixed for (row, day, item) (Box-Muller on a sha256)."""
    h = hashlib.sha256(f"{row}|{day}|{item_id}".encode()).digest()
    u1 = (int.from_bytes(h[:8], "big") + 1) / (2 ** 64 + 1)   # (0, 1]: log() is safe
    u2 = int.from_bytes(h[8:16], "big") / 2 ** 64
    return math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)


def order(members, sigma, row, day):
    """members: [(item id, rating or None)] in Emby's order. Returns ids, reordered.

    A missing rating counts as 0, so unrated titles stay at the back (where Emby
    puts them too). sorted() is stable, so sigma 0 returns Emby's order unchanged.
    """
    if sigma <= 0:
        return [i for i, _ in members]
    scored = [(-((r or 0.0) + sigma * z(row, day, i)), i) for i, r in members]
    return [i for _, i in sorted(scored, key=lambda t: t[0])]


def page(ids, start, limit):
    """ids[start:start+limit]; limit 0 = everything from start."""
    return ids[start:start + limit] if limit else ids[start:]


class TTLCache:
    """Tiny thread-safe {key: value} cache with a fixed time-to-live."""

    def __init__(self, ttl, max_entries=5000):
        self.ttl, self.max = ttl, max_entries
        self._d = {}
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            hit = self._d.get(key)
            if hit and time.monotonic() - hit[0] < self.ttl:
                return hit[1]
            return None

    def put(self, key, value):
        with self._lock:
            if len(self._d) >= self.max:
                self._d.clear()   # crude, but entries are cheap to rebuild
            self._d[key] = (time.monotonic(), value)

    def clear(self):
        with self._lock:
            self._d.clear()
