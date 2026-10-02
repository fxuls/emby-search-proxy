#!/usr/bin/env python3
"""Live smoke test against a running proxy.

    EMBY_API_KEY=... scripts/smoke.py [BASE_URL]      (default http://localhost:8096)

Checks that ordinary Emby traffic passes through untouched (API, images,
byte-range streaming, websocket upgrade, auth errors) and that search is
re-ranked. Exits non-zero on any failure. Read-only against Emby.
"""
import json
import os
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "searchfix"))
import rank  # noqa: E402  (the row order the live service should be using)

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8096").rstrip("/")
KEY = os.environ.get("EMBY_API_KEY")
if not KEY:
    sys.exit("set EMBY_API_KEY")

failures = 0


def check(name, ok, detail=""):
    global failures
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    failures += 0 if ok else 1


def get(path, params=None, headers=None, key=True):
    params = dict(params or {})
    if key:
        params["api_key"] = KEY
    url = f"{BASE}/emby{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"X-Searchfix-Test": "1", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


# --- pass-through -----------------------------------------------------------
s, _, b = get("/System/Info/Public", key=False)
check("public system info", s == 200)
users = json.loads(get("/Users")[2])
uid = os.environ.get("EMBY_USER_ID") or next(u["Id"] for u in users if u["Policy"].get("IsAdministrator"))

s, _, b = get(f"/Users/{uid}/Items", {"Recursive": "true", "IncludeItemTypes": "Movie",
                                      "Limit": "1", "Fields": "Path", "HasPrimaryImage": "true"})
movie = json.loads(b)["Items"][0]
check("plain items query untouched", s == 200)

s, h, b = get(f"/Items/{movie['Id']}/Images/Primary", {"maxWidth": "200"}, key=False)
check("image", s == 200 and h.get("Content-Type", "").startswith("image/"), f"{len(b)} bytes")

s, _, b = get(f"/Videos/{movie['Id']}/stream", {"static": "true"}, {"Range": "bytes=1000000-1999999"})
check("byte-range stream", s == 206 and len(b) == 1000000, f"status {s}, {len(b)} bytes")

s, _, _ = get(f"/Users/{uid}/Items", {"SearchTerm": "la"}, key=False)
check("search without auth rejected", s == 401, f"status {s}")

u = urllib.parse.urlsplit(BASE)
sock = socket.create_connection((u.hostname, u.port or 80), timeout=10)
sock.sendall((f"GET /embywebsocket?api_key={KEY}&deviceId=searchfix-smoke HTTP/1.1\r\n"
              f"Host: {u.netloc}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
              "Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n").encode())
line = sock.recv(200).split(b"\r\n")[0].decode(errors="replace")
sock.close()
check("websocket upgrade", " 101 " in line + " ", line)

# --- search -----------------------------------------------------------------
s, h, b = get("/ItemTypes", {"userId": uid, "SearchTerm": "La La", "Recursive": "true"})
rows = [t["Name"] for t in json.loads(b)["Items"]]
check("row order re-ranked", h.get("X-Searchfix") == "types"
      and rows == [t["Name"] for t in rank.order_types([{"Name": r} for r in rows])], ", ".join(rows))

s, h, b = get(f"/Users/{uid}/Items", {"SearchTerm": "harry", "Recursive": "true", "Limit": "16",
                                      "GroupProgramsBySeries": "true"})
items = json.loads(b)["Items"]
top = [f"{i['Type']}:{i['Name']}" for i in items[:4]]
check("top results re-ranked", h.get("X-Searchfix") == "items" and len(items) <= 16
      and items[0]["Type"] in ("Movie", "Series", "BoxSet"), "; ".join(top))

# Exactly what the web client's row loader sends for the Movies row.
s, h, b = get(f"/Users/{uid}/Items", {"SearchTerm": "harry", "IncludeItemTypes": "Movie", "Recursive": "true",
                                      "SortBy": "SortName", "SortOrder": "Ascending", "Limit": "16",
                                      "EnableTotalRecordCount": "false"})
items = json.loads(b)["Items"]
check("Movies row (default SortName) re-ranked", h.get("X-Searchfix") == "items"
      and items[0]["Name"].lower().startswith("harry"), "; ".join(i["Name"] for i in items[:3]))

s, h, b = get(f"/Users/{uid}/Items", {"SearchTerm": "harry", "Recursive": "true", "Limit": "5",
                                      "SortBy": "DateCreated"})
check("explicit non-name SortBy left alone", s == 200 and "X-Searchfix" not in h)

print(f"\n{failures} failure(s)")
sys.exit(1 if failures else 0)
