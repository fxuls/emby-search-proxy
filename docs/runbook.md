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

## Initial cutover (done 2026-09-30, kept for reference)

1. Check nobody is streaming:
   `curl -s "http://localhost:8096/emby/Sessions?api_key=<key>"`, looking for `NowPlayingItem`.
2. `cp ~/docker/emby/docker-compose.yml ~/docker/emby/docker-compose.yml.pre-searchproxy.bak`
3. In the `emby` service, delete the `- 8096:8096` port line. Append the two
   services from `deploy/compose-services.yml`.
4. `cd ~/docker/emby && docker compose build searchfix && docker compose up -d`.
   Emby is recreated because its ports changed, which interrupts it for about 30–60 s.
5. Set Emby → Settings → Network → *Read proxy headers to determine client IP
   addresses* = **Yes** (`ProxyHeaderMode=AllAddresses`).
6. `scripts/smoke.py`, then check the remote path through the relay:
   `docker run --rm --network container:<vpn-container> curlimages/curl -s http://localhost:<relay-port>/emby/System/Info/Public`

## Rollback (≈1 minute of Emby downtime)

```bash
cd ~/docker/emby
cp docker-compose.yml docker-compose.yml.with-searchproxy.bak
cp docker-compose.yml.pre-searchproxy.bak docker-compose.yml
docker compose up -d --remove-orphans     # emby gets :8096 back; proxy containers removed
```
Then set *Read proxy headers* back to **No**. Nothing else depends on the proxy.

Emergency: if nginx is wedged and you can't think, `docker restart emby-proxy`.

## Troubleshooting

| Symptom | Check |
|---|---|
| Emby unreachable on :8096 | `docker ps -a --filter name=emby-proxy`; `docker logs --tail 50 emby-proxy`. If nginx is fine, check Emby itself: `docker exec emby-proxy wget -qO- http://emby:8096/emby/System/Ping` |
| Search ordering looks like stock Emby | `docker logs --tail 20 emby-searchfix`. No lines when searching means searchfix is down (nginx is falling back) or that client doesn't use `/Items`+`/ItemTypes` search |
| A specific result ranks badly | Reproduce: `curl "http://localhost:8096/emby/Users/<uid>/Items?api_key=<key>&SearchTerm=<q>&Recursive=true&Limit=16"`. Add the case to `tests/test_rank.py`, tune `rank.py`, deploy |
| Remote playback errors | Not this proxy first: see gotchas.md "socat relay drops long connections". Then `docker logs emby-proxy` |
| Dashboard shows every client at 172.x | `ProxyHeaderMode` got reset. Set *Read proxy headers* to Yes |

## Tuning knobs

| Where | Knob | Default |
|---|---|---|
| `rank.py` | `TYPE_ORDER`, `TYPE_PENALTY`, tier values | see design.md |
| env `CANDIDATE_POOL` | items fetched from Emby before trimming | 150 |
| env `UPSTREAM_TIMEOUT` | seconds to wait for Emby | 30 |
| env `LOG_SEARCH_TERMS` | `0` logs `term='-'` instead of the search text | 1 |
