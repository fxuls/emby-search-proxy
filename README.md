# emby-search-proxy

Makes Emby's search put what you're looking for first. Searching "La La" should
show La La Land, not a row of studios, tags and actors called "La".

It sits in front of Emby on port 8096. nginx passes every request straight
through to Emby **except** searches (requests with a `SearchTerm`), which go to
a small Python service (`searchfix`) that re-orders Emby's own results.
Because it runs server-side, it works in every app: Android TV, iOS, Roku,
Samsung, LG, Xbox and the browser. The apps need no change.

```
 clients (LAN, and remote via an optional relay/VPN)
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

Searches return the same items Emby would, in a better order. The one
exception is **typos**: when nothing matches the whole query ("harry poter",
"interstelar"), the closest titles and people are added at the front; and
when a rare misspelled word barely matches anything ("Hary"), the common word
it was meant to be ("harry") is searched too.

Every search is also **logged** (who, when, app, device, term, top result) to
`~/docker/emby/searchfix-data/searches.db`. Read it from Telegram with
`/searches`.

### Home Screen Companion top-lists

The [Home Screen Companion](https://github.com/soderlund91/HomeScreenCompanion)
plugin builds ranked rows ("Top 10 this week") from hidden copies of each title,
with the rank number drawn on the copy's poster. Opening a card there opened the
copy, so the number showed on the detail page too, and an app holding an older
row got "item not found" once the plugin rebuilt its copies.

**Status (1.8.3): the home-row rewrite is OFF.** On real devices the Android TV
app cached the swapped item by Id and lost the rank numbers, and iOS home rows
failed to load. What's still active is the search swap and the hidden copy
version below. The original design, kept in `toplist.py`:

searchfix rewrote home rows (`/Users/{id}/Sections/{id}/Items`):

- **On a top-list row** each copy becomes the real title, but the card keeps
  the numbered poster: its image tag (`hsc<copyId>_<tag>`) is routed back to
  the copy's image. Tapping it opens the real title, with its own poster.
- **On any other row** (Continue Watching, ...) a copy is shown as the real title.
- **No second "Version".** The plugin merges each copy into its real title as an
  alternate version, so the detail page offered two versions of the same file.
  searchfix drops the copy from `MediaSources` in single-item requests and in
  `PlaybackInfo` (where Emby Web builds the Version list). nginx buffers
  PlaybackInfo bodies so a searchfix outage still falls back to Emby.
- **Search** shows the real title when Emby picked the copy.

The copy → title map is rebuilt every 10 minutes (`TOPLIST_REFRESH_MINUTES`) from
the libraries whose folder is under the plugin's directory. Needs `EMBY_API_KEY`;
`TOPLIST_ROWS=0` turns it off. Code: `searchfix/toplist.py`.

## Safety properties

- **Only searches, home rows, numbered top-list posters, single items and
  PlaybackInfo are touched** (the last two only to drop the plugin's copy version).
  Streaming, other images, logins, websockets and the rest of the API bypass
  the Python service entirely.
- **Fails open.** If searchfix is down, nginx sends the search straight to
  Emby, so users just get Emby's normal ordering. If re-ranking hits any
  error, Emby's response is served unmodified. The same goes for home rows; a
  numbered poster then falls back to the title's own poster (Emby ignores the
  unknown tag).
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
| `searchfix/library.py` | Background index: library titles per person (people signal) + all names (for fuzzy) |
| `searchfix/fuzzy.py` | Typo-tolerant lookup, used only when nothing matches the whole query |
| `searchfix/expand.py` | Query expansion for rare misspelled words (hary → also harry) |
| `searchfix/searchlog.py` | Search log writer (SQLite, one row per search) |
| `searchfix/toplist.py` | Home rows: Home Screen Companion top-list copies → real titles, numbered posters kept |
| `.env` | `EMBY_API_KEY` for the people signal. **Not in git** |
| `searchfix/Dockerfile` | Image `emby-searchfix:local` |
| `nginx/default.conf` | Front door on :8096, routing, timeouts, fallback |
| `deploy/compose-services.yml` | The services as added to `~/docker/emby/docker-compose.yml` |
| `tests/` | Unit tests (ranking) and in-process server tests against a fake Emby |
| `scripts/smoke.py` | Live check against a running proxy (pass-through + search) |
| `scripts/cutover.sh` | Put the proxy in front of Emby / take it out (dry run by default, auto-rollback) |
| `docs/design.md` | Why a proxy, ranking rules, alternatives considered |
| `docs/runbook.md` | Deploy, cutover, rollback, tuning, troubleshooting |

## Using it on your own Emby server

Built and tested against Emby 4.10 (web client, Android TV, iOS, Samsung, LG).
Paths in `deploy/` and `docs/` are from the author's server; adjust them.

1. Put `searchfix` and `emby-proxy` (nginx) in the same compose project as
   Emby, using `deploy/compose-services.yml` as the template. Move Emby's
   `8096:8096` port mapping to the `emby-proxy` service.
2. Optional (people ranking, typo handling): create an Emby API key and put
   `EMBY_API_KEY=<key>` in `.env` (git-ignored). Without it, re-ranking still works.
3. In Emby → Settings → Network, set *Read proxy headers to determine client IP
   addresses* to **Yes**, so Emby keeps seeing real client IPs.
4. Run `scripts/smoke.py` against the proxy. `scripts/cutover.sh` automates the
   switch and the rollback.

Python 3.12, standard library only; tests: `python3 -m unittest discover -s tests`.
MIT licensed.

## Quick reference

```bash
cd ~/homelab/emby-search-proxy
python3 -m unittest discover -s tests                            # offline tests
EMBY_API_KEY=<key> scripts/smoke.py http://localhost:8096        # live check
docker logs --tail 50 emby-searchfix                             # one line per re-ranked search
```

Deploying a change: [docs/runbook.md](docs/runbook.md#deploying-a-change).
