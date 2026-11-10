# Home-row shuffle (1.9.0)

A home row sorted by rating shows the same titles in the same order every day.
Most people never page past the first screen of a row (on our server, row
requests for the second page were ~4% of first-page requests), so most of a
long row is never seen. searchfix gives these rows a **new order every day**,
while higher-rated titles still lean toward the front.

## How it works

For each title in the row:

```
score = CommunityRating + SIGMA × z
```

and the row is sorted by `score`, highest first. `z` is a standard-normal
number (mean 0, spread 1) computed from a hash of **(row, day, item id)**:

- **Same order for every user, all day.** Every user's copy of a row has its
  own section id, so "row" means the section's query (its `TagIds`, `GenreIds`,
  `StudioIds`) instead. Every page is a slice of that one list, so scrolling
  never shows a title twice or skips one.
- **A new title doesn't reshuffle the row.** Each title's nudge depends only on
  that title, so a title added mid-day just slots in.
- **SIGMA is in rating points.** With `0.4`, a title often beats one rated ~0.4
  higher, sometimes one ~0.8 higher, and practically never one 1.5 higher. A 6.5
  never jumps an 8.5.
- **The day starts at `ROW_SHUFFLE_ROLLOVER_HOUR`** (default 04:00, container
  `TZ`), not at midnight, so a row doesn't change in the middle of an evening.
- Titles with no rating count as 0 and stay at the back, where Emby puts them too.
- `SIGMA = 0` gives exactly Emby's own order (the sort is stable). That's used
  to test the request path on real devices before any order changes.

### Which rows

A section is reordered only if **all** of these hold (`rowshuffle.eligible`):

- `SectionType` is `items` and it has a `TagIds`/`GenreIds`/`StudioIds` query;
- its stored `SortBy` is in `ROW_SHUFFLE_SORTBY` (default `CommunityRating,SortName`);
- its `SortOrder` is `Descending`.

Everything else (Continue Watching, Latest, rows sorted by name or date, the
plugin's top lists) is passed to Emby untouched, byte for byte.

## Request path

Apps load a row with `GET .../Users/{u}/Sections/{s}/Items?StartIndex&Limit&Fields...`.
Emby **ignores** any `SortBy` sent here and uses the order saved on the section,
so the order can't be changed by request parameters. It can only be changed
server-side, which is why this lives in the proxy.

1. nginx routes `/Users/*/Sections/*/Items` to searchfix (one `map` line).
2. searchfix looks the section up in the user's `/HomeSections` (cached 5 min).
   If it isn't eligible, the request goes to Emby unchanged.
3. It gets the whole row once: the same section endpoint, no `Limit`, only
   `Id` and `CommunityRating` (cached 10 min per user and section,
   `ROW_SHUFFLE_MEMBERS_TTL`). That's exactly Emby's membership and access rules for that user.
4. It orders the ids (`rowshuffle.order`), takes the requested page, and asks
   Emby for `.../Users/{u}/Items?Ids=<page>` plus the app's own `Fields` and image
   parameters. Emby returns items in the order of `Ids`.
5. Emby's response goes back as is. If the app wants a total
   (`EnableTotalRecordCount` not `false`), `TotalRecordCount` is set to the row size.

Unlike the 1.8.0 top-list rewrite, item ids don't change and item JSON isn't
edited: the app gets the same items Emby would send, in a different order.
Measured: about +10 ms per page once cached (+15–20 ms for the first page after
a cache expiry).

**Fails open:** any error (HomeSections or the row fetch fails, Emby answers
non-200, bad JSON) → the original request is sent to Emby and its answer
returned. If searchfix is down, nginx falls back to Emby (502/504 → `@emby_direct`).

## Settings (environment, searchfix container)

| Variable | Default | Meaning |
|---|---|---|
| `ROW_SHUFFLE` | `0` | `1` = handle home-row requests. `0` = pass them through unchanged |
| `ROW_SHUFFLE_SIGMA` | `0.4` | Nudge size in rating points. `0` = Emby's order (path test) |
| `ROW_SHUFFLE_ROLLOVER_HOUR` | `4` | Local hour the day (and so the order) changes |
| `ROW_SHUFFLE_SORTBY` | `CommunityRating,SortName` | `;`-separated stored sorts that qualify |
| `ROW_SHUFFLE_MEMBERS_TTL` | `600` | Seconds a row's membership is cached |
| `TOPLIST_ROW_REWRITE` | `0` | The old top-list copy swap on home rows (off since 1.8.3) |

`/health` shows `rows=sigma0.4` or `rows=off`. Each served page logs a line:
`rows  client='Emby for iOS' row='Animation' start=0 limit=12 of=303 sigma=0.4 day=2026-11-11`.

## Choosing SIGMA

Simulated over 165 real rows with >12 titles, 28 days, first 12 cards:

| SIGMA | cards new each day | different titles on page 1 in 28 days | avg rating of page 1 vs. today | today's #1 still on page 1 |
|---|---|---|---|---|
| 0 | 0% | 12 | 0.00 | 100% |
| 0.2 | 20% | 20 | −0.03 | 100% |
| 0.3 | 28% | 24 | −0.07 | 98% |
| **0.4** | **34%** | **28** | **−0.10** | **95%** |
| 0.5 | 39% | 31 | −0.15 | 92% |
| 0.75 | 48% | 37 | −0.24 | 83% |

Alternatives rejected: Emby's `Random` sort (ignores rating; reshuffles on
every load); `CommunityRating,Random` (only shuffles exact ties, ~4% change);
writing a sort key into an item field (shows up in other sorts and is lost on
metadata refresh); shuffling within fixed rating bands (arbitrary edges, little change).

## Turning it off / changing it

- **Change SIGMA or turn off:** edit the env in the compose file, then
  `docker compose up -d searchfix` (rows fall back to Emby for the few seconds it restarts).
- **Take searchfix out of the row path completely:** delete the
  `/Sections/[^/]+/Items` line in `nginx/default.conf`'s `map`, then
  `docker exec emby-proxy nginx -t && docker exec emby-proxy nginx -s reload`.

Code: `searchfix/rowshuffle.py` (pure functions), `Handler.handle_row_shuffle`
in `searchfix/server.py`. Tests: `tests/test_rowshuffle.py`.
