# Runbook

All commands assume `cd ~/homelab/emby-search-proxy`. API key for smoke tests:
the "Fx" key in Emby's `authentication.db` (see
Emby → Settings → API Keys). Never commit it.

## Deploying a change

1. Edit, then run the offline tests:
   ```bash
   python3 -m unittest discover -s tests
   ```
2. Try it on a side port without touching live traffic:
   ```bash
   docker build -q -t emby-searchfix:test searchfix/
   docker run -d --name searchfix-test --network emby_default --network-alias searchfix-test \
     -e EMBY_UPSTREAM=http://emby:8096 emby-searchfix:test
   sed 's/server searchfix:8080/server searchfix-test:8080/' nginx/default.conf > /tmp/test.conf
   docker run -d --name emby-proxy-test --network emby_default -p 8098:8096 \
     -v /tmp/test.conf:/etc/nginx/conf.d/default.conf:ro nginx:1.31-alpine
   EMBY_API_KEY=<key> scripts/smoke.py http://localhost:8098
   docker rm -f searchfix-test emby-proxy-test
   ```
3. Commit, then roll it out.
   - **searchfix code** (no Emby interruption; searches fall back to Emby for a few seconds):
     ```bash
     cd ~/docker/emby && docker compose build searchfix && docker compose up -d searchfix
     ```
   - **nginx config** (graceful reload, no dropped streams):
     ```bash
     docker exec emby-proxy nginx -t && docker exec emby-proxy nginx -s reload
     ```
4. Verify: `EMBY_API_KEY=<key> scripts/smoke.py` (defaults to :8096).
5. If the compose services changed, update `deploy/compose-services.yml` to match.

## Cutover / rollback

Scripted in `scripts/cutover.sh`. It works from the staged file
`~/docker/emby/docker-compose.searchproxy.yml` and the backup
`~/docker/emby/docker-compose.yml.pre-searchproxy.bak`.

```bash
EMBY_API_KEY=<key> scripts/cutover.sh              # dry run: validates everything, shows who is playing
EMBY_API_KEY=<key> scripts/cutover.sh --apply      # refuses during playback unless --force
EMBY_API_KEY=<key> scripts/cutover.sh --rollback
```

`--apply` does the following, and restores the backup automatically if any step fails:
1. Installs the staged compose file (emby loses its `8096:8096` mapping; adds searchfix + emby-proxy).
2. Runs `docker compose build searchfix && docker compose up -d`. Emby is recreated, a ~30–60 s interruption.
3. Waits for :8096, then sets Emby `ProxyHeaderMode=AllAddresses` via the API.
4. Runs `scripts/smoke.py`, then checks the remote path: <vpn-container> → socat :<relay-port> → :8096.

## Manual rollback (if the script itself can't run; ≈1 minute of Emby downtime)

```bash
cd ~/docker/emby
cp docker-compose.yml docker-compose.yml.with-searchproxy.bak
cp docker-compose.yml.pre-searchproxy.bak docker-compose.yml
docker compose up -d --remove-orphans     # emby gets :8096 back; proxy containers removed
```
Then set Emby → Settings → Network → *Read proxy headers* back to **No**.
Nothing else depends on the proxy.

Emergency: if nginx is wedged and you can't think, `docker restart emby-proxy`.

## Troubleshooting

| Symptom | Check |
|---|---|
| Emby unreachable on :8096 | `docker ps -a --filter name=emby-proxy`; `docker logs --tail 50 emby-proxy`. If nginx is fine, check Emby itself: `docker exec emby-proxy wget -qO- http://emby:8096/emby/System/Ping` |
| Search ordering looks like stock Emby | `docker logs --tail 20 emby-searchfix`. `skip` lines show requests it saw but didn't re-rank, and why (e.g. an explicit `sortby`). No lines at all means searchfix is down (nginx falls back to Emby) or the client doesn't search via `/Items`+`/ItemTypes` |
| A specific result ranks badly | Reproduce: `curl "http://localhost:8096/emby/Users/<uid>/Items?api_key=<key>&SearchTerm=<q>&Recursive=true&Limit=16"`. Add the case to `tests/test_rank.py`, tune `rank.py`, deploy |
| Remote playback errors | Not this proxy first: see gotchas.md "socat relay drops long connections". Then `docker logs emby-proxy` |
| Dashboard shows every client at 172.x | `ProxyHeaderMode` got reset. Set *Read proxy headers* to Yes |

## People signal

- Key: `.env` → `EMBY_API_KEY` (Emby API key "custom-search"). `chmod 600`, git-ignored.
  To rotate: create a new key in Emby → Settings → API Keys, edit `.env`,
  `cd ~/docker/emby && docker compose up -d searchfix`.
- Status: `docker exec emby-searchfix wget -qO- http://127.0.0.1:8080/health` → `ok people=69750`.
  `docker logs emby-searchfix | grep people:` shows builds, snapshot loads and failures.
- Force a rebuild now: `docker restart emby-searchfix`. It loads the snapshot, then
  rebuilds when the snapshot is ≥6 h old; delete the volume file first to force it immediately.
- If Emby is down during a refresh, the old table is kept and it retries every 10 min.

## Measuring search quality

```bash
cd ~/homelab/emby-search-proxy
docker run --rm --network emby_default -v $PWD:/repo:ro -v emby_searchfix-data:/data:ro \
  -e EMBY_API_KEY=<key> -e EMBY_USER_ID=<admin user id> \
  python:3.12-alpine python /repo/scripts/eval.py --n 400 --variants --live
```
Run it before and after any weight change; add a CASES entry for every
"search looks wrong" report. The volume is `emby_searchfix-data` (compose
prefixes the project name). A bare `searchfix-data` silently creates an empty one.

## Tuning knobs

| Where | Knob | Default |
|---|---|---|
| `rank.py` | `TYPE_ORDER`, `TYPE_PENALTY`, tier values | see design.md |
| env `CANDIDATE_POOL` | items fetched from Emby before trimming | 150 |
| env `UPSTREAM_TIMEOUT` | seconds to wait for Emby | 30 |
| env `LOG_SEARCH_TERMS` | `0` logs `term='-'` instead of the search text | 1 |
| env `PEOPLE_REFRESH_HOURS` | how often person title counts are rebuilt | 6 |
| `rank.py` | `PERSON_BONUS_PER_DOUBLING`, `PERSON_BONUS_MAX` | 2, 6 |
| `rank.py` | `REPEAT_STEP`, `FREE_REPEATS`, `PARTIAL_WORD_PENALTY` | 2, 3, 2 |
