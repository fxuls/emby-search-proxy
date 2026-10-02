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

## Failure modes

| Failure | Effect |
|---|---|
| searchfix container down or refusing connections | nginx `error_page 502 504 = @emby_direct` retries against Emby → normal Emby ordering |
| Re-ranking throws (unexpected JSON etc.) | Emby's response served unmodified (trimmed to `Limit`) |
| Emby down | 502, same as before (Telegram bot alerts; it pings `:8096` through the proxy) |
| nginx down | **Emby unreachable on :8096.** Mitigations: `restart: unless-stopped`, healthcheck, bot alert; rollback in runbook |
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
