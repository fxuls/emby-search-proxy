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

Type penalty:

| Types | Penalty |
|---|---|
| Movie, Series | 0 |
| BoxSet (collections) | 3 |
| Trailer, music, Book, Playlist | 5 |
| Episode, Person, TvChannel, Program (and unknown types) | 12 |
| Genre, Studio, Tag | 25 |

So a person whose name exactly matches (0+12) still beats a movie that only
matched through its cast list (40+0), and "tom hanks" still finds Tom Hanks
first. But a partial tag match (e.g. tag "internship" at 10+25) never beats a
title match.

Row order (`TYPE_ORDER`): Movie, Series, BoxSet, Episode, Trailer, music
types, Book, Playlist, TvChannel, Program, Person, Genre…, Studio, Tag, then
anything unknown.

### Candidate pool

For list queries, searchfix asks Emby for `max(Limit, 150)` items, re-ranks
them and returns the first `Limit`. That way a strong match Emby ranked
beyond the requested 16 can still surface. The extra cost measured on this
library was 10–300 ms per search.

## What is deliberately not rewritten

- Requests with `SortBy`: the client chose an order (e.g. library filter-by-name).
- Requests with `StartIndex`: paged lists. Re-ranking page 1 only would
  duplicate or skip items across pages.
- Other endpoints carrying `SearchTerm` (`/Persons`, `/Artists`, `/Genres`…):
  proxied unchanged.
- `/Search/Hints`: returns `[]` on Emby 4.10 (retired), so no client relies on it.

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
