#!/usr/bin/env bash
# Put emby-search-proxy in front of Emby on :8096 (or take it back out).
#
#   EMBY_API_KEY=... scripts/cutover.sh              dry run: checks + plan only
#   EMBY_API_KEY=... scripts/cutover.sh --apply      cut over; auto-rollback on failure
#   EMBY_API_KEY=... scripts/cutover.sh --rollback   restore pre-proxy compose file
#   optional: RELAY_CONTAINER=... RELAY_PORT=... also checks a remote-access relay
#
# --apply refuses while anyone is playing unless --force is also given.
# Expects ~/docker/emby/docker-compose.searchproxy.yml (the staged file) and
# ~/docker/emby/docker-compose.yml.pre-searchproxy.bak (the backup).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
EMBY_DIR=/home/mc/docker/emby
LIVE=$EMBY_DIR/docker-compose.yml
STAGED=$EMBY_DIR/docker-compose.searchproxy.yml
BACKUP=$EMBY_DIR/docker-compose.yml.pre-searchproxy.bak
BASE=http://localhost:8096/emby
: "${EMBY_API_KEY:?set EMBY_API_KEY}"

MODE=dry; FORCE=0
for a in "$@"; do
  case $a in
    --apply) MODE=apply ;;
    --rollback) MODE=rollback ;;
    --force) FORCE=1 ;;
    *) echo "unknown arg $a"; exit 2 ;;
  esac
done

say() { echo "[$(date +%T)] $*"; }

set_proxy_headers() {  # $1 = None | AllAddresses
  local cfg
  cfg=$(curl -sf "$BASE/System/Configuration?api_key=$EMBY_API_KEY")
  echo "$cfg" | python3 -c "import sys,json;d=json.load(sys.stdin);d['ProxyHeaderMode']='$1';print(json.dumps(d))" |
    curl -sf -o /dev/null -X POST -H 'Content-Type: application/json' --data-binary @- \
      "$BASE/System/Configuration?api_key=$EMBY_API_KEY"
  local now
  now=$(curl -sf "$BASE/System/Configuration?api_key=$EMBY_API_KEY" | python3 -c "import sys,json;print(json.load(sys.stdin)['ProxyHeaderMode'])")
  [ "$now" = "$1" ] && say "Emby ProxyHeaderMode = $now" || { say "ProxyHeaderMode is $now, expected $1"; return 1; }
}

wait_emby() {  # wait until :8096 answers (through whatever is on it)
  for _ in $(seq 1 90); do
    curl -sf -o /dev/null "$BASE/System/Info/Public" && return 0
    sleep 2
  done
  return 1
}

playing() {
  curl -sf "$BASE/Sessions?api_key=$EMBY_API_KEY" |
    python3 -c "import sys,json;p=[f\"{x.get('UserName')} ({x.get('Client')}): {x['NowPlayingItem'].get('Name')}\" for x in json.load(sys.stdin) if x.get('NowPlayingItem')];print('\n'.join(p))"
}

rollback() {
  say "ROLLBACK: restoring $BACKUP"
  cp "$LIVE" "$EMBY_DIR/docker-compose.yml.with-searchproxy.bak"
  cp "$BACKUP" "$LIVE"
  (cd "$EMBY_DIR" && docker compose up -d --remove-orphans)
  wait_emby && say "Emby answering on :8096 directly" || say "WARNING: Emby not answering yet"
  set_proxy_headers None || true
}

# ---------------------------------------------------------------- checks
[ -f "$BACKUP" ] || { echo "missing $BACKUP"; exit 1; }

if [ "$MODE" = rollback ]; then rollback; exit 0; fi
# --apply/dry run need the staged file; it was removed after the 2026-09-30
# cutover (the live compose file is the source of truth now).
[ -f "$STAGED" ] || { echo "missing $STAGED (only needed to cut over again)"; exit 1; }

(cd "$EMBY_DIR" && docker compose -f "$STAGED" config -q) && say "staged compose file valid"
(cd "$REPO" && python3 -W ignore -m unittest discover -s tests >/dev/null 2>&1) && say "unit tests pass"
docker run --rm -v "$REPO/nginx/default.conf:/etc/nginx/conf.d/default.conf:ro" \
  --add-host emby:127.0.0.1 --add-host searchfix:127.0.0.1 nginx:1.31-alpine nginx -t -q && say "nginx config valid"

P=$(playing)
if [ -n "$P" ]; then
  say "currently playing:"; echo "$P" | sed 's/^/    /'
  if [ "$MODE" = apply ] && [ $FORCE = 0 ]; then say "refusing to cut over during playback (use --force)"; exit 3; fi
else
  say "nobody is playing"
fi

if [ "$MODE" = dry ]; then
  say "DRY RUN. --apply would: install staged compose, build searchfix, recreate emby"
  say "(~30-60 s Emby interruption), start emby-proxy on :8096, set ProxyHeaderMode=AllAddresses,"
  say "run scripts/smoke.py + relay check, and roll back automatically if any of that fails."
  exit 0
fi

# ---------------------------------------------------------------- apply
trap 'say "failure, rolling back"; rollback; exit 1' ERR
cp "$STAGED" "$LIVE"
say "building searchfix"
(cd "$EMBY_DIR" && docker compose build -q searchfix)
say "recreating stack"
(cd "$EMBY_DIR" && docker compose up -d)
wait_emby || { say "Emby not answering through proxy"; false; }
say "Emby answering through emby-proxy"
set_proxy_headers AllAddresses
EMBY_API_KEY=$EMBY_API_KEY python3 "$REPO/scripts/smoke.py" http://localhost:8096
# Optional: if remote users reach Emby through a relay running inside another
# container's network (e.g. a VPN container + socat), check that path too.
#   RELAY_CONTAINER=<container name> RELAY_PORT=<port it listens on>
if [ -n "${RELAY_CONTAINER:-}" ] && [ -n "${RELAY_PORT:-}" ]; then
  say "checking remote path ($RELAY_CONTAINER :$RELAY_PORT -> :8096)"
  docker run --rm --network "container:$RELAY_CONTAINER" curlimages/curl -sf -o /dev/null \
    "http://localhost:$RELAY_PORT/emby/System/Info/Public" && say "relay path OK"
fi
trap - ERR
say "CUTOVER COMPLETE"
