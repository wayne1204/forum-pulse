#!/usr/bin/env bash
#
#   ./label-loop.sh [--from YYYY-MM-DD]     keep deciding Aliases and labelling until nothing is pending
#
# Runs `./run.sh label` over and over. When the Claude subscription's session
# limit stops a run, sleeps until the reset time the CLI reported (plus a few
# minutes), then carries on. Any other stop — the lock held by a cron run, the
# network down — is retried after a short wait. Output goes to data/label-loop.log.
set -uo pipefail
cd "$(dirname "$0")"

LOG=data/label-loop.log
RETRY=900          # seconds to wait after a stop that is not the usage limit
MARGIN=300         # seconds past the reported reset time
mkdir -p data
say() { echo "$(date +%H:%M:%S) loop: $*" >>"$LOG"; }

say "started (args: $*)"
while true; do
  flock data/.daily.lock true           # wait out a run already going
  start=$(stat -c %s "$LOG")
  ./run.sh label --max 10000 "$@" >>"$LOG" 2>&1
  out=$(tail -c +"$((start + 1))" "$LOG")

  if grep -qE "aliases: (nothing to decide|decided [0-9]+, 0 left)" <<<"$out" &&
     grep -qE "nothing to label|labelled [0-9]+, 0 left" <<<"$out"; then
    say "nothing left to decide or label; done"
    exit 0
  fi

  reset=$(grep -oP "resets \K[^(]+" <<<"$out" | tail -1 | xargs)
  if [ -n "$reset" ] && at=$(TZ=Asia/Taipei date -d "$reset" +%s 2>/dev/null); then
    now=$(date +%s)
    [ "$at" -le "$now" ] && at=$((at + 86400))   # "resets 8am" said after 8am means tomorrow
    wait=$((at - now + MARGIN))
    say "usage limit; sleeping until $(date -d "@$((now + wait))" +%H:%M)"
  else
    wait=$RETRY
    say "run stopped without a usage limit; retrying in $((wait / 60)) min"
  fi
  sleep "$wait"
done
