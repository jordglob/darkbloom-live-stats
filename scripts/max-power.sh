#!/bin/bash
# Deliberate maximum-load run - saturates CPU and GPU so thermal and fan
# behaviour can be watched under real heat rather than waiting for paid
# traffic to happen to produce it. This is what validates a fan-control
# change: apply load, watch whether the fan actually follows temperature.
#
# What it loads:
#   CPU - one busy process per logical core (performance AND efficiency).
#   GPU - a continuous MLX matmul loop through Metal, if MLX is present.
#   ANE - NOT loaded. Reaching the Neural Engine needs a compiled CoreML
#         model; neither coremltools nor Xcode is installed here, and a
#         fake that doesn't actually touch the ANE would be worse than
#         honestly leaving it out.
#   Audio - never touched, deliberately.
#
# IMPORTANT: this competes with real paid inference on the same GPU. It is
# meant to be run knowingly, for a bounded time, not left on.
#
# Everything runs in this script's own process group so the caller can kill
# the whole tree with one signal - see stop_max_power() in dashboard/server.py.

set -uo pipefail

DURATION="${1:-300}"
MLX_PY="${MLX_PY:-/opt/homebrew/bin/python3}"
PIDS=()

cleanup() {
  for p in "${PIDS[@]:-}"; do
    [ -n "$p" ] && kill "$p" 2>/dev/null
  done
  wait 2>/dev/null
  echo "[max-power] stopped"
}
trap cleanup EXIT INT TERM

NCPU="$(sysctl -n hw.logicalcpu)"
echo "[max-power] starting: ${DURATION}s, ${NCPU} CPU workers"

for _ in $(seq "$NCPU"); do
  # Busy loop in the shell itself - no external binary, nothing to install,
  # and it pins a core just as well as anything fancier.
  ( while :; do :; done ) &
  PIDS+=($!)
done

if "$MLX_PY" -c "import mlx.core" 2>/dev/null; then
  echo "[max-power] GPU: MLX found, starting Metal matmul loop"
  "$MLX_PY" - <<'PY' &
import mlx.core as mx
# Large enough to keep the GPU genuinely busy, small enough to stay well
# inside unified memory alongside whatever models are already loaded.
n = 4096
a = mx.random.normal((n, n))
b = mx.random.normal((n, n))
while True:
    c = a @ b
    mx.eval(c)           # force execution; MLX is lazy otherwise
    a = c * 1e-6         # keep values bounded, prevent inf/NaN drift
PY
  PIDS+=($!)
else
  echo "[max-power] GPU: MLX not available at $MLX_PY - CPU only"
fi

sleep "$DURATION"
