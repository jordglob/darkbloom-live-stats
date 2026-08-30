#!/bin/bash
# Continuous loop (runs as a launchd LaunchAgent) that alternates which model
# the darkbloom provider serves. The timer toward the next switch only starts
# once full hardware-trust is achieved (self_signed time doesn't count, since
# no traffic is routed there anyway) and must hold for INTERVAL_SEC (default
# 4h) in a row. If trust is lost mid-period the timer resets - that period
# didn't produce any real serving time to compare against.
#
# Logs requests/tokens for the ending model's period to model-rotate.log so
# you can compare demand between models over time.
#
# EDIT THIS: set MODELS to the model IDs you've downloaded (see `darkbloom
# models catalog`). Only ever ONE of these is loaded at a time - this script
# does not attempt to serve multiple models concurrently.

set -uo pipefail
export LC_ALL=C
: "${HOME:=$(eval echo ~"$(id -un)")}"

DARKBLOOM="$HOME/.darkbloom/bin/darkbloom"
STATE="$HOME/.darkbloom/model-rotate.state"
LOG="$HOME/.darkbloom/model-rotate.log"
MODELS=(gpt-oss-20b qwen3-vl-30b-a3b-instruct)
INTERVAL_SEC=14400   # 4 hours of continuous hardware-trust
POLL_SEC=60

now_iso() { date "+%Y-%m-%dT%H:%M:%S%z" | sed -E 's/([0-9]{2})([0-9]{2})$/\1:\2/'; }
log() { echo "[$(date '+%H:%M:%S')] $*"; }

if [ -f "$STATE" ]; then
  # shellcheck disable=SC1090
  source "$STATE"
fi
: "${CURRENT_MODEL:=${MODELS[0]}}"
: "${TRUST_ACHIEVED_AT:=0}"

save_state() {
  {
    echo "CURRENT_MODEL=$CURRENT_MODEL"
    echo "TRUST_ACHIEVED_AT=$TRUST_ACHIEVED_AT"
  } > "$STATE"
}
save_state

get_trust_level() {
  "$DARKBLOOM" status 2>/dev/null | grep -m1 "^Trust:" | sed -E 's/^Trust:[[:space:]]*([a-z_]+).*/\1/'
}

do_switch() {
  local STATUS REQS TOKENS NEXT_MODEL
  STATUS=$("$DARKBLOOM" status 2>/dev/null || true)
  REQS=$(echo "$STATUS" | grep -o 'Requests served: [0-9]*' | grep -o '[0-9]*' || true)
  TOKENS=$(echo "$STATUS" | grep -oE 'tokens: [0-9]+' | grep -o '[0-9]*' || true)
  REQS=${REQS:-0}
  TOKENS=${TOKENS:-0}

  if [ "$CURRENT_MODEL" = "${MODELS[0]}" ]; then
    NEXT_MODEL="${MODELS[1]}"
  else
    NEXT_MODEL="${MODELS[0]}"
  fi

  [ -f "$LOG" ] || echo "timestamp,ending_model,requests_served,tokens,switching_to,hardware_trust_seconds" > "$LOG"
  echo "$(now_iso),$CURRENT_MODEL,$REQS,$TOKENS,$NEXT_MODEL,$INTERVAL_SEC" >> "$LOG"

  log "Switching from $CURRENT_MODEL (reqs=$REQS tokens=$TOKENS, ${INTERVAL_SEC}s hardware-trust reached) to $NEXT_MODEL"

  "$DARKBLOOM" stop 2>&1 || true
  sleep 3
  "$DARKBLOOM" start --model "$NEXT_MODEL" --local-endpoint 2>&1

  CURRENT_MODEL="$NEXT_MODEL"
  TRUST_ACHIEVED_AT=0
  save_state
}

log "Starting model-rotate. Active model: $CURRENT_MODEL. Requires ${INTERVAL_SEC}s of continuous hardware-trust to switch."

while true; do
  TRUST=$(get_trust_level)
  NOW=$(date +%s)

  if [ "$TRUST" = "hardware" ]; then
    if [ "$TRUST_ACHIEVED_AT" = "0" ]; then
      TRUST_ACHIEVED_AT=$NOW
      save_state
      log "Hardware-trust reached for $CURRENT_MODEL - timer toward ${INTERVAL_SEC}s starts now"
    else
      ELAPSED=$((NOW - TRUST_ACHIEVED_AT))
      if [ "$ELAPSED" -ge "$INTERVAL_SEC" ]; then
        do_switch
      fi
    fi
  else
    if [ "$TRUST_ACHIEVED_AT" != "0" ]; then
      log "Trust lost (now: ${TRUST:-unknown}) - timer reset, will restart once hardware-trust is regained"
      TRUST_ACHIEVED_AT=0
      save_state
    fi
  fi

  sleep "$POLL_SEC"
done
