"""Background library index: per-person title counts + names for fuzzy search.

Built in a background thread from four Emby queries (movies, series and
episodes with Fields=People, plus collections) and rebuilt every
REFRESH_HOURS. It provides:
  * person title counts: an episode credit counts toward its series, so ten
    guest spots on one show = 1 title. Keyed by Emby person Id, which is what
    search results carry.
  * names: (Id, Type, Name) for every movie, series, collection and credited
    person, which fuzzy.py indexes for typo-tolerant search.

The last good table is snapshotted to SNAPSHOT so a restart has counts
immediately. Until a table exists, lookups return None and ranking simply
skips the signal; a search never waits on this.
"""
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from collections import defaultdict

TYPES = ("Movie", "Series", "Episode", "BoxSet")


class LibraryIndex:
    def __init__(self, upstream, api_key, snapshot, refresh_hours=6.0, log=print):
        self.upstream = upstream.rstrip("/")
        self.api_key = api_key
        self.snapshot = snapshot
        self.refresh_s = refresh_hours * 3600
        self.log = log
        self._counts = None
        self.names = []        # [(id, type, name)] movies, series, collections, people
        self.on_build = []     # callbacks run after each (re)build / snapshot load
        self.built_at = None
        self._load_snapshot()

    # ---- lookups --------------------------------------------------------
    def get(self, person_id):
        counts = self._counts
        if counts is None:
            return None
        return counts.get(person_id, 0)

    @property
    def ready(self):
        return self._counts is not None

    def __len__(self):
        return len(self._counts or {})

    # ---- building -------------------------------------------------------
    def _fetch(self, item_type):
        q = urllib.parse.urlencode({
            "api_key": self.api_key, "Recursive": "true",
            "IncludeItemTypes": item_type, "Fields": "People" if item_type != "BoxSet" else "",
            "EnableImages": "false", "EnableUserData": "false",
        })
        with urllib.request.urlopen(f"{self.upstream}/emby/Items?{q}", timeout=300) as r:
            return json.load(r)["Items"]

    def build(self):
        t0 = time.monotonic()
        titles = defaultdict(set)
        names, person_names = [], {}
        for item_type in TYPES:
            for item in self._fetch(item_type):
                if item_type != "Episode":
                    names.append((item["Id"], item_type, item.get("Name") or ""))
                title_id = item.get("SeriesId") if item_type == "Episode" else item["Id"]
                title_id = title_id or item["Id"]
                for p in item.get("People") or []:
                    if p.get("Id"):
                        titles[p["Id"]].add(title_id)
                        person_names[p["Id"]] = p.get("Name") or ""
        names += [(pid, "Person", n) for pid, n in person_names.items()]
        counts = {pid: len(s) for pid, s in titles.items()}
        self._counts, self.names = counts, names
        self.built_at = time.time()
        self.log(f"library: {len(counts)} people with title counts, {len(names)} names, "
                 f"built in {time.monotonic() - t0:.1f}s")
        self._save_snapshot()
        self._notify()

    def _notify(self):
        for fn in self.on_build:
            try:
                fn(self)
            except Exception as e:
                self.log(f"library: on_build callback failed ({e!r})")

    def _save_snapshot(self):
        if not self.snapshot:
            return
        tmp = self.snapshot + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump({"built_at": self.built_at, "counts": self._counts, "names": self.names},
                          f, separators=(",", ":"))
            os.replace(tmp, self.snapshot)
        except OSError as e:
            self.log(f"library: snapshot not saved ({e!r})")

    def _load_snapshot(self):
        try:
            with open(self.snapshot) as f:
                d = json.load(f)
            self._counts, self.built_at = d["counts"], d["built_at"]
            self.names = [tuple(n) for n in d.get("names", [])]
            if not self.names:          # snapshot from before names existed: rebuild soon
                self.built_at = 0
            age_h = (time.time() - (self.built_at or time.time())) / 3600
            self.log(f"library: loaded snapshot, {len(self._counts)} people, "
                     f"{len(self.names)} names ({age_h:.1f}h old)")
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def _loop(self):
        if self.built_at:  # snapshot loaded: refresh when it's due
            wait = self.refresh_s - (time.time() - self.built_at)
            if wait > 0:
                time.sleep(wait)
        while True:
            try:
                self.build()
                time.sleep(self.refresh_s)
            except Exception as e:  # Emby down, timeout...: keep old table, retry soon
                self.log(f"library: refresh failed ({e!r}); retrying in 10 min")
                time.sleep(600)

    def start(self):
        if self.names:
            self._notify()   # make the snapshot usable before the first rebuild
        threading.Thread(target=self._loop, name="library-index", daemon=True).start()
        return self
