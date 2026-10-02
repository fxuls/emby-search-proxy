"""Search log: who searched for what, when, from which app and device.

One row per *search*, not per request. A search page fires several requests
(rows, top results), and apps that search as you type send "h", "ha", "har"…
Requests from the same device are folded into the previous row while the term
keeps extending or shrinking (typing or backspacing) within FOLD_SECONDS, so
the row ends up holding the final term.

Writes go through a queue to a background thread; a search never waits on
disk. The database lives in /data (bind-mounted from the host) so the
Telegram bot can read it directly. Rows older than RETENTION_DAYS are pruned
daily.
"""
import json
import queue
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.request

FOLD_SECONDS = 20
RETENTION_DAYS = 365

SCHEMA = """
CREATE TABLE IF NOT EXISTS searches (
    id          INTEGER PRIMARY KEY,
    ts          REAL NOT NULL,      -- unix time of the latest request in this search
    first_ts    REAL NOT NULL,      -- unix time of the first keystroke/request
    user_id     TEXT,
    user_name   TEXT,
    client      TEXT,               -- app, e.g. "Emby Web", "AndroidTv"
    app_version TEXT,
    device      TEXT,               -- device name, e.g. "Chrome Windows"
    device_id   TEXT,
    ip          TEXT,
    term        TEXT NOT NULL,      -- final search text
    results     INTEGER,            -- candidates Emby returned (mixed list)
    top_type    TEXT,
    top_name    TEXT,               -- what was shown first
    fuzzy       INTEGER DEFAULT 0,  -- 1 if typo-tolerant matches were added
    corrected   TEXT,               -- term also searched for a rare misspelled word ("harry" for "hary")
    requests    INTEGER DEFAULT 1,  -- how many requests were folded into this row
    emby_ms     INTEGER
);
CREATE INDEX IF NOT EXISTS searches_ts ON searches(ts);
"""

_AUTH_FIELD = re.compile(r'(\w+)="([^"]*)"')


def parse_auth(headers, query_params):
    """Client/Device/DeviceId/Version from the Emby auth header or query string."""
    out = {}
    for h in ("X-Emby-Authorization", "Authorization"):
        v = headers.get(h)
        if v:
            out.update({k.lower(): val for k, val in _AUTH_FIELD.findall(v)})
            break
    for k, v in query_params.items():  # web client sends these as query args
        kl = k.lower()
        if kl.startswith("x-emby-"):
            out.setdefault(kl[len("x-emby-"):].replace("-", ""), v)
    return {
        "client": out.get("client"),
        "device": out.get("device") or out.get("devicename"),
        "device_id": out.get("deviceid"),
        "app_version": out.get("version") or out.get("clientversion"),
    }


def user_id_from(path, params):
    m = re.search(r"/Users/([0-9a-fA-F]{32})/", path)
    if m:
        return m.group(1).lower()
    uid = params.get("userid")
    return uid.lower() if uid else None


class UserNames:
    """Emby user id -> name, refreshed at most every 10 minutes."""

    def __init__(self, upstream, api_key):
        self.upstream, self.api_key = upstream.rstrip("/"), api_key
        self.names, self.fetched = {}, 0.0
        self.lock = threading.Lock()

    def get(self, uid):
        if not uid or not self.api_key:
            return None
        with self.lock:
            if uid not in self.names and time.time() - self.fetched > 600:
                try:
                    q = urllib.parse.urlencode({"api_key": self.api_key})
                    with urllib.request.urlopen(f"{self.upstream}/emby/Users?{q}", timeout=10) as r:
                        self.names = {u["Id"].lower(): u["Name"] for u in json.load(r)}
                except Exception:
                    pass
                self.fetched = time.time()
            return self.names.get(uid)


class SearchLog:
    def __init__(self, path, user_names=None, log=print):
        self.path = path
        self.user_names = user_names
        self.log = log
        self.q = queue.Queue(maxsize=10000)
        self.last = {}  # fold key -> (row id, term, ts)

    def start(self):
        threading.Thread(target=self._run, name="searchlog", daemon=True).start()
        return self

    def record(self, **event):
        """Queue one request's event. Never blocks, never raises."""
        try:
            event.setdefault("ts", time.time())
            self.q.put_nowait(event)
        except Exception:
            pass

    # ---- writer thread ---------------------------------------------------
    def _run(self):
        db = sqlite3.connect(self.path)
        db.executescript(SCHEMA)
        cols = {r[1] for r in db.execute("PRAGMA table_info(searches)")}
        if "corrected" not in cols:  # added in 1.7.0
            db.execute("ALTER TABLE searches ADD COLUMN corrected TEXT")
        db.execute("PRAGMA journal_mode=WAL")  # readers (the bot) never block the writer
        last_prune = 0.0
        while True:
            ev = self.q.get()
            try:
                self._write(db, ev)
                if time.time() - last_prune > 86400:
                    db.execute("DELETE FROM searches WHERE ts < ?", (time.time() - RETENTION_DAYS * 86400,))
                    db.commit()
                    last_prune = time.time()
            except Exception as e:
                self.log(f"searchlog: write failed ({e!r})")

    @staticmethod
    def _folds(old, new):
        o, n = old.lower(), new.lower()
        return o == n or n.startswith(o) or o.startswith(n)

    def _write(self, db, ev):
        name = self.user_names.get(ev.get("user_id")) if self.user_names else None
        key = (ev.get("device_id") or ev.get("ip"), ev.get("user_id"))
        prev = self.last.get(key)
        has_result = ev.get("results") is not None
        if prev and ev["ts"] - prev[2] <= FOLD_SECONDS and self._folds(prev[1], ev["term"]):
            row_id = prev[0]
            sets = ["ts=?", "term=?", "requests=requests+1"]
            vals = [ev["ts"], ev["term"]]
            if has_result:  # the mixed-list request carries the interesting fields
                sets += ["results=?", "top_type=?", "top_name=?", "fuzzy=?", "emby_ms=?", "corrected=?"]
                vals += [ev["results"], ev.get("top_type"), ev.get("top_name"),
                         int(bool(ev.get("fuzzy"))), ev.get("emby_ms"), ev.get("corrected")]
            db.execute(f"UPDATE searches SET {', '.join(sets)} WHERE id=?", (*vals, row_id))
        else:
            cur = db.execute(
                "INSERT INTO searches (ts, first_ts, user_id, user_name, client, app_version, device,"
                " device_id, ip, term, results, top_type, top_name, fuzzy, emby_ms, corrected)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ev["ts"], ev["ts"], ev.get("user_id"), name, ev.get("client"), ev.get("app_version"),
                 ev.get("device"), ev.get("device_id"), ev.get("ip"), ev["term"], ev.get("results"),
                 ev.get("top_type"), ev.get("top_name"), int(bool(ev.get("fuzzy"))), ev.get("emby_ms"),
                 ev.get("corrected")))
            row_id = cur.lastrowid
        db.commit()
        self.last[key] = (row_id, ev["term"], ev["ts"])
