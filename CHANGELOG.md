# Changelog

## 1.0.0 — 2026-09-30
- Initial release: nginx front door on :8096 plus the `searchfix` re-ranker.
- Re-orders `/ItemTypes` search rows (Movies/Shows first, Tags/Studios last).
- Re-ranks `/Items?SearchTerm=` lists by title-match tier + type penalty over
  a 150-item candidate pool.
- Leaves `SortBy`/`StartIndex` requests and other search endpoints untouched.
- Fails open: nginx retries directly against Emby if searchfix is down.
- nginx re-resolves container names (survives emby/searchfix recreation),
  3600 s stream timeouts + TCP keepalive (a 100 s paused stream was verified to resume).
