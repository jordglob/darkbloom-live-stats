#!/bin/bash
# Started by the io.darkbloom.powermetrics LaunchAgent. Samples CPU+GPU power
# into the raw log energy-monitor.sh and the dashboard read.
#
# The interval comes from ~/.darkbloom/pm-interval-ms, which the dashboard
# sets: 200 ms while the page is open (the live gauges move with it), 1000 ms
# otherwise. At 200 ms around the clock powermetrics used ~11% of a CPU core
# and wrote ~70 MB an hour for nobody to look at. The dashboard restarts this
# job (launchctl kickstart -k) when it changes the value.
INTERVAL_FILE="$HOME/.darkbloom/pm-interval-ms"
INTERVAL=$(cat "$INTERVAL_FILE" 2>/dev/null)
case "$INTERVAL" in ''|*[!0-9]*) INTERVAL=1000 ;; esac
exec /usr/bin/sudo -n /usr/bin/powermetrics --samplers cpu_power,gpu_power -i "$INTERVAL" -o /tmp/darkbloom-pm-raw.log
