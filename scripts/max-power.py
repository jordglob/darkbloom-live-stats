#!/usr/bin/env python3
"""Proportional CPU + GPU load, steerable while running.

Reads a target percentage (0-100) from a level file every cycle, so a slider
can move the load up and down live without restarting anything. Each worker
duty-cycles inside a fixed period: busy for level% of it, asleep for the rest.
That gives a smooth 0-100 rather than the coarse steps you'd get from varying
the number of workers.

What it loads:
  CPU   - one worker per logical core, duty-cycled.
  GPU   - an MLX matmul loop through Metal, duty-cycled the same way. Only if
          MLX is importable by whichever interpreter runs this.
  ANE   - not loaded. Reaching the Neural Engine needs a compiled CoreML
          model; a fake that never touches it would be worse than its absence.
  Audio - never touched, deliberately.

Exits on its own when the level reaches 0, when the level file disappears, or
when the hard duration cap expires - so it can't outlive the thing steering it.

Usage: max-power.py <level-file> [max-seconds]
"""
import multiprocessing
import os
import sys
import time
from pathlib import Path

PERIOD = 0.1          # duty-cycle window
LEVEL_POLL_SEC = 0.5  # how often a worker re-reads the target


def read_level(path):
    try:
        return max(0.0, min(100.0, float(path.read_text().strip())))
    except Exception:
        return 0.0


def _run(path, deadline, gpu):
    level, checked = 0.0, 0.0
    mx = a = b = None
    if gpu:
        try:
            import mlx.core as mx_
            mx = mx_
            n = 4096
            a = mx.random.normal((n, n))
            b = mx.random.normal((n, n))
        except Exception:
            mx = None

    while time.time() < deadline:
        now = time.time()
        if now - checked >= LEVEL_POLL_SEC:
            if not path.exists():
                return
            level = read_level(path)
            checked = now
        if level <= 0:
            return
        frac = level / 100.0

        if mx is not None:
            # One matmul is the smallest unit of GPU work here; throttle by
            # sleeping proportionally after it rather than by shrinking it,
            # so the GPU still reaches full occupancy at 100%.
            t0 = time.time()
            c = a @ b
            mx.eval(c)
            a = c * 1e-6  # keep values bounded, prevent inf/NaN drift
            busy = time.time() - t0
            if frac < 1.0:
                time.sleep(busy * (1.0 - frac) / max(frac, 0.01))
        else:
            end = now + PERIOD * frac
            while time.time() < end:
                pass
            time.sleep(PERIOD * (1.0 - frac))


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    path = Path(sys.argv[1])
    max_seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 1800.0
    deadline = time.time() + max_seconds

    try:
        import mlx.core  # noqa: F401
        has_gpu = True
    except Exception:
        has_gpu = False

    ncpu = os.cpu_count() or 4
    print(f"[max-power] {ncpu} CPU workers, GPU {'on' if has_gpu else 'unavailable'}, cap {max_seconds:.0f}s", flush=True)

    procs = [multiprocessing.Process(target=_run, args=(path, deadline, False), daemon=True)
             for _ in range(ncpu)]
    if has_gpu:
        procs.append(multiprocessing.Process(target=_run, args=(path, deadline, True), daemon=True))
    for p in procs:
        p.start()
    try:
        for p in procs:
            p.join()
    except KeyboardInterrupt:
        pass
    print("[max-power] stopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
