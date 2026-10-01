# Changelog

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
