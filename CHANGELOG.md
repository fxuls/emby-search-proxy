# Changelog

## 1.8.3 — 2026-10-29
- **Home-row rewrite turned off (nginx no longer routes `/Sections/{id}/Items`
  to searchfix).** Tested on real devices it broke: the Android TV app caches
  items by Id, so after opening a title (now the real item, clean poster) the
  row card showed the clean poster too and the rank numbers disappeared; on
  iOS the home rows stopped loading (the app dropped its row requests, "broken
  pipe" in searchfix; not reproduced). The code stays in `toplist.py` but is
  unused until a design that survives app caching is found. Numbered-poster
  routing stays, for rows apps still hold.
- Still active: search swaps lone copies (1.8.1), and single-item/PlaybackInfo
  responses hide the copy version (1.8.2).

## 1.8.2 — 2026-10-29
- **Top-list titles no longer offer a second "Version".** Home Screen Companion
  merges each `.strm` copy into its real title as an alternate version, so the
  detail page showed a Version dropdown with two entries for the same file.
  searchfix now drops the copy's entries (`MediaSources[].ItemId` is a known
  copy) from `GET .../Users/{u}/Items/{id}` and from `.../Items/{id}/PlaybackInfo`,
  which is where Emby Web builds the dropdown. The real file's entry is passed
  through untouched (direct play verified), and other multi-version movies are
  left alone.
- Request bodies are read once and forwarded as-is, including chunked uploads;
  side lookups never resend the client's body.
- nginx: PlaybackInfo has its own location with `proxy_request_buffering on`, so
  when searchfix is down the device profile still reaches Emby via
  `@emby_direct` (tested by stopping searchfix).

## 1.8.1 — 2026-10-29
- **Search shows the real title instead of a lone top-list copy.** Emby lists
  merged versions once and sometimes picks Home Screen Companion's copy:
  "Infinity War" returned only the numbered copy (17 of 169 top-list titles
  did this). After ranking and paging, any copy whose real title isn't already
  in the list is replaced by it, fetched with the user's own credentials
  (`Server.swap_lone_copies`). Measured after deploy: 0 of 169.

## 1.8.0 — 2026-10-29
- **Top-list rows keep their numbers; everything else gets the real title**
  (`searchfix/toplist.py`). Home Screen Companion's ranked rows are made of
  `.strm` copies with the rank drawn into each copy's poster, so opening a card
  opened the copy and its detail page showed the number. The copies also get new
  ids whenever the plugin rebuilds them, so an app showing an older row got
  "item not found" (seen on Android TV).
- nginx now also sends home rows (`/Users/{u}/Sections/{id}/Items`) to searchfix.
  On a top-list row (the section's library is one of the plugin's), each copy is
  replaced by the real title, fetched with the user's own credentials, and its
  Primary/Thumb tags become `hsc<copyId>_<tag>`. On other rows (Continue
  Watching) a copy becomes the plain real title.
- Image requests with an `hsc…` tag are routed to searchfix and served from the
  copy. If the copy is gone, the real title's own poster is served instead.
- The copy → title map (IMDb, else TMDb, per type) is rebuilt every 10 minutes
  from Emby's library list (`TOPLIST_REFRESH_MINUTES`, `TOPLIST_ROWS=0` = off).
  A user's section list is cached for 5 minutes. Fails open everywhere.

## 1.7.1 — 2026-10-01
- Home Screen Companion's top-watched rows are built from `.strm` copies in
  `/config/plugins/HomeScreenCompanion/toplists/`. Accounts that can see them
  got each such title twice in search ("Philosopher's Stone" at #1 and #10).
  `rank.drop_hsc_copies` removes a copy whenever the real item (same type and
  TMDB id, else same name and year) is in the same list. A lone copy is kept. It
  applies to Top Results, rows and fuzzy matches, before paging. Pools now also
  request `Path,ProviderIds,ProductionYear` (paths are visible to non-admins too).

## 1.7.0 — 2026-10-01
- **Query expansion** (`searchfix/expand.py`): "Hary" showed only 3 obscure
  people ("Haryadi Anwar"…), because something matched, so fuzzy didn't fire.
  Now a rare word with a far more common near-identical library word (hary →
  harry) also searches the common word. Results are ranked together, and
  matches only to the correction score one tier lower (`rank.ALT_TERM_PENALTY`),
  so a real match for what was typed always comes first. Applies to Top
  Results, the row list and each row.
- Safety, measured on 1,102 real library queries: a naive corrector rewrote 92
  of them ("Rick and Morty" → "rick and marty"). The shipped rules fire on 1
  and move 0 targets down:
  - only when the original matches ≤ 5 titles/people/studios AND none of them
    strongly (name == query or starts with it as whole words)
  - never on a non-word that starts a longer word (still typing: "skan" → Skandar)
  - the decision is cached per user and term for 60 s (one gate query per search)
- Search log gets a `corrected` column (auto-migrated); `/searches` shows
  "✨also searched harry".
- eval.py mirrors expansion with the same code, compares expansion off/on on
  the same pools, and adds "Hary" checks.

## 1.6.0 — 2026-10-01
- **Search log** (`searchfix/searchlog.py`): one SQLite row per search in
  `~/docker/emby/searchfix-data/searches.db`. It records user, app, app version,
  device, IP, final term, result count, top result and whether fuzzy fired.
  Typing/backspacing on one device within 20 s folds into one row holding the
  final term. Background writer, WAL mode, 365-day retention. Requests with
  `X-Searchfix-Test` (smoke/eval) are not logged.
- **Telegram `/searches`** (homelab bot): latest searches, `/searches 30`,
  `/searches <user>`.
- **Fuzzy search** (`searchfix/fuzzy.py`): only when nothing in Emby's
  results matches the whole query. Trigram candidates, then word-level
  similarity. Matches move or are fetched (by Id, **with the user's own
  credentials**, so access rules hold) to the front of mixed lists.
  30/30 typo checks; lookup median 12–16 ms, worst ~30 ms; zero cost for
  ordinary searches. Response header `X-Searchfix: items fuzzy`.
- People are fetched separately by Id: Emby drops them from mixed-type Ids queries.
- Deterministic candidate tie-break (an earlier build depended on the
  process's hash seed: "matrx" found The Matrix in some processes only).
  Names are also indexed without a leading article.
- `people.py` → `library.py` (`LibraryIndex`): the same background scan now
  also collects names (movies, series, collections, credited people) for fuzzy.
  Snapshot `library_index.json`.
- Data moved from the Docker volume to the host dir
  `~/docker/emby/searchfix-data` (container runs as uid 1000) so the bot can read it.
- eval.py: 28 live typo checks; live comparison skips fuzzy responses.

## 1.5.0 — 2026-10-01
Measured with scripts/eval.py (400 targets, 1,288 queries, 26 checks) against
1.4.1: hit@1 58.9% → 61.7%, @3 75.8% → 79.6%, @10 88.9% → 93.9%, MRR
0.688 → 0.722, checks 20/26 → 26/26 (stock Emby: 42.7% / 55.4% / 71.7%).
- Split candidate pool: mixed lists fetch shows/movies/collections separately
  and merge them in front, because Emby can fill the 150-item pool with people
  ("john": 147/150 were people).
- People bonus now only reorders people among themselves (`within`), at 1 per
  doubling, capped at 4 (2: −1, 4: −2, 8: −3, 16+: −4). Famous people no
  longer jump studios or collections ("gilbert", "Dirty Harry Collection").
- Exact multi-word name match caps the type penalty at 5, so "eddie murphy"
  finds the person first. Single words keep the type order; tags excluded.
- Tag penalty 25 → 30: an exact tag ("time travel") no longer beats titles
  starting with it.

## 1.4.1 — 2026-10-01
- Person bonus capped at −4 (user decision "A"): a prolific person whose name
  starts with the query no longer outranks a movie where it is a later word
  (Harry Shearer below *Dirty Harry*).
- `scripts/eval.py`: search-quality evaluation. Samples ~400 real library
  targets, builds ~1,300 realistic queries (full title, first word, first 4
  letters…), compares stock Emby vs rank.py, runs 26 expectation checks and
  scores weight variants offline. `--live` checks the live proxy matches rank.py.
- rank.py gains `PERSON_BONUS_MODE` and `EXACT_PHRASE_PENALTY_CAP` (both off by
  default; ranking unchanged) for the evaluation to try.

## 1.4.0 — 2026-10-01
- People signal: people credited in more library titles rank higher. A bonus
  of −2 per doubling of titles, capped at −6 (1 title: 0, 2: −2, 4: −4, 8+: −6).
- `searchfix/people.py` counts titles per Emby person Id from three
  background queries (movies, series, episodes; an episode credit counts toward
  its show). Rebuilt every 6 h (`PEOPLE_REFRESH_HOURS`), ≈3.6 s of Emby
  work, 69,750 people. Snapshot in the `searchfix-data` volume
  (`/data/people_counts.json`, ≈0.75 MB) so restarts have counts immediately.
- Needs its own Emby API key (`EMBY_API_KEY` in `.env`, git-ignored; Emby key
  "custom-search"). Unset = signal off, ranking unchanged.
- Deliberately separate from the curated-lists DB (`~/scripts/hsc_metadata.db`):
  that DB covers movies only (≈57% of people, no TV), keys people by name, is
  never refreshed per movie, and is owned by a different nightly pipeline.
- `/health` reports `people=<n>` or `people=off`.

## 1.3.0 — 2026-10-01
- Repeat penalty: in mixed lists (Top Results), the 4th and later Person,
  Episode, Tag, Studio, Genre and Live TV items each cost +2 more than the
  previous one of their type. Series/Movie/BoxSet are exempt. "harry" no longer
  shows 112 people before the studio and episodes (studio #128 → #23,
  episodes #127–133 → #19–29). Single-type rows are unaffected.
- Partial-word penalty (+2): "harry" matching "Harryhausen" or "inter"
  matching "Interstellar" ranks just below a whole-word match at the same level.

## 1.2.0 — 2026-09-30
- Row order set by the user: TV shows, Movies, Collections, Genres, Studios,
  People, Episodes, Live TV (programs, channels), Tags. Media types not in the
  library yet (music, books…) sit between Live TV and Tags.
- Top Results leans the same way (show > movie > collection > genre >
  studio > person > episode > Live TV > tag on equal matches). Title-match
  quality still dominates: an exact movie title beats a show that merely
  starts with the query.
- Smoke test checks row order against `rank.py` instead of a hardcoded list.

## 1.1.0 — 2026-09-30
- **Fix:** search rows in the web client stayed alphabetical ("harry" showed
  Dirty Harry first, Harry Potter last). The web client's row loader adds
  `SortBy=SortName` to every search row, which 1.0.0 treated as an explicit
  order and skipped. Default name sorts are now replaced by relevance; other
  sorts are still respected.
- Paged requests (`StartIndex`) are now re-ranked consistently: each page is a
  slice of one ranking over `max(150, start+limit)` candidates.
- Fix: requests with no `Limit` were capped at 150 results.
- Candidate pool is fetched in Emby's relevance order (client sort dropped),
  so alphabetical sorting can't push good matches out of the pool.
- Log every search request searchfix passes through without re-ranking
  (`skip …`), with the reason, plus the row type and page start on re-ranked
  ones.
- Smoke test now replays the web client's exact Movies-row query.

## 1.0.0 — 2026-09-30
- Initial release: nginx front door on :8096 plus the `searchfix` re-ranker.
- Re-orders `/ItemTypes` search rows (Movies/Shows first, Tags/Studios last).
- Re-ranks `/Items?SearchTerm=` lists by title-match tier + type penalty over
  a 150-item candidate pool.
- Leaves `SortBy`/`StartIndex` requests and other search endpoints untouched.
- Fails open: nginx retries directly against Emby if searchfix is down.
- nginx re-resolves container names (survives emby/searchfix recreation),
  3600 s stream timeouts + TCP keepalive (a 100 s paused stream was verified to resume).
