"""How many titles in the library each person is credited in.

Built in a background thread from three Emby queries (movies, series and
episodes, each with Fields=People) and rebuilt every REFRESH_HOURS. An episode
credit counts toward its series, so ten guest spots on one show = 1 title.
Keyed by Emby person Id, which is what search results carry.

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

TYPES = ("Movie", "Series", "Episode")


class PeopleCounts:
    def __init__(self, upstream, api_key, snapshot, refresh_hours=6.0, log=print):
        self.upstream = upstream.rstrip("/")
        self.api_key = api_key
        self.snapshot = snapshot
        self.refresh_s = refresh_hours * 3600
        self.log = log
        self._counts = None
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
            "IncludeItemTypes": item_type, "Fields": "People",
            "EnableImages": "false", "EnableUserData": "false",
        })
        with urllib.request.urlopen(f"{self.upstream}/emby/Items?{q}", timeout=300) as r:
            return json.load(r)["Items"]

    def build(self):
        t0 = time.monotonic()
        titles = defaultdict(set)
        for item_type in TYPES:
            for item in self._fetch(item_type):
                title_id = item.get("SeriesId") if item_type == "Episode" else item["Id"]
                title_id = title_id or item["Id"]
                for p in item.get("People") or []:
                    if p.get("Id"):
                        titles[p["Id"]].add(title_id)
        counts = {pid: len(s) for pid, s in titles.items()}
        self._counts = counts
        self.built_at = time.time()
        self.log(f"people: counted titles for {len(counts)} people in {time.monotonic() - t0:.1f}s")
        self._save_snapshot()

    def _save_snapshot(self):
        if not self.snapshot:
            return
        tmp = self.snapshot + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump({"built_at": self.built_at, "counts": self._counts}, f, separators=(",", ":"))
            os.replace(tmp, self.snapshot)
        except OSError as e:
            self.log(f"people: snapshot not saved ({e!r})")

    def _load_snapshot(self):
        try:
            with open(self.snapshot) as f:
                d = json.load(f)
            self._counts, self.built_at = d["counts"], d["built_at"]
            age_h = (time.time() - self.built_at) / 3600
            self.log(f"people: loaded snapshot of {len(self._counts)} people ({age_h:.1f}h old)")
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
                self.log(f"people: refresh failed ({e!r}); retrying in 10 min")
                time.sleep(600)

    def start(self):
        threading.Thread(target=self._loop, name="people-counts", daemon=True).start()
        return self
