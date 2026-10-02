# emby-search-proxy

Makes Emby's search put what you're looking for first. Searching "La La" should
show La La Land, not a row of studios, tags and actors called "La".

It sits in front of Emby on port 8096. nginx passes every request straight
through to Emby **except** searches (requests with a `SearchTerm`), which go to
a small Python service (`searchfix`) that re-orders Emby's own results.
Because it runs server-side, it works in every app: Android TV, iOS, Roku,
Samsung, LG, Xbox and the browser. The apps need no change.

```
 clients (LAN, and remote via <vpn-container> → socat relay)
                     │  :8096
               ┌─────▼─────┐   SearchTerm=…   ┌───────────┐
               │ emby-proxy├─────────────────►│ searchfix │
               │  (nginx)  │                  │ (python)  │
               └─────┬─────┘                  └─────┬─────┘
          everything │ else                         │ widened query
                     ▼                              ▼
               ┌──────────────────────────────────────────┐
               │           emby  (emby:8096, internal)    │
               └──────────────────────────────────────────┘
```

## What changes for users

| Search | Emby alone | With searchfix |
|---|---|---|
| "La La" | rows: Studio, Tag, Person, Series, Episode, **Movie** | rows: Series, **Movie**, Studio, Person, Episode, Tag |
| "The Office" | #1 is the *tag* "office" | #1 The Office (US), #2 Office Space |
| "harry" | Dirty Harry, then 7 actors named Harry | the Harry Potter films |
| "Inter" | Interstellar, then 4 tags | Interstellar, The Interview, The Intern, … |

Two things are re-ordered:

1. **The rows on the search page.** Apps draw one row per type in whatever order
   Emby's `/ItemTypes` returns, and that order is arbitrary. Rows now go TV shows,
   Movies, Collections, Genres, Studios, People, Episodes, Live TV, Tags.
2. **The results inside a list** (the "Top Results" row, the quick search in the
   side menu, each type row). An exact title match beats a match on the start
   of the title, which beats a match at the start of a later word, which beats
   a match partway through a word. Movies and shows beat collections, which
   beat episodes and people, which beat tags and studios.
   Full rules: [docs/design.md](docs/design.md).

Nothing is added or removed. Searches return the same items Emby would, in a
better order.

## Safety properties

- **Only searches are touched.** Streaming, images, logins, websockets and the
  rest of the API bypass the Python service entirely.
- **Fails open.** If searchfix is down, nginx sends the search straight to
  Emby, so users just get Emby's normal ordering. If re-ranking hits any
  error, Emby's response is served unmodified.
- **Respects real sort choices.** The default alphabetical sort that apps add
  to search rows is replaced by relevance. Any other sort (date added,
  rating, random) passes through unchanged. Paged results are slices of one
  ranking, so scrolling never duplicates or skips items.
- **Stateless.** No database and no config beyond environment variables. Stdlib-only Python.

## Layout

| Path | What |
|---|---|
| `searchfix/rank.py` | Ranking rules. Pure functions; this is the file to tune |
| `searchfix/server.py` | HTTP service: decides what to rewrite, talks to Emby |
| `searchfix/people.py` | Background table: library titles per person (people signal) |
| `.env` | `EMBY_API_KEY` for the people signal. **Not in git** |
| `searchfix/Dockerfile` | Image `emby-searchfix:local` |
| `nginx/default.conf` | Front door on :8096, routing, timeouts, fallback |
| `deploy/compose-services.yml` | The services as added to `~/docker/emby/docker-compose.yml` |
| `tests/` | Unit tests (ranking) and in-process server tests against a fake Emby |
| `scripts/smoke.py` | Live check against a running proxy (pass-through + search) |
| `scripts/cutover.sh` | Put the proxy in front of Emby / take it out (dry run by default, auto-rollback) |
| `docs/design.md` | Why a proxy, ranking rules, alternatives considered |
| `docs/runbook.md` | Deploy, cutover, rollback, tuning, troubleshooting |

## Quick reference

```bash
cd ~/homelab/emby-search-proxy
python3 -m unittest discover -s tests                            # offline tests
EMBY_API_KEY=<key> scripts/smoke.py http://localhost:8096        # live check
docker logs --tail 50 emby-searchfix                             # one line per re-ranked search
```

Deploying a change: [docs/runbook.md](docs/runbook.md#deploying-a-change).
