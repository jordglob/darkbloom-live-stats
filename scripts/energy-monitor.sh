#!/bin/bash
# Tracks electricity cost vs. estimated Darkbloom revenue over time.
# Reads power data from /tmp/darkbloom-pm-raw.log (written by pm-start.sh,
# which needs sudo - see README). Does NOT need sudo itself. Logs to
# ~/.darkbloom/energy-log.csv every 5 minutes.
#
# ELECTRICITY PRICE: defaults to Sweden's free spot-price API
# (elprisetjustnu.se, zone SE3). If you're elsewhere, set FIXED_PRICE_PER_KWH
# below to a flat rate in your own currency instead - the rest of the script
# doesn't care about currency, it just needs a number per kWh.

set -uo pipefail
export LC_ALL=C  # force period as decimal separator (some locales use comma, which bc/awk can't parse)
: "${HOME:=$(eval echo ~"$(id -un)")}"

RAW_LOG="/tmp/darkbloom-pm-raw.log"
DIR="$HOME/.darkbloom"
CSV="$DIR/energy-log.csv"
STATE="$DIR/energy-monitor.state"
ZONE="SE3"                 # Swedish price zone - only relevant if FIXED_PRICE_PER_KWH is empty
FIXED_PRICE_PER_KWH=""     # e.g. "0.30" to skip the Swedish API entirely and use a flat rate
INTERVAL=300
DARKBLOOM="$HOME/.darkbloom/bin/darkbloom"
# powermetrics cpu_power/gpu_power only measures the SoC's own power rails, not
# the whole machine's wall power (RAM, storage, networking/fans, PSU losses are
# not counted). BASELINE_W is a constant estimate for everything else - set it
# to your own measurement if you have a smart plug, otherwise a reasonable
# guess for a Mac mini/Studio at idle-ish load.
BASELINE_W=7
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
[ -f "$CSV" ] || echo "timestamp,avg_power_w,interval_wh,cum_wh,elpris_sek_kwh,interval_cost_sek,cum_cost_sek,usd_sek,requests_served,tokens,est_revenue_usd_approx,est_revenue_sek_approx,net_sek_approx,total_power_w,avg_gpu_active_pct" > "$CSV"

LAST_OFFSET=0
CUM_WH=0
CUM_COST=0
ELPRIS_TS=0
ELPRIS_VAL=0
USDSEK_TS=0
USDSEK_VAL=10

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
[ -z "$CUM_WH" ] && CUM_WH=0
[ -z "$CUM_COST" ] && CUM_COST=0
[ -z "$ELPRIS_VAL" ] && ELPRIS_VAL=0
[ -z "$USDSEK_VAL" ] && USDSEK_VAL=10

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

  # --- electricity price, cached 15 min ---
  if [ -n "$FIXED_PRICE_PER_KWH" ]; then
    ELPRIS_VAL="$FIXED_PRICE_PER_KWH"
    ELPRIS_TS=$NOW
  elif [ $((NOW - ELPRIS_TS)) -gt 900 ]; then
    RESP=$(curl -s --max-time 10 "https://www.elprisetjustnu.se/api/v1/prices/$(date +%Y)/$(date +%m)-$(date +%d)_${ZONE}.json" || true)
    NEWVAL=$(echo "$RESP" | jq -r --arg now "$NOW_ISO" '[.[] | select(.time_start <= $now and .time_end > $now)][0].SEK_per_kWh // empty' 2>/dev/null || true)
    if [ -n "${NEWVAL:-}" ]; then
      ELPRIS_VAL=$NEWVAL
      ELPRIS_TS=$NOW
    else
      log "WARNING: could not fetch electricity price right now, keeping last value ($ELPRIS_VAL per kWh)"
    fi
  fi

  # --- usd/sek, cached 1h (only meaningful if you're actually tracking SEK) ---
  if [ $((NOW - USDSEK_TS)) -gt 3600 ]; then
    NEWRATE=$(curl -sL --max-time 10 "https://api.frankfurter.app/latest?from=USD&to=SEK" | jq -r '.rates.SEK // empty' 2>/dev/null || true)
    if [ -n "${NEWRATE:-}" ]; then
      USDSEK_VAL=$NEWRATE
      USDSEK_TS=$NOW
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

  TOTAL_W=$(calc "scale=3; $AVG_W + $BASELINE_W")
  INTERVAL_WH=$(calc "scale=6; $TOTAL_W * $INTERVAL / 3600")
  CUM_WH=$(calc "scale=6; $CUM_WH + $INTERVAL_WH")
  INTERVAL_COST=$(calc "scale=6; ($INTERVAL_WH/1000) * $ELPRIS_VAL")
  CUM_COST=$(calc "scale=6; $CUM_COST + $INTERVAL_COST")

  STATUS=$("$DARKBLOOM" status 2>/dev/null || true)
  REQS=$(echo "$STATUS" | grep -o 'Requests served: [0-9]*' | grep -o '[0-9]*' || true)
  TOKENS=$(echo "$STATUS" | grep -oE 'tokens: [0-9]+' | grep -o '[0-9]*' || true)
  REQS=${REQS:-0}
  TOKENS=${TOKENS:-0}

  EST_REV_USD=$(calc "scale=6; $TOKENS * $BLENDED_USD_PER_TOKEN")
  EST_REV_SEK=$(calc "scale=6; $EST_REV_USD * $USDSEK_VAL")
  NET_SEK=$(calc "scale=6; $EST_REV_SEK - $CUM_COST")

  echo "$NOW_ISO,$AVG_W,$INTERVAL_WH,$CUM_WH,$ELPRIS_VAL,$INTERVAL_COST,$CUM_COST,$USDSEK_VAL,$REQS,$TOKENS,$EST_REV_USD,$EST_REV_SEK,$NET_SEK,$TOTAL_W,$AVG_GPU_ACTIVE_PCT" >> "$CSV"

  {
    echo "LAST_OFFSET=$LAST_OFFSET"
    echo "CUM_WH=$CUM_WH"
    echo "CUM_COST=$CUM_COST"
    echo "ELPRIS_TS=$ELPRIS_TS"
    echo "ELPRIS_VAL=$ELPRIS_VAL"
    echo "USDSEK_TS=$USDSEK_TS"
    echo "USDSEK_VAL=$USDSEK_VAL"
  } > "$STATE"

  log "SoC=${AVG_W}W (+baseline ${BASELINE_W}W = ${TOTAL_W}W)  cumulative energy=${CUM_WH}Wh  electricity cost=${CUM_COST}  ~revenue=${EST_REV_SEK} SEK  net=${NET_SEK} SEK"

  sleep "$INTERVAL"
done
