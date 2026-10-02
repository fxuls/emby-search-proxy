# Changelog

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
