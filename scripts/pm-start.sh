#!/bin/bash
# Starts continuous CPU+GPU power sampling to a raw log file that
# energy-monitor.sh reads from.
#
# This script is only needed if you're running things manually / without the
# sudoers setup described in the README. If you've installed the scoped
# sudoers rule (see README "Enabling electricity-cost tracking" section), use the
# io.darkbloom.powermetrics LaunchAgent instead - it does the same thing
# automatically at login, with no manual step.
#
# Manual usage: needs sudo (one password prompt), then runs in the foreground
# until you Ctrl-C it, or in the background via:
#   nohup ./pm-start.sh > /tmp/darkbloom-pm-start.out 2>&1 &

RAW_LOG="/tmp/darkbloom-pm-raw.log"

echo "Starting powermetrics, logging to $RAW_LOG (every 60s)..."
exec sudo powermetrics --samplers cpu_power,gpu_power -i 60000 -o "$RAW_LOG"
