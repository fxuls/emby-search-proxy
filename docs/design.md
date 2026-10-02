# Design

## The problem (2026-09-30)

Searching "La La" in Emby did not surface La La Land on the first screen. The
search page is built by the client from two server calls:

- `GET /ItemTypes?SearchTerm=…`: which types have matches. The client draws
  **one row per type in the order returned**. That order is arbitrary. For
  "La La" it was Studio, Tag, Person, Series, Episode, Movie.
- `GET /Users/{id}/Items?SearchTerm=…&Limit=16`: the "Top Results" row,
  plus one call per row with `IncludeItemTypes=<type>`. Emby's own ranking
  mixes in TMDb keyword tags (≈3,100 of them), actors and studios, so for
  "harry" the top results were Dirty Harry and seven actors named Harry.

Every official app (Android TV, iOS, Android, Samsung, LG, Xbox, Windows,
Chromecast and the web) runs the same web-client code and makes these
calls. Roku is a native app and may search differently; see "Known limits".

## Options considered

| Option | Verdict |
|---|---|
| Emby setting | None exists. There is no search-weighting setting in 4.10. |
| Official Emby plugin | Not possible. The plugin API lets plugins *add* things (metadata providers, tasks, new endpoints, event handlers). It has no hook to change core endpoints' responses, and search ranking lives in Emby's own database query. |
| Harmony-patching plugin | Possible; this is how [StrmAssistant](https://github.com/OneManJS/StrmAssistant) changes search. It patches Emby internals at runtime. It can break on any Emby update, a bad patch can stop Emby from starting, and Emby staff explicitly oppose it: *"Ideally, intercepting a core route should not be possible"* ([forum thread](https://emby.media/community/topic/143814-search-replacement-plugin/)). Rejected. |
| Patch the web client (`dashboard-ui`) | Only reaches browser users, 4 of ~20. The apps bundle their own copy of the client. Rejected. |
| Remove TMDb keyword tags | Only partly helps (actors still flood results, row order unchanged). The curated home rows depend on tags. Rejected. |
| **Reverse proxy that re-ranks search responses** | **Chosen.** Works in every app, touches only search, and fails open. The cost is a hop in front of all traffic, mitigated below. |
| Official `IFullTextSearchProvider` | Suggested by Emby staff in the thread above; not shipped as of 2026-09. If it lands, a proper plugin could replace this. |

## Ranking rules (`searchfix/rank.py`)

Score = **match tier + type penalty**. Lower ranks higher. Ties keep Emby's order.

Match tier: the best match across `Name`, `OriginalTitle` and `SortName`,
after lowercasing and stripping accents and punctuation:

| Tier | Score | Example (query "inter") |
|---|---|---|
| exact (also ignoring a leading The/A/An) | 0 | "Inter" |
| title starts with query | 10 | "Interstellar", "The Intern" |
| a later word starts with query | 20 | "Girl, Interrupted" |
| query appears mid-word | 30 | "Winterfell" |
| every query word starts some title word | 35 | "star wars" → "Wars of the Stars" |
| no visible match (Emby matched another field) | 40 | a movie found via its cast |

Extra penalties: +3 when matching `OriginalTitle`/`SortName` instead of `Name`;
+5 when matching only after dropping the query's own leading article (so
"The Office" prefers "The Office (US)" to "Office Space").

Type penalty (row order and penalties chosen by the user, 2026-09-30):

| Types | Penalty |
|---|---|
| Series | 0 |
| Movie | 1 |
| BoxSet (collections) | 3 |
| Trailer, music, Book, Playlist (none in library yet) | 5 |
| Genre | 14 |
| Studio | 15 |
| Person | 16 |
| Episode | 18 |
| Program, TvChannel (Live TV), unknown types | 20 |
| Tag | 25 |

Tiers are 10 apart, so among shows, movies and collections (spread 0–3) a
better title match always wins. People, studios and episodes only beat media
when they match clearly better. Example: "tom hanks" finds the person (0+16)
before a movie matched via its cast (40+1). Example: "fox" puts the studio FOX
(exact, 0+15) before *Fantastic Mr. Fox* (later word, 20+1).

Row order (`TYPE_ORDER`): TV shows, Movies, Collections, Genres, Studios,
People, Episodes, Live TV programs, Live TV channels, (music/books/other media
when they exist), Tags, then anything unknown.

### Repeat penalty and partial words (1.3.0)

- **Partial word +2.** If the query covers only part of the matching word
  ("harry" → *Harryhausen*), the match costs 2 more than a whole-word match at
  the same level (starts-with 10 vs 12, later word 20 vs 22).
- **Repeat penalty.** Each type's items are queued best-first, and the list is
  built by repeatedly taking the cheapest queue head after adding its repeat
  penalty: `REPEAT_STEP × max(0, n − FREE_REPEATS + 1)`, where n is how many of
  that type are already placed. With `FREE_REPEATS=3` and `REPEAT_STEP=2`, the
  4th person costs +2, the 5th +4, and so on. Series, Movie and BoxSet are
  exempt so a franchise stays together. A single-type list (a search row)
  keeps its pure score order.

### People signal (1.4.0)

People credited in more titles in the library rank higher among people:
`bonus = −min(6, 2 × log2(titles))`, so 1 title: 0, 2: −2, 4: −4, 8 or more: −6.
Titles are counted per Emby person Id across movies, series and episodes, with
an episode credit counting toward its series. The count is library-wide (all
libraries, not per user); it only changes the order of people the searching
user can already see.

Built by `people.py` in a background thread (three `/Items?Fields=People`
queries using the searchfix API key), rebuilt every 6 h, snapshotted to
`/data/people_counts.json`. Until a table exists the bonus is simply skipped.
A search never waits on it.

Why not reuse the curated-lists DB (`~/scripts/hsc_metadata.db`, `movies_meta.people`)?
It covers movies only: 37,893 of ~70k people, with no TV or guest stars. It
keys people by name, not Id, so namesakes merge. Each movie is fetched once and
never refreshed. And it is owned by a separate nightly AI pipeline. Coupling
the front door for all Emby traffic to it would add a failure mode for a 3-second scan's worth of savings.

Interaction with type order: the bonus (max 6) is smaller than a match level
(10) but larger than the gap between some types. A prolific person whose name
*starts with* the query (10 + 16 − 6 = 20) outranks a movie where the query is
a *later word* (20 + 1 = 21). See the decision note in CHANGELOG/STATE.

### Candidate pool

For list queries, searchfix asks Emby for `max(Limit, 150)` items, re-ranks
them and returns the first `Limit`. That way a strong match Emby ranked
beyond the requested 16 can still surface. The extra cost measured on this
library was 10–300 ms per search.

## Sorting and paging

- **Default name sorts are replaced by relevance.** The web client's row
  loader (`sectionscontroller.js`) adds `SortBy=SortName&SortOrder=Ascending`
  (episodes: `SeriesSortName,ParentIndexNumber,IndexNumber,SortName`) to
  *every* search row. Respecting it kept rows alphabetical: "Dirty Harry"
  before "Harry Potter" (bug found and fixed 2026-09-30, v1.1.0). These
  default sorts (`DEFAULT_NAME_SORTS` in `server.py`) don't count as a
  request for a specific order.
- **Other sorts are respected.** `DateCreated`, `CommunityRating`, `Random`,
  `ChannelNumber,SortName` (Live TV rows) and so on pass through untouched.
- **Paging stays consistent.** For `StartIndex=s&Limit=n`, searchfix asks Emby
  for items `0 … max(150, s+n)` in Emby's relevance order (sort/paging
  params dropped), re-ranks them, then returns `[s : s+n]`. Every page is a
  slice of the same ranking, so nothing is duplicated or skipped.
- **No `Limit`** means the client wants everything. No limit is sent to Emby
  and everything comes back re-ranked.
- Other endpoints carrying `SearchTerm` (`/Persons`, `/Artists`, `/Genres`…)
  are proxied unchanged. `/Search/Hints` returns `[]` on Emby 4.10 (retired).

## Fuzzy search (1.6.0)

**When:** a mixed list (Top Results, side-menu search; never a single-type row)
where no title, collection or person in Emby's results matches the whole query
(`fuzzy.has_whole_query_match`). Emby ORs words, so "harry poter" returns 134
Harrys, none matching both words. Ordinary and half-typed searches ("harry
pot") never trigger it.

**How:** query letter trigrams → the 150 names sharing the most (ties: most
similar trigram set, then index order, which keeps it deterministic) → each
query word scored against the closest word of the name (difflib), so long
titles aren't penalised and "rngs" ≈ "rings". Matches scoring ≥ 0.82 go to the
front. Ones Emby already returned are moved; others are fetched by Id **using
the searching user's credentials**, so Emby's library access and parental
controls decide what appears. People need a separate fetch with
`IncludeItemTypes=Person`.

**Cost (measured 2026-10-01):** index of 72.5k names, ~1 s to build in the
background, ~70 MB. Lookup median 12–16 ms, worst ~30 ms, plus one Ids fetch.
Ordinary searches pay one in-memory check (microseconds). For comparison,
Emby's own search takes 200–600 ms for multi-word queries.

## Query expansion (1.7.0)

Fuzzy (above) only helps when *nothing* matches. "Hary" matches three obscure
people, so the user never saw the hundreds of "Harry" results they meant.
`expand.py` builds a word-frequency vocabulary from the same library names and,
when a typed word is rare (< 10 names) and a word with the same first letter,
≤ 2 letters difference, similarity ≥ 0.8 and ≥ 10× the frequency exists,
also searches the corrected term. Pools are merged; every item scores the
better of (score for the typed term) and (score for the corrected term + 10).

Gate, because correction is risky (a naive version rewrote 8% of real queries):
expand only if the typed query matches ≤ 5 titles/people/studios and none
strongly (exact name, or name starting with the query as whole words). Never
correct a non-word that begins a longer word (the user is still typing).
Decision cached 60 s per user and term, so a search page's several requests
share one gate query. Measured: fires on 1/1,102 real queries, 0 targets
moved down, all 28 expectation checks pass.

## Search log (1.6.0)

`searches.db` (SQLite, WAL) in the bind-mounted data dir. One row per search:
time, user, app/client, app version, device, device id, IP (from nginx's
`X-Real-IP`), final term, candidate count, top result, fuzzy flag. A search
page fires several requests, and some apps search per keystroke, so requests
from one device are folded into the previous row while the term keeps
extending or shrinking within 20 s. Writes are queued to a background thread.
Retention 365 days. Smoke/eval send `X-Searchfix-Test` and are not logged.
Remote users' IP is the Docker bridge (the socat relay hides it).

## Failure modes

| Failure | Effect |
|---|---|
| searchfix container down or refusing connections | nginx `error_page 502 504 = @emby_direct` retries against Emby → normal Emby ordering |
| Re-ranking throws (unexpected JSON etc.) | Emby's response served unmodified (trimmed to `Limit`) |
| Emby down | 502, same as before (Telegram bot alerts; it pings `:8096` through the proxy) |
| nginx down | **Emby unreachable on :8096.** Mitigations: `restart: unless-stopped`, healthcheck, bot alert; rollback in runbook |
| fuzzy index or search log unavailable | ranking continues without it; health shows `fuzzy=off` / `searchlog=off` |
| emby/searchfix recreated with a new IP | nginx re-resolves container names every 10 s (`resolver 127.0.0.11`, `server … resolve`) |
| Long paused direct-play stream | `send_timeout`/`proxy_*_timeout` 3600 s + TCP keepalive; verified a 100 s stall resumes |

## Client IPs

Emby sees connections coming from nginx. Emby's *Read proxy headers*
setting (`ProxyHeaderMode`) is set to `AllAddresses`, and nginx
**overwrites** `X-Forwarded-For` with the real peer address (never appends).
A client can't spoof its IP by sending the header itself. Remote users still
appear as the Docker bridge address, because the socat relay hides them;
that was already the case before this proxy.

## Known limits

- Roku is a native client. If it builds search from different calls, those
  pass through unchanged. `docker logs emby-searchfix` shows the client name
  on every re-ranked search, which is how to tell.
- Re-ranking only reorders what Emby returns. It can't find items Emby's
  search doesn't match.
