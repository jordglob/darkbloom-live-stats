#!/bin/bash
# Tracks electricity cost vs. estimated Darkbloom revenue over time.
# Reads power data from /tmp/darkbloom-pm-raw.log (written by pm-start.sh,
# which needs sudo - see README). Does NOT need sudo itself. Logs to
# ~/.darkbloom/energy-log.csv every 5 minutes.
#
# ELECTRICITY PRICE: comes from the dashboard server's single configured price
# source (GET /api/price_now) - pick your country/zone/currency or a flat rate
# in the dashboard's Electricity Price panel, nothing to edit here. The CSV
# columns are still named elpris_sek_kwh / usd_sek for history compatibility,
# but they hold "price per kWh in your configured currency" and "units of that
# currency per 1 USD".
#
# POWER: if `macmon` (brew install macmon) is on PATH, whole-system power is
# read from the Mac's own SMC sensor (covers RAM, SSD, fans, everything - not
# just CPU+GPU), divided by PSU_EFFICIENCY to approximate wall draw. Without
# it, falls back to powermetrics CPU+GPU plus a flat BASELINE_W guess.

set -uo pipefail
export LC_ALL=C  # force period as decimal separator (some locales use comma, which bc/awk can't parse)
: "${HOME:=$(eval echo ~"$(id -un)")}"
# launchd starts us with a minimal PATH; Homebrew tools (jq, macmon) live here.
export PATH="$PATH:/opt/homebrew/bin:/usr/local/bin"

RAW_LOG="/tmp/darkbloom-pm-raw.log"
DIR="$HOME/.darkbloom"
CSV="$DIR/energy-log.csv"
STATE="$DIR/energy-monitor.state"
DASHBOARD_URL="http://127.0.0.1:8787"
INTERVAL=300
DARKBLOOM="$HOME/.darkbloom/bin/darkbloom"
# powermetrics cpu_power/gpu_power only measures the SoC's own power rails, not
# the whole machine's wall power (RAM, storage, networking/fans, PSU losses are
# not counted). BASELINE_W is the fallback constant for everything else when
# macmon isn't available. Measured on an M4 Pro Mac mini: the real gap is
# ~5W at idle and grows under load (RAM + fans), so this is a rough middle.
BASELINE_W=7
# SMC's system-power sensor sits after the power supply, so wall draw is a bit
# higher. ~90% is typical for Apple's small-form-factor PSUs at these loads;
# a smart plug is the only way to pin this down for your own unit.
PSU_EFFICIENCY=0.90
MACMON="$(command -v macmon 2>/dev/null || true)"
MACMON_LOG="$DIR/macmon.jsonl"
MACMON_PID=""
# Rough revenue estimate. Originally a 50/50 blend of quoted alpha pricing
# ($0.05/M input + $0.20/M output = $0.125/M), but that never matched real
# ledger data even after correcting for the real ~2:1 prompt:completion mix
# (which alone would predict ~$0.097/M, not the ~$0.044/M actually paid) -
# either that pricing is stale or it's the customer-facing rate, not the
# per-token payout providers see. Recalibrated 2026-09-02 straight from real
# ledger data instead (dashboard's server.py LOCAL_BLENDED_USD_PER_TOKEN -
# keep these two in sync manually).
# LAST CALIBRATED 2026-09-10: $0.048/M, same method but excluding jobs
# matching the exact-25-prompt-token spam signature Darkbloom's team
# confirmed and began rate-limiting that day - recalibrate periodically,
# don't trust indefinitely (the $0.125 guess drifted 2.8x stale unnoticed).
BLENDED_USD_PER_TOKEN=$(echo "scale=12; 0.048/1000000" | bc 2>/dev/null)
[ -z "$BLENDED_USD_PER_TOKEN" ] && BLENDED_USD_PER_TOKEN=0.000000048

mkdir -p "$DIR"
[ -f "$CSV" ] || echo "timestamp,avg_power_w,interval_wh,cum_wh,elpris_sek_kwh,interval_cost_sek,cum_cost_sek,usd_sek,requests_served,tokens,est_revenue_usd_approx,est_revenue_sek_approx,net_sek_approx,total_power_w,avg_gpu_active_pct,power_method" > "$CSV"

# Whole-system power sampler: macmon reads the SMC every 5s into a JSONL log
# this script averages per interval. Runs as our child (no sudo), dies with us.
start_macmon() {
  [ -n "$MACMON" ] || return 0
  : > "$MACMON_LOG"
  "$MACMON" pipe -s 0 -i 5000 >> "$MACMON_LOG" 2>/dev/null &
  MACMON_PID=$!
}
cleanup() { [ -n "$MACMON_PID" ] && kill "$MACMON_PID" 2>/dev/null; }
trap cleanup EXIT INT TERM
start_macmon
MACMON_OFFSET=0

LAST_OFFSET=0
CUM_WH=0
CUM_COST=0
ELPRIS_TS=0
ELPRIS_VAL=0
USDSEK_TS=0
USDSEK_VAL=10
LAST_TOKENS=0
CUM_TOKENS=0

if [ -f "$STATE" ]; then
  # shellcheck disable=SC1090
  source "$STATE"
fi
# Self-heal from a past bc error that could have left empty values in the state file
: "${LAST_OFFSET:=0}"
: "${CUM_WH:=0}"
: "${CUM_COST:=0}"
: "${ELPRIS_TS:=0}"
: "${ELPRIS_VAL:=0}"
: "${USDSEK_TS:=0}"
: "${USDSEK_VAL:=10}"
: "${LAST_TOKENS:=0}"
: "${CUM_TOKENS:=0}"
[ -z "$CUM_WH" ] && CUM_WH=0
[ -z "$CUM_COST" ] && CUM_COST=0
[ -z "$ELPRIS_VAL" ] && ELPRIS_VAL=0
[ -z "$USDSEK_VAL" ] && USDSEK_VAL=10
[ -z "$LAST_TOKENS" ] && LAST_TOKENS=0
[ -z "$CUM_TOKENS" ] && CUM_TOKENS=0

log() { echo "[$(date '+%H:%M:%S')] $*"; }
# bc wrapper: falls back to 0 if bc ever returns empty, so a single glitch
# can never get stuck and propagate forever.
calc() {
  local r
  r=$(echo "$1" | bc 2>/dev/null)
  echo "${r:-0}"
}

log "Starting energy-monitor. CSV: $CSV"
log "Note: waiting for power data from $RAW_LOG (run pm-start.sh with sudo separately to fill it)."

while true; do
  NOW=$(date +%s)
  NOW_ISO=$(date "+%Y-%m-%dT%H:%M:%S%z" | sed -E 's/([0-9]{2})([0-9]{2})$/\1:\2/')

  # --- rotate the root-owned raw log if it's grown large. We don't own the file
  # and can't truncate/write it ourselves - but powermetrics -o TRUNCATES its
  # target file on start, so restarting it via launchctl (no sudo needed for
  # that, it's our own launchd job) rotates it effectively. ---
  RAW_LOG_MAX_BYTES=104857600  # 100MB
  if [ -f "$RAW_LOG" ]; then
    RAW_SIZE=$(stat -f%z "$RAW_LOG" 2>/dev/null || echo 0)
    if [ "$RAW_SIZE" -gt "$RAW_LOG_MAX_BYTES" ]; then
      log "Raw power log is ${RAW_SIZE} bytes (>${RAW_LOG_MAX_BYTES}) - restarting powermetrics to rotate it"
      launchctl kickstart -k "gui/$(id -u)/io.darkbloom.powermetrics" 2>/dev/null || true
      LAST_OFFSET=0
    fi
  fi

  # --- electricity price + exchange rate, one call to the dashboard's configured
  # source, cached 15 min. The dashboard owns which country/zone/currency is
  # in use so this script and the charts can never disagree. ---
  if [ $((NOW - ELPRIS_TS)) -gt 900 ]; then
    RESP=$(curl -s --max-time 10 "$DASHBOARD_URL/api/price_now" || true)
    NEWVAL=$(echo "$RESP" | jq -r '.price_per_kwh // empty' 2>/dev/null || true)
    NEWRATE=$(echo "$RESP" | jq -r '.local_per_usd // empty' 2>/dev/null || true)
    if [ -n "${NEWVAL:-}" ]; then
      ELPRIS_VAL=$NEWVAL
      ELPRIS_TS=$NOW
      if [ -n "${NEWRATE:-}" ]; then
        USDSEK_VAL=$NEWRATE
        USDSEK_TS=$NOW
      fi
    else
      log "WARNING: could not get the electricity price from the dashboard at $DASHBOARD_URL (is it running?) - keeping last value ($ELPRIS_VAL per kWh)"
    fi
  fi

  # --- parse new power data since last run ---
  AVG_W=0
  AVG_GPU_ACTIVE_PCT=""
  if [ -f "$RAW_LOG" ]; then
    SIZE=$(stat -f%z "$RAW_LOG" 2>/dev/null || echo 0)
    # Self-heal: if the raw log got recreated/truncated (e.g. powermetrics
    # restarted), LAST_OFFSET can end up bigger than the file ever was again,
    # which silently stalls parsing at 0W forever. Catch up instead of stalling.
    if [ "$LAST_OFFSET" -gt "$SIZE" ]; then
      LAST_OFFSET=0
    fi
    if [ "$SIZE" -gt "$LAST_OFFSET" ]; then
      CHUNK=$(tail -c +$((LAST_OFFSET + 1)) "$RAW_LOG")
      AVG_MW=$(printf '%s\n' "$CHUNK" | awk '
        /^CPU Power:/ { cpu=$3 }
        /^GPU Power:/ { gpu=$3; print cpu+gpu; cpu=0; gpu=0 }
      ' | awk '{s+=$1; n++} END{ if (n>0) print s/n; else print 0 }')
      # Real GPU busy-ness (0-100%), averaged over every sample in this
      # 5-minute window - same "GPU HW active residency" line the dashboard's
      # live gauge reads, just averaged here instead of a single snapshot.
      # Feeds the Utilization (last hour) gauge in the dashboard.
      AVG_GPU_ACTIVE_PCT=$(printf '%s\n' "$CHUNK" | awk '
        /^GPU HW active residency:/ { gsub("%", "", $5); print $5 }
      ' | awk '{s+=$1; n++} END{ if (n>0) printf "%.1f", s/n; else print "" }')
      LAST_OFFSET=$SIZE
      AVG_W=$(calc "scale=3; ${AVG_MW:-0} / 1000")
    fi
  fi

  # --- whole-system power from macmon's SMC log for this interval, if we have
  # it; otherwise SoC + baseline guess. Restart macmon if it died. ---
  POWER_METHOD="soc+baseline"
  TOTAL_W=$(calc "scale=3; $AVG_W + $BASELINE_W")
  if [ -n "$MACMON" ]; then
    if [ -n "$MACMON_PID" ] && ! kill -0 "$MACMON_PID" 2>/dev/null; then
      log "macmon exited - restarting it"
      start_macmon
      MACMON_OFFSET=0
    fi
    if [ -f "$MACMON_LOG" ]; then
      MSIZE=$(stat -f%z "$MACMON_LOG" 2>/dev/null || echo 0)
      [ "$MACMON_OFFSET" -gt "$MSIZE" ] && MACMON_OFFSET=0
      if [ "$MSIZE" -gt "$MACMON_OFFSET" ]; then
        # macmon 0.7 reports sys_power == all_power exactly when the SMC read
        # fails for a sample - drop those rather than average them in.
        SYS_W=$(tail -c +$((MACMON_OFFSET + 1)) "$MACMON_LOG" | jq -s '[.[] | select(.sys_power != null and .all_power != null and ((.sys_power - .all_power) | fabs) > 0.01) | .sys_power] | if length > 0 then add / length else empty end' 2>/dev/null || true)
        MACMON_OFFSET=$MSIZE
        if [ -n "${SYS_W:-}" ]; then
          TOTAL_W=$(calc "scale=3; $SYS_W / $PSU_EFFICIENCY")
          POWER_METHOD="smc"
        fi
      fi
      # keep the log from growing forever: it's only ever read incrementally
      if [ "$MSIZE" -gt 20000000 ]; then
        cleanup; start_macmon; MACMON_OFFSET=0
      fi
    fi
  fi
  INTERVAL_WH=$(calc "scale=6; $TOTAL_W * $INTERVAL / 3600")
  CUM_WH=$(calc "scale=6; $CUM_WH + $INTERVAL_WH")
  INTERVAL_COST=$(calc "scale=6; ($INTERVAL_WH/1000) * $ELPRIS_VAL")
  CUM_COST=$(calc "scale=6; $CUM_COST + $INTERVAL_COST")

  STATUS=$("$DARKBLOOM" status 2>/dev/null || true)
  REQS=$(echo "$STATUS" | grep -o 'Requests served: [0-9]*' | grep -o '[0-9]*' || true)
  TOKENS=$(echo "$STATUS" | grep -oE 'tokens: [0-9]+' | grep -o '[0-9]*' || true)
  REQS=${REQS:-0}
  TOKENS=${TOKENS:-0}

  # $TOKENS is the darkbloom daemon's own lifetime counter, which resets to 0
  # every time the daemon itself restarts (independent of this script's own
  # uptime) - multiplying it directly gave a revenue figure that silently
  # dropped back near $0 on every daemon restart while CUM_COST above kept
  # climbing, making "cumulative" cost vs. revenue compare two different time
  # windows. CUM_TOKENS tracks the real lifetime total the same way CUM_WH
  # does: add the delta each poll, and if TOKENS has gone backwards (daemon
  # restarted since the last poll) treat the whole new value as newly earned
  # rather than losing it.
  if [ "$TOKENS" -lt "$LAST_TOKENS" ]; then
    TOKEN_DELTA=$TOKENS
  else
    TOKEN_DELTA=$(calc "$TOKENS - $LAST_TOKENS")
  fi
  CUM_TOKENS=$(calc "scale=6; $CUM_TOKENS + $TOKEN_DELTA")
  LAST_TOKENS=$TOKENS

  EST_REV_USD=$(calc "scale=6; $CUM_TOKENS * $BLENDED_USD_PER_TOKEN")
  EST_REV_SEK=$(calc "scale=6; $EST_REV_USD * $USDSEK_VAL")
  NET_SEK=$(calc "scale=6; $EST_REV_SEK - $CUM_COST")

  echo "$NOW_ISO,$AVG_W,$INTERVAL_WH,$CUM_WH,$ELPRIS_VAL,$INTERVAL_COST,$CUM_COST,$USDSEK_VAL,$REQS,$TOKENS,$EST_REV_USD,$EST_REV_SEK,$NET_SEK,$TOTAL_W,$AVG_GPU_ACTIVE_PCT,$POWER_METHOD" >> "$CSV"

  {
    echo "LAST_OFFSET=$LAST_OFFSET"
    echo "CUM_WH=$CUM_WH"
    echo "CUM_COST=$CUM_COST"
    echo "ELPRIS_TS=$ELPRIS_TS"
    echo "ELPRIS_VAL=$ELPRIS_VAL"
    echo "USDSEK_TS=$USDSEK_TS"
    echo "USDSEK_VAL=$USDSEK_VAL"
    echo "LAST_TOKENS=$LAST_TOKENS"
    echo "CUM_TOKENS=$CUM_TOKENS"
  } > "$STATE"

  if [ "$POWER_METHOD" = "smc" ]; then
    log "SoC=${AVG_W}W  whole-system (SMC/${PSU_EFFICIENCY} PSU)=${TOTAL_W}W  cumulative energy=${CUM_WH}Wh  electricity cost=${CUM_COST}  ~revenue=${EST_REV_SEK}  net=${NET_SEK} (local currency)"
  else
    log "SoC=${AVG_W}W (+baseline ${BASELINE_W}W = ${TOTAL_W}W)  cumulative energy=${CUM_WH}Wh  electricity cost=${CUM_COST}  ~revenue=${EST_REV_SEK}  net=${NET_SEK} (local currency)"
  fi

  sleep "$INTERVAL"
done
