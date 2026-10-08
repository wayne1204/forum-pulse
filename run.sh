#!/usr/bin/env bash
#
#   ./run.sh daily [--no-label] [--budget 5]   crawl → match → label → prices → backtest → site
#   ./run.sh label [--from YYYY-MM-DD] [--max N] [--workers N]  label Stances only (0 = no cap), republish
#   ./run.sh rebuild                           re-apply Review Queue rules, republish
#   ./run.sh serve [port]                      dashboard + Review Queue, http://127.0.0.1:8765
#   ./run.sh deploy                            upload site/ to Cloudflare Pages (daily does this too)
#   ./run.sh test                              pytest
#
# Creates .venv on first use (uv if present, else python3.11 -m venv).
set -euo pipefail
cd "$(dirname "$0")"

PY=.venv/bin/python
STAMP=.venv/.requirements.sha256
if [ ! -x "$PY" ]; then
  if command -v uv >/dev/null; then uv venv -q -p 3.11 .venv
  else "$(command -v python3.11 || command -v python3)" -m venv .venv; fi
fi
want="$(sha256sum requirements.txt | cut -d' ' -f1)"
if [ "$(cat "$STAMP" 2>/dev/null || true)" != "$want" ]; then
  echo "installing dependencies …" >&2
  if command -v uv >/dev/null; then uv pip install -q -p "$PY" -r requirements.txt
  else "$PY" -m pip install -q -r requirements.txt; fi
  printf '%s' "$want" >"$STAMP"
fi

# Cron runs without the login shell's environment; pick up the API key here.
[ -f .env ] && set -a && . ./.env && set +a

# Publish site/ to Cloudflare Pages, minus the Review Queue: it needs server.py and
# the local DB, so its links point back at `serve` on this machine instead.
# Skipped until CLOUDFLARE_API_TOKEN is in .env (or `wrangler login` was run, for manual use).
deploy() {
  local wrangler
  wrangler="$(command -v wrangler || ls -d "$HOME"/.nvm/versions/node/*/bin/wrangler 2>/dev/null | tail -1 || true)"
  if [ -z "$wrangler" ]; then echo "deploy: wrangler not found (npm install -g wrangler)" >&2; return 1; fi
  local out=data/deploy
  mkdir -p "$out"
  rsync -a --delete --exclude "review.*" site/ "$out/"
  # Pages won't _redirect to a loopback address, so a stub page hands off instead.
  cat >"$out/review.html" <<'HTML'
<!doctype html><meta charset="utf-8"><title>Review Queue</title><link rel="icon" href="favicon.svg" type="image/svg+xml">
<meta http-equiv="refresh" content="0; url=http://127.0.0.1:8765/review.html">
<p>The Review Queue runs locally: <a href="http://127.0.0.1:8765/review.html">open it</a> (needs <code>./run.sh serve</code>).
HTML
  # wrangler is a node script; cron's PATH has no node, so put its own bin dir first.
  PATH="$(dirname "$wrangler"):$PATH" "$wrangler" pages deploy "$out" \
    --project-name "${CF_PAGES_PROJECT:-forum-pulse}" --branch main --commit-dirty=true
}

cmd="${1:-daily}"; shift || true
case "$cmd" in
  test) exec "$PY" -m pytest -q "$@" ;;
  deploy) deploy ;;
  # One daily run at a time: a cron firing during a long backfill just steps aside.
  daily)
    mkdir -p data
    flock -n data/.daily.lock "$PY" -m forum_pulse daily "$@"
    if [ -n "${CLOUDFLARE_API_TOKEN:-}" ]; then deploy; fi ;;
  label) mkdir -p data; exec flock -n data/.daily.lock "$PY" -m forum_pulse "$cmd" "$@" ;;
  rebuild|serve) exec "$PY" -m forum_pulse "$cmd" "$@" ;;
  *) echo "unknown command: $cmd" >&2; exit 2 ;;
esac
