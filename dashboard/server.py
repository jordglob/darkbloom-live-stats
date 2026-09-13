#!/usr/bin/env python3
"""Local live dashboard for the Darkbloom provider: status + energy/electricity cost.
Reads ~/.darkbloom/energy-log.csv and runs `darkbloom status` on demand.
Binds to 127.0.0.1 (this machine only) on port 8787.
"""
import csv
import http.server
import json
import plistlib
import re
import socketserver
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path.home()
CSV_PATH = HOME / ".darkbloom" / "energy-log.csv"
DARKBLOOM_BIN = HOME / ".darkbloom" / "bin" / "darkbloom"
RAW_POWER_LOG = Path("/tmp/darkbloom-pm-raw.log")
PORT = 8787
MAX_POINTS = 300  # downsample if the log grows large
# Same baseline assumption as energy-monitor.sh: powermetrics cpu_power/gpu_power
# only measures the SoC's own power rails, not the whole machine's wall power
# (RAM, storage, networking/fans, PSU losses aren't counted). Keep this in sync
# with BASELINE_W over there.
BASELINE_W = 7
# Same flat-rate guess as energy-monitor.sh's BLENDED_USD_PER_TOKEN - kept in sync
# manually. Used only to show what our own naive local formula WOULD have guessed,
# so it can be compared against what Darkbloom's real ledger actually paid.
# Recalibrated 2026-09-02 from this account's real ledger (gpt-oss-20b and
# gemma-4-26b real rates both landed within ~2% of each other around
# $0.044/M tokens, despite very different model sizes - a stable blended
# network rate, not noise). Previous guess of $0.125/M was never measured
# and was off by ~2.8x, which is what most of the old "gap" number turned
# out to be.
# LAST CALIBRATED 2026-09-10: $0.048/M, computed the same way but excluding
# jobs matching the exact-25-prompt-token spam signature Darkbloom's team
# confirmed and began rate-limiting that day (see nerdy_stats.spam_jobs) -
# spam jobs pay real (near-$0) money for real tokens, so leaving them in
# would have dragged this constant down again. A real-world number like this
# will drift as network pricing/mix shifts - recalibrate periodically rather
# than trusting it indefinitely, the same way the $0.125 guess quietly went
# 2.8x stale before anyone checked it against reality.
LOCAL_BLENDED_USD_PER_TOKEN = 0.048 / 1_000_000
LIVE_POWER_TAIL_BYTES = 40000  # a few complete samples of lookback at ~5Hz (~5-8KB/sample)
LOCAL_JSON = HOME / ".darkbloom" / "local.json"
WARMUP_CONFIG = HOME / ".darkbloom" / "warmup.json"
WARMUP_LOG = HOME / ".darkbloom" / "warmup.log"
WARMUP_DEFAULT_INTERVAL_MIN = 20  # same cadence as SplittyDev/darkbloom-dashboard
DAEMON_STATE_PATH = HOME / ".darkbloom" / "daemon-state.json"
DOCTOR_POLL_INTERVAL_SEC = 300  # doctor makes a real network call + ~2 dozen checks - too heavy for the 10s cadence
PROVIDER_PLIST = HOME / "Library" / "LaunchAgents" / "io.darkbloom.provider.plist"
PRICE_GUARD_CONFIG = HOME / ".darkbloom" / "price-guard.json"
PRICE_GUARD_LOG = HOME / ".darkbloom" / "price-guard.log"
PRICE_GUARD_POLL_INTERVAL_SEC = 300  # matches the account/energy poll cadence elsewhere

# Real account data pulled directly from Darkbloom's own API, using the same
# device token the `darkbloom` CLI already stores locally after `darkbloom
# login` - we never touch or store any credential ourselves, just read the
# same file the CLI reads. No browser step needed.
AUTH_TOKEN_PATH = HOME / ".darkbloom" / "auth_token"
ACCOUNT_API_URL = "https://api.darkbloom.dev/v1/provider/account-earnings"
# 1000 is a real server-side cap, not a choice we made - confirmed empirically
# (limit=5000 still returns exactly 1000) and there's no working pagination
# either (offset/before_id/cursor/page params are all silently ignored,
# every one returns the identical most-recent-1000 regardless). On a busy
# day that single-call window can be under 4 hours - see EARNINGS_HISTORY_*
# below for how we get further back than that.
ACCOUNT_API_LIMIT = 1000
ACCOUNT_POLL_INTERVAL_SEC = 30  # how often we actually hit Darkbloom's API
EARNINGS_HISTORY_PATH = HOME / ".darkbloom" / "earnings-history.jsonl"
EARNINGS_HISTORY_MAX_AGE_SEC = 48 * 3600  # keep 2 days locally, margin over the ~36h target

SAMPLE_HEADER_RE = re.compile(r"\*\*\* Sampled system activity \((.+?)\) \((.+?)\) \*\*\*")
GPU_ACTIVE_RESIDENCY_RE = re.compile(r"^GPU HW active residency:\s*([\d.]+)%")


def get_live_power():
    """Latest SINGLE CPU/GPU sample from the raw log (not the 5-min average)."""
    if not RAW_POWER_LOG.exists():
        return None
    try:
        size = RAW_POWER_LOG.stat().st_size
        with open(RAW_POWER_LOG, "rb") as f:
            f.seek(max(0, size - LIVE_POWER_TAIL_BYTES))
            chunk = f.read().decode("utf-8", errors="ignore")
    except Exception:
        return None

    cpu_mw = None
    gpu_mw = None
    gpu_active_pct = None
    sample_time = None
    for line in chunk.splitlines():
        m = SAMPLE_HEADER_RE.search(line)
        if m:
            sample_time = m.group(1)
            continue
        if line.startswith("CPU Power:"):
            try:
                cpu_mw = float(line.split()[2])
            except (IndexError, ValueError):
                pass
        elif line.startswith("GPU Power:"):
            try:
                gpu_mw = float(line.split()[2])
            except (IndexError, ValueError):
                pass
        elif line.startswith("GPU HW active residency:"):
            m2 = GPU_ACTIVE_RESIDENCY_RE.match(line)
            if m2:
                try:
                    gpu_active_pct = float(m2.group(1))
                except ValueError:
                    pass

    if cpu_mw is None or gpu_mw is None:
        return None
    soc_w = (cpu_mw + gpu_mw) / 1000
    return {
        "cpu_w": round(cpu_mw / 1000, 3),
        "gpu_w": round(gpu_mw / 1000, 3),
        "total_w": round(soc_w + BASELINE_W, 3),  # incl. estimated system baseline
        "baseline_w": BASELINE_W,
        "sample_time": sample_time,
        # GPU busy-ness (0-100%), read directly from powermetrics' own "GPU HW
        # active residency" line - a real measurement, not derived/estimated
        # from power draw. Optional/non-fatal unlike cpu_mw/gpu_mw above: a
        # missing residency line just leaves these None instead of failing
        # the whole function, since callers that only need cpu_w/gpu_w/total_w
        # shouldn't break if this section is ever absent.
        "gpu_active_pct": round(gpu_active_pct, 1) if gpu_active_pct is not None else None,
        "headroom_pct": round(100 - gpu_active_pct, 1) if gpu_active_pct is not None else None,
    }

UTIL_WINDOW_MIN = 60  # how far back "recent" looks


def get_utilization():
    """Real request/token throughput over the last ~60 min, plus real
    hardware utilization over the same window - the actual average of
    energy-monitor.sh's own per-5-min "GPU HW active residency" logging
    (same real powermetrics figure the live GPU Headroom gauge reads, just
    averaged across many samples instead of one snapshot). Replaced an
    earlier version of this gauge that used a request-arrival duty cycle (%
    of 5-min slices that saw a new request) as a utilization proxy - real
    measured GPU load is what was actually asked for, and duty cycle can't
    tell a Mac serving one tiny request per slice from one pegged at 100%
    for the whole slice. That duty-cycle number was itself a deliberate
    replacement for an even earlier SoC power-draw threshold, which was
    unreliable for the same reason get_live_power()'s gpu_active_pct exists
    at all: idle background power floats non-zero and isn't a clean signal.
    gpu_util_avg_pct is None until energy-monitor.sh has logged at least one
    row with real residency data - the column didn't exist before this was
    added, so historical coverage starts from whenever this shipped, not
    retroactively."""
    if not CSV_PATH.exists():
        return None
    try:
        size = CSV_PATH.stat().st_size
        with open(CSV_PATH, "rb") as f:
            f.seek(max(0, size - 6000))
            chunk = f.read().decode("utf-8", errors="ignore")
        now = time.time()
        rows = []
        for parts in csv.reader(l for l in chunk.splitlines() if l and not l.startswith("timestamp")):
            if len(parts) < 10:
                continue
            try:
                ts = time.mktime(time.strptime(parts[0][:19], "%Y-%m-%dT%H:%M:%S"))
                gpu_pct = None
                if len(parts) > 14 and parts[14].strip():
                    try:
                        gpu_pct = float(parts[14])
                    except ValueError:
                        gpu_pct = None
                rows.append((ts, int(parts[8]), int(parts[9]), gpu_pct))
            except Exception:
                continue
        rows = [r for r in rows if now - r[0] <= UTIL_WINDOW_MIN * 60]
    except Exception:
        return None

    if len(rows) < 2:
        return None

    span_hr = (rows[-1][0] - rows[0][0]) / 3600
    # requests_served/tokens are the darkbloom daemon's own lifetime counters,
    # which reset to 0 whenever the daemon restarts - a plain last-minus-first
    # over the window goes negative right after a restart (same bug class as
    # the energy-monitor.sh revenue fix). Sum consecutive deltas instead, and
    # treat any decrease as a reset where the whole new value was newly
    # earned, rather than losing it or going negative.
    def _sum_counter_deltas(values):
        total = 0
        for prev, cur in zip(values, values[1:]):
            total += (cur - prev) if cur >= prev else cur
        return total

    req_total = _sum_counter_deltas([r[1] for r in rows])
    tok_total = _sum_counter_deltas([r[2] for r in rows])
    req_per_hour = req_total / span_hr if span_hr > 0 else None
    tok_per_hour = tok_total / span_hr if span_hr > 0 else None

    gpu_samples = [r[3] for r in rows if r[3] is not None]
    gpu_util_avg_pct = (sum(gpu_samples) / len(gpu_samples)) if gpu_samples else None

    return {
        "gpu_util_avg_pct": round(gpu_util_avg_pct, 1) if gpu_util_avg_pct is not None else None,
        "sample_count": len(rows),
        "gpu_sample_count": len(gpu_samples),
        "requests_per_hour": round(req_per_hour, 1) if req_per_hour is not None else None,
        "tokens_per_hour": round(tok_per_hour) if tok_per_hour is not None else None,
    }


PHYSMEM_RE = re.compile(r"PhysMem:\s*([\d.]+)([GM])\s*used.*?([\d.]+)([GM])\s*unused")


def get_ram_status():
    """System-wide RAM headroom - lets the dashboard warn about the same
    RAM contention that caused real model-load failures earlier this session,
    BEFORE a request fails, not just after."""
    try:
        top_out = subprocess.run(
            ["top", "-l", "1", "-n", "0"], capture_output=True, text=True, timeout=5
        ).stdout
        total_bytes = int(
            subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5).stdout.strip()
        )
        total_gb = total_bytes / 1e9

        def to_gb(val, unit):
            v = float(val)
            return v if unit == "G" else v / 1024

        m = PHYSMEM_RE.search(top_out)
        if not m:
            return {"total_gb": round(total_gb, 1)}
        used_gb = to_gb(m.group(1), m.group(2))
        unused_gb = to_gb(m.group(3), m.group(4))
        return {
            "used_gb": round(used_gb, 1),
            "unused_gb": round(unused_gb, 1),
            "total_gb": round(total_gb, 1),
        }
    except Exception:
        return None


FAN_RE = re.compile(r"Fan \d+:\s*actual\s*(\d+),\s*target\s*(\d+),\s*range\s*(\d+)-(\d+)")
GPU_SENSOR_TEMP_RE = re.compile(r"=([\d.]+)\s*C")


def get_fan_temp():
    """Fan RPM and GPU temperature, read via `darkbloom fan status` - a
    read-only report that does NOT enable Darkbloom's fan-control helper,
    just reads the same hardware sensors it would use. Apple Silicon doesn't
    expose fan speed or die temperature through powermetrics at all, so this
    is the only practical source without writing a native SMC-reading helper."""
    try:
        out = subprocess.run(
            [str(DARKBLOOM_BIN), "fan", "status"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except Exception:
        return None

    fan_match = FAN_RE.search(out)
    fan_rpm = int(fan_match.group(1)) if fan_match else None
    fan_max_rpm = int(fan_match.group(4)) if fan_match else None

    gpu_temps = [float(v) for v in GPU_SENSOR_TEMP_RE.findall(out)]
    gpu_temp_c = round(sum(gpu_temps) / len(gpu_temps), 1) if gpu_temps else None
    gpu_temp_max_c = round(max(gpu_temps), 1) if gpu_temps else None

    if fan_rpm is None and gpu_temp_c is None:
        return None
    return {
        "fan_rpm": fan_rpm,
        "fan_max_rpm": fan_max_rpm,
        "gpu_temp_c": gpu_temp_c,
        "gpu_temp_max_c": gpu_temp_max_c,
    }


def get_ollama_status():
    """Whether Ollama currently has any model resident in memory - this is
    exactly what competed for RAM with the darkbloom provider during our
    stress test, so surface it directly instead of leaving it a mystery."""
    try:
        out = subprocess.run(["ollama", "ps"], capture_output=True, text=True, timeout=5).stdout
        lines = [l for l in out.splitlines()[1:] if l.strip()]
        models = [line.split()[0] for line in lines if line.split()]
        return {"loaded": models, "count": len(models)}
    except Exception:
        return {"loaded": [], "count": 0}


def get_daemon_state():
    """Reads the daemon's own live state file directly (plain local JSON the
    daemon rewrites continuously) - gives us inference_active (a request is
    being served RIGHT NOW, vs. idle-but-warm) plus GPU memory/KV-backend
    detail that `darkbloom status`'s text output doesn't expose at all."""
    try:
        data = json.loads(DAEMON_STATE_PATH.read_text())
    except Exception:
        return None
    stats = data.get("stats") or {}
    capacity = data.get("capacity") or {}
    slots = data.get("slots") or []
    written_at = data.get("written_at")
    load_err = data.get("last_model_load_error")
    return {
        "inference_active": data.get("inference_active"),
        "requests_served": stats.get("requests_served"),
        "tokens_generated": stats.get("tokens_generated"),
        "usage_gaps": stats.get("usage_gaps"),
        "gpu_memory_active_gb": capacity.get("gpu_memory_active_gb"),
        "gpu_memory_cache_gb": capacity.get("gpu_memory_cache_gb"),
        "total_memory_gb": capacity.get("total_memory_gb"),
        "slots": [
            {"model": s.get("model"), "kv_backend": s.get("kv_backend"), "mtp_enabled": s.get("mtp_enabled")}
            for s in slots
        ],
        "age_sec": round(time.time() - written_at, 1) if written_at else None,
        "last_model_load_error": {
            "model": load_err.get("model"),
            "message": load_err.get("message"),
            "age_sec": round(time.time() - load_err["at"], 1) if load_err.get("at") else None,
        } if load_err else None,
    }


INFERENCE_POLL_SEC = 0.5
INFERENCE_DURATIONS_LOG = HOME / ".darkbloom" / "inference-durations.csv"


def inference_duration_tracker_loop():
    """Darkbloom exposes no per-request duration/latency anywhere - not in the
    earnings API, not in logs, not in daemon-state.json beyond the
    instantaneous inference_active flag. This builds real duration stats
    ourselves by watching that flag's true->false transitions at a tight poll
    interval. Necessarily our own observation, not an official Darkbloom
    number: can undercount requests shorter than the poll interval, and only
    accumulates data from whenever this thread first started running."""
    INFERENCE_DURATIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
    if not INFERENCE_DURATIONS_LOG.exists():
        INFERENCE_DURATIONS_LOG.write_text("started_at,ended_at,duration_sec\n")
    active_since = None
    while True:
        try:
            state = get_daemon_state()
            active = bool(state and state.get("inference_active"))
        except Exception:
            active = None
        now = time.time()
        if active and active_since is None:
            active_since = now
        elif not active and active_since is not None:
            duration = now - active_since
            try:
                with open(INFERENCE_DURATIONS_LOG, "a") as f:
                    f.write(f"{active_since},{now},{duration:.3f}\n")
            except Exception:
                pass
            active_since = None
        time.sleep(INFERENCE_POLL_SEC)


def get_inference_duration_stats():
    """Reads back the local duration log the tracker thread above writes.
    total_sec/window_start_at let callers compute a real $/hour rate over
    the exact same wall-clock window this log covers - not overall history,
    since the log only started recording at some point after account history
    began."""
    empty = {"count": 0, "avg_sec": None, "min_sec": None, "max_sec": None,
              "total_sec": 0.0, "window_start_at": None}
    if not INFERENCE_DURATIONS_LOG.exists():
        return empty
    try:
        with open(INFERENCE_DURATIONS_LOG, newline="") as f:
            reader = csv.DictReader(f)
            rows = [r for r in reader if r.get("duration_sec") and r.get("started_at")]
    except Exception:
        return empty
    if not rows:
        return empty
    durations = [float(r["duration_sec"]) for r in rows]
    return {
        "count": len(durations),
        "avg_sec": sum(durations) / len(durations),
        "min_sec": min(durations),
        "max_sec": max(durations),
        "total_sec": sum(durations),
        "window_start_at": min(float(r["started_at"]) for r in rows),
    }


STATUS_PATTERNS = {
    "trust": re.compile(r"Trust:\s*(.+)"),
    "trust_reason": re.compile(r"→\s*(.+)"),
    "daemon": re.compile(r"Daemon:\s*(.+)"),
    "requests": re.compile(r"Requests served:\s*(\d+)\s*\|\s*tokens:\s*(\d+)"),
    "warm_models": re.compile(r"Warm models:\s*(.+)"),
    "last_error": re.compile(r"Last model-load error:\s*(.+)"),
    "local_models": re.compile(r"Local MLX models:\s*(\d+)"),
}


def get_darkbloom_status():
    try:
        out = subprocess.run(
            [str(DARKBLOOM_BIN), "status"], capture_output=True, text=True, timeout=10
        ).stdout
    except Exception as e:
        return {"error": str(e)}

    result = {
        "trust": None,
        "trust_reason": None,
        "daemon": None,
        "requests_served": 0,
        "tokens": 0,
        "warm_models": None,
        "last_error": None,
        "local_models": 0,
        "raw": out,
    }
    for line in out.splitlines():
        m = STATUS_PATTERNS["requests"].search(line)
        if m:
            result["requests_served"] = int(m.group(1))
            result["tokens"] = int(m.group(2))
            continue
        m = STATUS_PATTERNS["trust"].search(line)
        if m and result["trust"] is None:
            result["trust"] = m.group(1).strip()
            continue
        m = STATUS_PATTERNS["daemon"].search(line)
        if m:
            result["daemon"] = m.group(1).strip()
            continue
        m = STATUS_PATTERNS["warm_models"].search(line)
        if m:
            result["warm_models"] = m.group(1).strip()
            continue
        m = STATUS_PATTERNS["last_error"].search(line)
        if m:
            result["last_error"] = m.group(1).strip()
            continue
        m = STATUS_PATTERNS["local_models"].search(line)
        if m:
            result["local_models"] = int(m.group(1))
            continue
    # trust-reason (the "→" line directly after the Trust line)
    lines = out.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("Trust:"):
            for j in range(i + 1, min(i + 3, len(lines))):
                mm = STATUS_PATTERNS["trust_reason"].search(lines[j])
                if mm:
                    result["trust_reason"] = mm.group(1).strip()
                    break
            break
    return result


def read_warmup_config():
    # "models": null means "every configured model" (original behavior). Set
    # to a specific list to warm only a subset - needed on this hardware
    # since some model combinations can't stay resident together (loading a
    # second one evicts the first even though the combined catalog size
    # looks like it should fit in the 44GB budget - real overhead is higher
    # than the static estimate), so warming every configured model on a
    # fixed interval would otherwise just thrash between evicting one to
    # load the other, paying a real cold-load cost each swap for nothing.
    default = {"enabled": False, "interval_min": WARMUP_DEFAULT_INTERVAL_MIN, "last_run": None, "last_ok": None, "models": None}
    if not WARMUP_CONFIG.exists():
        return default
    try:
        data = json.loads(WARMUP_CONFIG.read_text())
        default.update(data)
        return default
    except Exception:
        return default


def write_warmup_config(cfg):
    WARMUP_CONFIG.write_text(json.dumps(cfg))


def log_warmup(line):
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    with open(WARMUP_LOG, "a") as f:
        f.write(f"[{ts}] {line}\n")


def get_configured_models():
    """The full set of models this provider is configured to serve, read from
    the launchd plist's --model flags. Deliberately not 'Warm models' from
    darkbloom status - that only lists models actually loaded into memory so
    far, which misses any configured model that hasn't taken its first real
    job yet (exactly the case right after adding a second --model)."""
    try:
        with open(PROVIDER_PLIST, "rb") as f:
            plist = plistlib.load(f)
        args = plist.get("ProgramArguments", [])
        models = [args[i + 1] for i, a in enumerate(args) if a == "--model" and i + 1 < len(args)]
        if models:
            return models
    except Exception:
        pass
    status = get_darkbloom_status()
    if status.get("warm_models"):
        return [m.strip() for m in status["warm_models"].split(",") if m.strip()]
    return []


def send_warmup_ping():
    """Sends a minimal chat completion per configured model to the provider's
    local endpoint, forcing each into memory - the same daemon that serves the
    network. One ping per model since MLX only loads a given model on its
    first request."""
    if not LOCAL_JSON.exists():
        log_warmup("ERROR: local.json missing - start the provider with --local-endpoint")
        return False
    try:
        conf = json.loads(LOCAL_JSON.read_text())
    except Exception as e:
        log_warmup(f"ERROR: could not read local.json: {e}")
        return False

    cfg = read_warmup_config()
    models = cfg.get("models") or get_configured_models()
    if not models:
        log_warmup("ERROR: no configured models found to warm up")
        return False

    all_ok = True
    for model in models:
        body = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1,
        }).encode()
        req = urllib.request.Request(
            conf["base_url"].rstrip("/") + "/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {conf['api_key']}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                resp.read()
            log_warmup(f"OK: warmed up {model}")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                # Not a real failure - the local endpoint only ever returns
                # 429 when it's already busy serving real paid requests for
                # this exact model, which means it was already warm anyway
                # and never needed this ping to begin with.
                log_warmup(f"OK: {model} already busy with real traffic, no warmup needed")
            else:
                log_warmup(f"ERROR: warmup against {model} failed: HTTP {e.code} {e.reason}")
                all_ok = False
        except Exception as e:
            log_warmup(f"ERROR: warmup against {model} failed: {e}")
            all_ok = False
    return all_ok


def read_price_guard_config():
    default = {
        "mode": "manual",  # "manual" | "auto" - manual never calls start/stop on its own
        "margin_pct": 15,
        "min_running_min": 60,
        "min_stopped_min": 30,
        "last_action": None,
        "last_action_at": None,
        "last_reason": None,
        "last_evaluated_at": None,
        "last_price_sek_per_kwh": None,
        "last_break_even_sek_per_kwh": None,
    }
    if not PRICE_GUARD_CONFIG.exists():
        return default
    try:
        data = json.loads(PRICE_GUARD_CONFIG.read_text())
        default.update(data)
        return default
    except Exception:
        return default


def write_price_guard_config(cfg):
    PRICE_GUARD_CONFIG.write_text(json.dumps(cfg))


def log_price_guard(line):
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    with open(PRICE_GUARD_LOG, "a") as f:
        f.write(f"[{ts}] {line}\n")


def _get_provider_start_args():
    """Reconstructs the args for a non-interactive `darkbloom start`,
    read straight from the live plist's own ProgramArguments instead of
    hardcoding them here - so this can never drift out of sync with however
    the provider is actually configured (models, idle-timeout, port, etc.),
    even if that's changed by hand later. Drops the binary path, the "start"
    subcommand itself, and --foreground (that flag only makes sense when
    launchd itself execs the binary directly - we want `start` to register
    and load the service and return, the same as a first-time interactive
    `darkbloom start --model ...` would)."""
    try:
        with open(PROVIDER_PLIST, "rb") as f:
            plist = plistlib.load(f)
        args = plist.get("ProgramArguments", [])
        return [a for a in args[2:] if a != "--foreground"]
    except Exception:
        return []


def _apply_price_guard_action(action, reason):
    """Applies a real start/stop decision via darkbloom's own CLI (not raw
    launchctl) so the coordinator sees a clean, intentional disconnect/
    reconnect rather than something that could look like a crash to its
    trust/continuity tracking. No-ops if the daemon is already in the target
    state, so a repeated decision (or a manual action right before an auto
    one) never sends a redundant command."""
    daemon = get_darkbloom_status().get("daemon") or ""
    running = daemon.startswith("running")
    if action == "stop":
        if not running:
            return True
        try:
            r = subprocess.run([str(DARKBLOOM_BIN), "stop"], capture_output=True, text=True, timeout=30)
            ok = r.returncode == 0
        except Exception as e:
            log_price_guard(f"ERROR: stop failed: {e}")
            return False
        log_price_guard(f"{'STOPPED' if ok else 'ERROR: stop returned nonzero'}: {reason}")
        if ok:
            notify_mac("Darkbloom Live & Stats", f"Stopped serving: {reason}")
        return ok
    elif action == "start":
        if running:
            return True
        args = _get_provider_start_args()
        if not args:
            log_price_guard("ERROR: start skipped - could not read provider plist args")
            return False
        try:
            r = subprocess.run([str(DARKBLOOM_BIN), "start"] + args, capture_output=True, text=True, timeout=60)
            ok = r.returncode == 0
        except Exception as e:
            log_price_guard(f"ERROR: start failed: {e}")
            return False
        log_price_guard(f"{'STARTED' if ok else 'ERROR: start returned nonzero'}: {reason}")
        if ok:
            notify_mac("Darkbloom Live & Stats", f"Resumed serving: {reason}")
        return ok
    return False


def _evaluate_price_guard(cfg):
    """Pure decision function, no side effects - computes today's real
    break-even price and returns an action recommendation (or None) plus the
    numbers behind it, so the math can be sanity-checked independently of
    whatever actually applies it (the loop below, or the manual buttons -
    neither one hits this function's logic path). Compares the account's own
    real 'active $/hr' rate (Nerdy Stats' 'Real pay rate') against what
    running the Mac actually costs right now at the real current price -
    both converted to the same SEK/kWh unit so they're directly comparable.
    Returns None outright (no recommendation either way) whenever the real
    inputs aren't available yet - never guesses a direction from partial
    data."""
    if not CSV_PATH.exists():
        return None
    try:
        with open(CSV_PATH, newline="") as f:
            rows = list(csv.DictReader(f))
        if not rows:
            return None
        last = rows[-1]
        price = float(last.get("elpris_sek_kwh", 0) or 0)
        usd_sek = float(last.get("usd_sek", 0) or 0)
        total_w = float(last.get("total_power_w", 0) or 0)
    except Exception:
        return None
    if price <= 0 or usd_sek <= 0 or total_w <= 0:
        return None

    account = get_account_data()
    rate_cmp = (account or {}).get("hourly_rate_comparison") or {}
    active_rate = rate_cmp.get("active_rate_per_hour_usd")
    if not active_rate or active_rate <= 0:
        return None

    total_kw = total_w / 1000
    break_even_sek_per_kwh = (active_rate * usd_sek) / total_kw
    margin = max(0, cfg.get("margin_pct", 15)) / 100

    daemon_running = (get_darkbloom_status().get("daemon") or "").startswith("running")
    now = time.time()
    last_action = cfg.get("last_action")
    last_action_at = cfg.get("last_action_at") or 0
    min_running_sec = max(0, cfg.get("min_running_min", 60)) * 60
    min_stopped_sec = max(0, cfg.get("min_stopped_min", 30)) * 60

    action = None
    reason = None
    if price > break_even_sek_per_kwh * (1 + margin) and daemon_running:
        if last_action == "stop" or (now - last_action_at) >= min_running_sec:
            action = "stop"
            reason = (f"price {price:.3f} SEK/kWh is above break-even {break_even_sek_per_kwh:.3f} "
                       f"(+{cfg.get('margin_pct', 15)}% margin) for the measured ${active_rate:.4f}/hr active rate")
        else:
            reason = "would stop, but min_running_min not yet elapsed since last action"
    elif price < break_even_sek_per_kwh * (1 - margin) and not daemon_running:
        if last_action == "start" or (now - last_action_at) >= min_stopped_sec:
            action = "start"
            reason = (f"price {price:.3f} SEK/kWh is below break-even {break_even_sek_per_kwh:.3f} "
                       f"(-{cfg.get('margin_pct', 15)}% margin) for the measured ${active_rate:.4f}/hr active rate")
        else:
            reason = "would start, but min_stopped_min not yet elapsed since last action"
    elif daemon_running:
        reason = f"running and profitable ({price:.3f} vs break-even {break_even_sek_per_kwh:.3f} SEK/kWh)"
    else:
        reason = f"stopped and still unprofitable to resume ({price:.3f} vs break-even {break_even_sek_per_kwh:.3f} SEK/kWh)"

    return {
        "price_sek_per_kwh": price,
        "break_even_sek_per_kwh": break_even_sek_per_kwh,
        "active_rate_per_hour_usd": active_rate,
        "total_power_w": total_w,
        "daemon_running": daemon_running,
        "action": action,
        "reason": reason,
    }


def price_guard_loop():
    while True:
        try:
            cfg = read_price_guard_config()
            decision = _evaluate_price_guard(cfg)
            if decision:
                cfg["last_evaluated_at"] = time.time()
                cfg["last_price_sek_per_kwh"] = decision["price_sek_per_kwh"]
                cfg["last_break_even_sek_per_kwh"] = decision["break_even_sek_per_kwh"]
                if decision["action"] and cfg.get("mode") == "auto":
                    ok = _apply_price_guard_action(decision["action"], decision["reason"])
                    if ok:
                        cfg["last_action"] = decision["action"]
                        cfg["last_action_at"] = time.time()
                        cfg["last_reason"] = decision["reason"]
                write_price_guard_config(cfg)
        except Exception as e:
            log_price_guard(f"ERROR: evaluation loop failed: {e}")
        time.sleep(PRICE_GUARD_POLL_INTERVAL_SEC)


CHAT_MAX_TOKENS = 2048
CHAT_FINAL_CHANNEL_RE = re.compile(r"<\|channel\|>final<\|message\|>(.*?)(?:<\|(?:end|return)\|>|$)", re.DOTALL)
CHAT_ANALYSIS_CHANNEL_RE = re.compile(r"<\|channel\|>analysis<\|message\|>(.*?)(?:<\|end\|>|<\|start\|>|$)", re.DOTALL)


def _split_chat_content(content):
    """gpt-oss-20b's local endpoint doesn't parse its own 'harmony' response
    format - it returns the raw generated text, internal <|channel|>analysis
    reasoning included, with the actual reply buried after a
    <|channel|>final<|message|> marker. Other models (e.g. gemma) don't use
    this format at all and pass through untouched (reasoning stays None).
    Returns (final_text, reasoning_or_none) so the frontend can offer a
    "show reasoning" toggle instead of the backend deciding for it. If
    generation got cut off by max_tokens before ever reaching the final
    channel, final_text says so plainly rather than showing a page of raw
    internal reasoning as if it were the answer - the cut-off reasoning
    itself is still returned so the toggle can reveal it."""
    final_m = CHAT_FINAL_CHANNEL_RE.search(content)
    analysis_m = CHAT_ANALYSIS_CHANNEL_RE.search(content)
    reasoning = analysis_m.group(1).strip() if analysis_m else None
    if final_m:
        return final_m.group(1).strip(), reasoning
    if reasoning is not None:
        return "[the model's reasoning ran long and got cut off before its final answer - try a shorter question or try again]", reasoning
    return content.strip(), None


def _proxy_local_chat(model, messages):
    """Proxies a chat turn straight to this Mac's own local endpoint (the
    same one warmup pings already use) - only when the daemon is running AND
    not currently mid-request on real paid traffic (get_daemon_state()'s
    inference_active flag, the same signal the "running, idle" badge already
    uses). Fails closed: if that state can't be read at all, treated as
    unavailable rather than risking a chat request competing with real work
    we can't see. A 429 from the local endpoint means a real job started in
    the gap between this check and the actual request - same benign meaning
    already established for warmup pings, surfaced here as a clear retry
    message instead of a generic error."""
    if not model or not messages:
        return {"error": "bad_request", "message": "model and messages are required"}
    configured = get_configured_models()
    if configured and model not in configured:
        return {"error": "bad_model", "message": f"{model} is not a configured model"}
    daemon = get_darkbloom_status().get("daemon") or ""
    if not daemon.startswith("running"):
        return {"error": "not_running", "message": "The provider daemon is not running right now."}
    state = get_daemon_state()
    if state is None or state.get("inference_active") is not False:
        return {"error": "busy", "message": "Busy serving real paid traffic right now - try again in a moment."}
    if not LOCAL_JSON.exists():
        return {"error": "no_local_endpoint", "message": "local.json missing - provider not started with --local-endpoint"}
    try:
        conf = json.loads(LOCAL_JSON.read_text())
    except Exception as e:
        return {"error": "config_error", "message": str(e)}
    body = json.dumps({
        "model": model,
        "messages": messages,
        "max_tokens": CHAT_MAX_TOKENS,
    }).encode()
    req = urllib.request.Request(
        conf["base_url"].rstrip("/") + "/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {conf['api_key']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
        content = data["choices"][0]["message"]["content"]
        final_text, reasoning = _split_chat_content(content)
        return {"content": final_text, "reasoning": reasoning}
    except urllib.error.HTTPError as e:
        if e.code == 429:
            return {"error": "busy", "message": "Just got busy with real traffic - try again in a moment."}
        return {"error": "http_error", "message": f"HTTP {e.code} {e.reason}"}
    except Exception as e:
        return {"error": "request_failed", "message": str(e)}


TRUST_LOG = HOME / ".darkbloom" / "trust-changes.log"


def notify_mac(title, message):
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification {json.dumps(message)} with title {json.dumps(title)}'],
            timeout=5,
        )
    except Exception:
        pass


def log_trust_change(line):
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    with open(TRUST_LOG, "a") as f:
        f.write(f"[{ts}] {line}\n")


def trust_monitor_loop():
    """Watches the trust level and fires a real macOS notification on change -
    catches the same flicker bug that already bit us once, even if nobody is
    looking at the dashboard tab right then."""
    last_trust = None
    while True:
        try:
            status = get_darkbloom_status()
            trust = status.get("trust")
            if trust and trust != last_trust:
                if last_trust is not None:  # skip the very first read at startup
                    was_hw = last_trust.startswith("hardware")
                    now_hw = trust.startswith("hardware")
                    if was_hw and not now_hw:
                        notify_mac("Darkbloom Live & Stats", f"Trust dropped: {last_trust} -> {trust}")
                        log_trust_change(f"DROP {last_trust} -> {trust}")
                    elif not was_hw and now_hw:
                        notify_mac("Darkbloom Live & Stats", f"Trust recovered: {trust}")
                        log_trust_change(f"RECOVER {last_trust} -> {trust}")
                    else:
                        log_trust_change(f"{last_trust} -> {trust}")
                last_trust = trust
        except Exception:
            pass
        time.sleep(60)


def warmup_loop():
    while True:
        cfg = read_warmup_config()
        if cfg.get("enabled"):
            ok = send_warmup_ping()
            cfg["last_run"] = time.time()
            cfg["last_ok"] = ok
            write_warmup_config(cfg)
            sleep_sec = max(60, int(cfg.get("interval_min", WARMUP_DEFAULT_INTERVAL_MIN)) * 60)
        else:
            sleep_sec = 30  # check often in case it got turned on, without spamming while off
        time.sleep(sleep_sec)


_account_cache = {"data": None, "fetched_at": 0.0}
_account_cache_lock = threading.Lock()


def _fetch_real_account_data():
    """Hits Darkbloom's real earnings API directly with the CLI's own local
    device token - no browser, no bookmarklet. Returns parsed JSON or None
    on any failure (missing token, network error, non-200, bad JSON)."""
    if not AUTH_TOKEN_PATH.exists():
        return None
    try:
        token = AUTH_TOKEN_PATH.read_text().strip()
        if not token:
            return None
        req = urllib.request.Request(
            f"{ACCOUNT_API_URL}?limit={ACCOUNT_API_LIMIT}",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _parse_iso_ts(ts):
    """Parses ISO8601 timestamps from either data source this file talks to:
    the earnings ledger's created_at (trailing 'Z' = UTC, variable-length
    fractional seconds, e.g. '.83428' vs '.178346' - Python's fromisoformat
    is strict about fraction length) and elprisetjustnu.se's price entries
    (explicit +HH:MM offset, no 'Z', no fractional seconds). Returns a unix
    timestamp either way."""
    ts = ts.strip()
    if ts.endswith("Z"):
        ts = ts[:-1]
        if "." in ts:
            base, frac = ts.split(".")
            ts = base + "." + (frac + "000000")[:6]
        return datetime.fromisoformat(ts).replace(tzinfo=timezone.utc).timestamp()
    return datetime.fromisoformat(ts).timestamp()


FLOOR_JOB_ID_RE = re.compile(r"^floor:(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})Z:")


def _compute_hourly_rate_comparison(raw, duration_stats):
    """Compares two real, locally-verifiable $/hour rates from this account's
    own ledger - no advertised/quoted pricing involved:
    - 'active' rate: real earnings (excl. base_reward) during the exact wall-
      clock window our own inference_active-flag tracker has been running,
      divided by the real active-serving seconds that tracker measured.
    - 'floor' rate: base_reward earnings in that same window, divided by
      real elapsed floor-slot time. Slot width isn't assumed - it's read
      straight from the 'floor:<minute>Z:...' job_id Darkbloom itself embeds
      in every base_reward entry, by taking the smallest gap between
      consecutive slot timestamps (i.e. two genuinely back-to-back idle
      slots), so it's derived from the account's own data either way.
    Returns None until the local tracker has logged at least one real
    duration - before that there's no time-aligned window to compare against."""
    window_start = duration_stats.get("window_start_at")
    active_sec = duration_stats.get("total_sec") or 0.0
    if not window_start or active_sec <= 0:
        return None

    real_usd = 0.0
    real_jobs = 0
    base_usd = 0.0
    floor_slot_starts = set()
    for e in raw.get("earnings", []):
        try:
            t = _parse_iso_ts(e["created_at"])
        except Exception:
            continue
        if t < window_start:
            continue
        amt = (e.get("amount_micro_usd", 0) or 0) / 1e6
        if e.get("model") == "base_reward":
            base_usd += amt
            m = FLOOR_JOB_ID_RE.match(e.get("job_id") or "")
            if m:
                floor_slot_starts.add(m.group(1))
        else:
            real_usd += amt
            real_jobs += 1

    active_hours = active_sec / 3600
    active_rate_per_hour = real_usd / active_hours if active_hours > 0 else None

    floor_slots = sorted(floor_slot_starts)
    slot_minutes = None
    if len(floor_slots) >= 2:
        gaps_min = []
        for a, b in zip(floor_slots, floor_slots[1:]):
            ta = datetime.fromisoformat(a).replace(tzinfo=timezone.utc).timestamp()
            tb = datetime.fromisoformat(b).replace(tzinfo=timezone.utc).timestamp()
            gaps_min.append((tb - ta) / 60)
        slot_minutes = min(gaps_min)
    floor_hours = (len(floor_slots) * slot_minutes / 60) if (floor_slots and slot_minutes) else None
    floor_rate_per_hour = (base_usd / floor_hours) if floor_hours else None

    return {
        "window_start_at": window_start,
        "active_hours": active_hours,
        "active_usd": real_usd,
        "active_jobs": real_jobs,
        "active_rate_per_hour_usd": active_rate_per_hour,
        "floor_slots": len(floor_slots),
        "floor_slot_minutes": slot_minutes,
        "floor_hours": floor_hours,
        "floor_usd": base_usd,
        "floor_rate_per_hour_usd": floor_rate_per_hour,
        "ratio_active_to_floor": (
            active_rate_per_hour / floor_rate_per_hour
            if active_rate_per_hour is not None and floor_rate_per_hour
            else None
        ),
    }


def _build_account_view(raw, age_sec):
    """Aggregates the raw earnings list per model and computes the same
    'local estimate vs. real payout' comparison as before. base_reward-style
    entries (no model tag) naturally fall into their own 'unknown' bucket and
    are excluded from the aggregate cut, since they aren't token-based.

    Uses the locally-accumulated earnings history (see
    _update_earnings_history) instead of raw's own single-call "earnings"
    list wherever possible - the API itself never returns more than its most
    recent 1000 entries, which can be under 4 hours on a busy day. Account-
    level totals (balance, lifetime, count) still come straight from raw,
    since those are the real account-wide figures the API already computes
    server-side."""
    raw = dict(raw)
    raw["earnings"] = _load_earnings_history() or raw.get("earnings", [])
    per_model = {}
    for e in raw.get("earnings", []):
        model = e.get("model") or "unknown"
        d = per_model.setdefault(model, {"amount_usd": 0.0, "prompt_tokens": 0, "completion_tokens": 0, "jobs": 0})
        d["amount_usd"] += (e.get("amount_micro_usd", 0) or 0) / 1e6
        d["prompt_tokens"] += e.get("prompt_tokens", 0) or 0
        d["completion_tokens"] += e.get("completion_tokens", 0) or 0
        d["jobs"] += 1

    total_local_est = 0.0
    total_real = 0.0
    for model, d in per_model.items():
        tokens = d["prompt_tokens"] + d["completion_tokens"]
        local_est = tokens * LOCAL_BLENDED_USD_PER_TOKEN
        real = d["amount_usd"]
        d["local_estimate_usd"] = local_est
        d["cut_pct"] = ((local_est - real) / local_est * 100) if local_est > 0 else None
        # Profitability per model: the REAL rate Darkbloom actually paid per
        # million tokens for this model - lets you compare models by efficiency,
        # not just raw job/token volume (a model with fewer jobs can still pay
        # a higher effective rate per token).
        d["real_usd_per_m_tokens"] = (real / tokens * 1_000_000) if tokens > 0 else None
        if tokens > 0:
            total_local_est += local_est
            total_real += real

    # base_reward entries are flat floor payments with 0 tokens - folding them
    # into an "average query length" stat would skew it hard toward zero, so
    # they're excluded here the same way they're excluded from cut_summary above.
    real_models = {m: d for m, d in per_model.items() if m != "base_reward"}
    real_jobs = sum(d["jobs"] for d in real_models.values())
    real_prompt_tokens = sum(d["prompt_tokens"] for d in real_models.values())
    real_completion_tokens = sum(d["completion_tokens"] for d in real_models.values())

    # Observed 2026-09-10: a big chunk of "real" jobs are a uniform 25-prompt-
    # token size, paying near-$0 each - matches a network-wide Gemma spam
    # flood Darkbloom's own team confirmed and started rate-limiting that same
    # day ("a spammer on Gemma side - we have rate limited them", their Slack
    # #providers channel), and other providers independently reported the
    # same signature. Not an official Darkbloom spam flag - just this
    # account's own repeated-exact-size pattern - so this is a heuristic, not
    # a certainty, and shown as a second (not replacement) figure so the raw
    # number above stays honest about what actually happened.
    SPAM_PROMPT_TOKENS = 25
    real_entries = [e for e in raw.get("earnings", []) if (e.get("model") or "unknown") != "base_reward"]
    clean_entries = [e for e in real_entries if (e.get("prompt_tokens") or 0) != SPAM_PROMPT_TOKENS]
    spam_jobs = len(real_entries) - len(clean_entries)
    clean_jobs = len(clean_entries)
    clean_prompt_tokens = sum(e.get("prompt_tokens", 0) or 0 for e in clean_entries)
    clean_completion_tokens = sum(e.get("completion_tokens", 0) or 0 for e in clean_entries)

    nerdy_stats = {
        "real_jobs": real_jobs,
        "base_reward_jobs": per_model.get("base_reward", {}).get("jobs", 0),
        "avg_prompt_tokens": (real_prompt_tokens / real_jobs) if real_jobs else None,
        "avg_completion_tokens": (real_completion_tokens / real_jobs) if real_jobs else None,
        "spam_jobs": spam_jobs,
        "avg_prompt_tokens_excl_spam": (clean_prompt_tokens / clean_jobs) if clean_jobs else None,
        "avg_completion_tokens_excl_spam": (clean_completion_tokens / clean_jobs) if clean_jobs else None,
    }

    # This is now the locally-accumulated window (see _load_earnings_history
    # above), which grows toward EARNINGS_HISTORY_MAX_AGE_SEC the longer this
    # dashboard keeps running - not the raw single API call's own window
    # (hard-capped at 1000 most recent, under 4h on a busy day). Right after
    # this feature first ships the two are the same; surfaced here so the
    # real coverage is always visible either way, not assumed.
    entry_times = [e["created_at"] for e in raw.get("earnings", []) if e.get("created_at")]
    window_start_at = min(entry_times) if entry_times else None
    window_end_at = max(entry_times) if entry_times else None
    window_hours = None
    if window_start_at and window_end_at:
        try:
            window_hours = (_parse_iso_ts(window_end_at) - _parse_iso_ts(window_start_at)) / 3600
        except Exception:
            window_hours = None

    return {
        "connected": True,
        "age_sec": round(age_sec, 1),
        "balance_usd": raw.get("available_balance_usd"),
        "withdrawable_balance_usd": raw.get("withdrawable_balance_usd"),
        "lifetime_usd": raw.get("total_usd"),
        "total_jobs": raw.get("count"),
        "sample_size": len(raw.get("earnings", [])),
        "sample_window_start_at": window_start_at,
        "sample_window_end_at": window_end_at,
        "sample_window_hours": round(window_hours, 1) if window_hours is not None else None,
        "per_model": per_model,
        "nerdy_stats": nerdy_stats,
        "cut_summary": {
            "total_local_estimate_usd": total_local_est,
            "total_real_usd": total_real,
            "cut_pct": ((total_local_est - total_real) / total_local_est * 100) if total_local_est > 0 else None,
        },
        "hourly_rate_comparison": _compute_hourly_rate_comparison(raw, get_inference_duration_stats()),
    }


_earnings_history_lock = threading.Lock()


def _update_earnings_history(fresh_entries):
    """Merges newly-fetched earnings entries into a local, deduped,
    age-pruned log on disk - the API can never return more than its most
    recent 1000 entries per call (see ACCOUNT_API_LIMIT), but by
    accumulating what each poll DOES return, real local coverage grows the
    longer this dashboard keeps running - same pattern as the
    inference-duration tracker. Starts from zero whenever this first ships;
    reaching the full EARNINGS_HISTORY_MAX_AGE_SEC window takes that many
    hours of actual uptime, nothing can backfill history that was never
    locally recorded before now."""
    if not fresh_entries:
        return
    with _earnings_history_lock:
        existing = {}
        try:
            if EARNINGS_HISTORY_PATH.exists():
                with open(EARNINGS_HISTORY_PATH) as f:
                    for line in f:
                        try:
                            e = json.loads(line)
                            existing[e["id"]] = e
                        except Exception:
                            continue
        except Exception:
            existing = {}
        for e in fresh_entries:
            if e.get("id") is not None:
                existing[e["id"]] = e
        cutoff = time.time() - EARNINGS_HISTORY_MAX_AGE_SEC
        kept = []
        for e in existing.values():
            try:
                if _parse_iso_ts(e["created_at"]) >= cutoff:
                    kept.append(e)
            except Exception:
                continue
        try:
            EARNINGS_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(EARNINGS_HISTORY_PATH, "w") as f:
                for e in kept:
                    f.write(json.dumps(e) + "\n")
        except Exception:
            pass


def _load_earnings_history():
    """Reads back the locally accumulated earnings log - real entries this
    dashboard has actually observed over time via repeated polling, covering
    further back than any single API call can (hard-capped at 1000 most
    recent, no working pagination). Empty until _update_earnings_history has
    run at least once."""
    if not EARNINGS_HISTORY_PATH.exists():
        return []
    out = []
    try:
        with open(EARNINGS_HISTORY_PATH) as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    return out


def get_account_data():
    """Real account data, polled directly from Darkbloom's API on its own
    cadence (ACCOUNT_POLL_INTERVAL_SEC), independent of how often the
    dashboard itself is polled. Serves a cached snapshot between refreshes,
    and keeps serving the last good snapshot (rather than going blank) if a
    refresh attempt fails."""
    now = time.time()
    with _account_cache_lock:
        cached, fetched_at = _account_cache["data"], _account_cache["fetched_at"]
        if cached is not None and (now - fetched_at) < ACCOUNT_POLL_INTERVAL_SEC:
            return _build_account_view(cached, now - fetched_at)

    raw = _fetch_real_account_data()
    if raw is None:
        with _account_cache_lock:
            cached, fetched_at = _account_cache["data"], _account_cache["fetched_at"]
        if cached is not None:
            return _build_account_view(cached, now - fetched_at)
        if not AUTH_TOKEN_PATH.exists():
            return {"connected": False, "reason": "no auth_token found - run `darkbloom login` first"}
        return {"connected": False, "reason": "could not reach Darkbloom's API right now"}

    with _account_cache_lock:
        _account_cache["data"] = raw
        _account_cache["fetched_at"] = now
    _update_earnings_history(raw.get("earnings", []))
    return _build_account_view(raw, 0)


DOCTOR_FIX_RE = re.compile(r"↳\s*fix:\s*(.+)")
_doctor_cache = {"data": None, "fetched_at": 0.0}
_doctor_cache_lock = threading.Lock()


def _parse_doctor_line(line, prefix):
    """Parses one [FAIL]/[WARN] line into (check, detail). Most lines use
    'label — detail'; falls back to the whole remainder as detail with no
    check label when there's no em-dash (e.g. "[WARN] config: missing,
    defaults are in memory only"), so nothing is silently dropped."""
    body = line.split(prefix, 1)[1].strip()
    if "—" in body:
        check, detail = body.split("—", 1)
        return check.strip(), detail.strip()
    return None, body


def _run_doctor():
    try:
        out = subprocess.run([str(DARKBLOOM_BIN), "doctor"], capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return None
    fails, warns = [], []
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.startswith("[FAIL]"):
            check, detail = _parse_doctor_line(stripped, "[FAIL]")
            fails.append({"check": check, "detail": detail, "fix": None})
        elif stripped.startswith("[WARN]"):
            check, detail = _parse_doctor_line(stripped, "[WARN]")
            warns.append({"check": check, "detail": detail})
        else:
            m = DOCTOR_FIX_RE.search(stripped)
            if m and fails:
                fails[-1]["fix"] = m.group(1).strip()
    return {"fails": fails, "warns": warns}


def get_doctor_report():
    """`darkbloom doctor` output, parsed for [FAIL]/[WARN] lines - operational
    health signals (e.g. model doesn't fit in RAM = requests failing to load
    right now) distinct from routine stats. Unlike daemon-state.json, doctor
    makes its own network call and runs ~2 dozen checks, so it's cached and
    refreshed on its own slow cadence, same pattern as get_account_data()."""
    now = time.time()
    with _doctor_cache_lock:
        cached, fetched_at = _doctor_cache["data"], _doctor_cache["fetched_at"]
        if cached is not None and (now - fetched_at) < DOCTOR_POLL_INTERVAL_SEC:
            return cached
    result = _run_doctor()
    if result is None:
        with _doctor_cache_lock:
            return _doctor_cache["data"]  # serve stale rather than nothing
    with _doctor_cache_lock:
        _doctor_cache["data"] = result
        _doctor_cache["fetched_at"] = now
    return result


def get_disk_usage():
    """Downloaded MLX models and how much disk they use, split into the
    currently active model vs. everything else - the leftovers from past
    model-rotation experiments that are safe to remove if disk space matters."""
    try:
        out = subprocess.run(
            [str(DARKBLOOM_BIN), "models", "list", "--json"],
            capture_output=True, text=True, timeout=15,
        ).stdout
        data = json.loads(out)
    except Exception:
        return None

    active_models = get_configured_models()
    models = []
    unused_total_bytes = 0
    for m in data.get("models", []):
        size_bytes = m.get("size_bytes", 0) or 0
        is_active = m.get("id") in active_models
        if not is_active:
            unused_total_bytes += size_bytes
        models.append({
            "id": m.get("id"),
            "size_gb": round(size_bytes / 1e9, 1),
            "active": is_active,
        })
    models.sort(key=lambda m: (not m["active"], -m["size_gb"]))
    return {
        "models": models,
        "active_models": active_models,
        "unused_total_gb": round(unused_total_bytes / 1e9, 1),
    }


def _bucket_downsample(rows, max_points):
    """Groups rows into max_points contiguous buckets and returns each
    bucket's per-column AVG (for the plotted line) plus the bucket's peak
    total_power_w (for a peak-envelope overlay) - unlike a fixed-stride pick,
    this can't silently skip over a brief power spike that happened to fall
    between two picked samples. Cumulative columns (cost/revenue) are
    monotonic, so avg vs. peak barely differs there; it mainly matters for
    the bursty instantaneous power columns."""
    n = len(rows)
    bucket_size = n / max_points
    numeric_keys = ["avg_power_w", "total_power_w", "cum_wh", "cum_cost_sek",
                     "elpris_sek_kwh", "usd_sek", "est_revenue_usd_approx"]
    out_rows = []
    peak_total_power_w = []
    for i in range(max_points):
        start = int(i * bucket_size)
        end = n if i == max_points - 1 else max(int((i + 1) * bucket_size), start + 1)
        bucket = rows[start:end]
        last = dict(bucket[-1])
        for k in numeric_keys:
            vals = [float(r.get(k, 0) or 0) for r in bucket]
            last[k] = sum(vals) / len(vals)
        peak_total_power_w.append(max(float(r.get("total_power_w", 0) or 0) for r in bucket))
        out_rows.append(last)
    return out_rows, peak_total_power_w


ELPRIS_ZONE_CONFIG = HOME / ".darkbloom" / "elpris-zone.json"
ELPRIS_VALID_ZONES = ["SE1", "SE2", "SE3", "SE4"]
ELPRIS_DEFAULT_ZONE = "SE3"
ELPRIS_POLL_INTERVAL_SEC = 900  # matches energy-monitor.sh's own elpris cache cadence
ELPRIS_PAST_DAYS = 1  # how many real (always-published) days to show before today
_elpris_cache = {"data": None, "fetched_at": 0.0, "zone": None}
_elpris_cache_lock = threading.Lock()


def get_elpris_zone():
    try:
        zone = json.loads(ELPRIS_ZONE_CONFIG.read_text()).get("zone")
        if zone in ELPRIS_VALID_ZONES:
            return zone
    except Exception:
        pass
    return ELPRIS_DEFAULT_ZONE


def set_elpris_zone(zone):
    if zone not in ELPRIS_VALID_ZONES:
        return False
    ELPRIS_ZONE_CONFIG.write_text(json.dumps({"zone": zone}))
    with _elpris_cache_lock:
        _elpris_cache["fetched_at"] = 0.0  # force a refetch under the new zone next poll
    return True


ELPRIS_SURCHARGE_CONFIG = HOME / ".darkbloom" / "elpris-surcharge.json"
# Swedish VAT on electricity is a fixed nationwide rate (25%) - a real fact,
# not a guess, unlike grid fee/energy tax below which genuinely vary per
# household (grid operator, subscription size, region) and can't be known
# without the user entering their own numbers.
ELPRIS_VAT_PCT = 25


def get_elpris_surcharge():
    """Grid fee (nätavgift) and energy tax (energiskatt), both öre/kWh,
    entered by the user via the dashboard - defaults to 0/0 (i.e. the
    'incl. fees & tax' chart line starts out identical to the raw spot
    price) rather than guessing at a 'typical' Swedish rate, since actual
    grid fees vary enormously by grid operator and subscription size, and
    the energy tax rate itself changes with the annual government budget."""
    try:
        data = json.loads(ELPRIS_SURCHARGE_CONFIG.read_text())
        return {
            "grid_fee_ore_per_kwh": float(data.get("grid_fee_ore_per_kwh", 0) or 0),
            "energy_tax_ore_per_kwh": float(data.get("energy_tax_ore_per_kwh", 0) or 0),
        }
    except Exception:
        return {"grid_fee_ore_per_kwh": 0.0, "energy_tax_ore_per_kwh": 0.0}


def set_elpris_surcharge(grid_fee_ore, energy_tax_ore):
    ELPRIS_SURCHARGE_CONFIG.write_text(json.dumps({
        "grid_fee_ore_per_kwh": grid_fee_ore,
        "energy_tax_ore_per_kwh": energy_tax_ore,
    }))


def _fetch_elpris_day(date_obj, zone):
    """One calendar day's hourly day-ahead prices for a Swedish Nord Pool
    bidding zone, straight from elprisetjustnu.se (free, no API key). Returns
    [] if that day's file isn't published yet (tomorrow's prices clear the
    day-ahead auction and go live daily around 13:00 CET) or on any fetch
    error - never raises, so a slow/offline API just means a shorter chart,
    not a broken page.

    NOT PORTABLE OUTSIDE SWEDEN - if you're adapting this dashboard for a
    different country, this is the one function to replace: elprisetjustnu.se
    only covers Sweden's four Nord Pool zones (SE1-SE4). Swap in your own
    market's day-ahead price API instead - e.g. ENTSO-E Transparency Platform
    (covers most of the EU, needs a free API token), aWATTar (DE/AT), Elexon/
    N2EX (UK), or your local utility/exchange's published day-ahead API.
    Whatever you use, keep returning this same shape - a list of
    {"time_start": ISO8601, "time_end": ISO8601, "sek_per_kwh": float} dicts
    (rename the price key to your own currency/unit) - so nothing downstream
    (get_elpris_48h, the /api/elpris_48h response, the frontend chart) needs
    to change. Also update ELPRIS_VALID_ZONES/ELPRIS_DEFAULT_ZONE above to
    your market's own zone codes, and the currency conversion in
    get_elpris_48h if your source isn't already in USD."""
    url = f"https://www.elprisetjustnu.se/api/v1/prices/{date_obj.year}/{date_obj.month:02d}-{date_obj.day:02d}_{zone}.json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "darkbloom-live-stats/1"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    out = []
    for e in data:
        try:
            out.append({
                "time_start": e["time_start"],
                "time_end": e["time_end"],
                "sek_per_kwh": float(e["SEK_per_kWh"]),
            })
        except Exception:
            continue
    return out


def _latest_usd_sek_rate():
    """The most recently logged USD/SEK rate (energy-monitor.sh refreshes it
    hourly and writes it into every CSV row) - reused here instead of a
    separate fetch, so the 48h price chart's USD conversion always matches
    what the rest of the dashboard is already using."""
    if not CSV_PATH.exists():
        return 9.5
    try:
        with open(CSV_PATH, newline="") as f:
            rows = list(csv.DictReader(f))
        return float(rows[-1].get("usd_sek", 0) or 0) or 9.5 if rows else 9.5
    except Exception:
        return 9.5


def _earnings_by_bucket(all_prices):
    """Real total earnings (incl. base_reward) landed in each 15-min price
    bucket, aligned to the same time grid as the forecast - a rough visual
    read on when earnings actually happened relative to price, not precise
    accounting (a bucket straddling 'now' will look partial, and a bucket
    with mixed real+base_reward jobs is just summed together). Only ever
    non-zero for buckets that have already elapsed. Uses the locally-
    accumulated earnings history (see _load_earnings_history) so the visible
    Net line can cover as much of the chart's 72h span as real local uptime
    has built up, not just the API's own single-call window (hard-capped at
    1000 most recent entries, under 4h on a busy day) - falls back to
    whatever the account cache holds if history isn't available yet."""
    history = _load_earnings_history()
    if not history:
        with _account_cache_lock:
            cached_raw = _account_cache["data"]
        history = cached_raw.get("earnings", []) if cached_raw else []
    if not history:
        return [0.0] * len(all_prices)
    parsed = []
    for e in history:
        try:
            parsed.append((_parse_iso_ts(e["created_at"]), (e.get("amount_micro_usd", 0) or 0) / 1e6))
        except Exception:
            continue
    out = []
    for p in all_prices:
        try:
            t_start = _parse_iso_ts(p["time_start"])
            t_end = _parse_iso_ts(p["time_end"])
        except Exception:
            out.append(0.0)
            continue
        out.append(sum(amt for t, amt in parsed if t_start <= t < t_end))
    return out


def _cost_by_bucket(all_prices):
    """Real measured electricity cost (converted to USD using each CSV row's
    own logged exchange rate) landed in each 15-min price bucket, from the
    same energy-log.csv the Power/Cost charts already use - not re-derived
    from the forecast price, since that's this Mac's own real zone (SE3)
    regardless of whichever zone is selected for the forecast display above.
    Combined with _earnings_by_bucket's real payout, this is what lets the
    chart show whether a given period was actually profitable, not just
    what it earned in isolation."""
    if not CSV_PATH.exists():
        return [0.0] * len(all_prices)
    try:
        with open(CSV_PATH, newline="") as f:
            rows = list(csv.DictReader(f))
    except Exception:
        return [0.0] * len(all_prices)
    parsed = []
    for r in rows:
        try:
            t = _parse_iso_ts(r["timestamp"])
            rate = float(r.get("usd_sek", 0) or 0) or 9.5
            cost_usd = float(r.get("interval_cost_sek", 0) or 0) / rate
            parsed.append((t, cost_usd))
        except Exception:
            continue
    out = []
    for p in all_prices:
        try:
            t_start = _parse_iso_ts(p["time_start"])
            t_end = _parse_iso_ts(p["time_end"])
        except Exception:
            out.append(0.0)
            continue
        out.append(sum(cost for t, cost in parsed if t_start <= t < t_end))
    return out


def _placeholder_day_prices(date_obj):
    """96 empty 15-min buckets (sek_per_kwh: None) for a day whose prices
    haven't been published yet. Keeps the chart's time axis a stable 48h
    even before tomorrow's day-ahead auction clears, instead of the whole
    chart shrinking to 24h and re-expanding once prices land - the frontend
    just draws a gap where the price is still None. Timezone offset is
    taken from the system's local time (this dashboard already assumes
    Europe/Stockholm for the today/tomorrow date split itself); if a DST
    transition falls exactly between today and tomorrow, these placeholder
    labels can be off by an hour for the few hours before real data
    (correct labels either way) replaces them - not worth the complexity
    to handle since it only affects two days a year and self-corrects fast."""
    tz = datetime.now().astimezone().tzinfo
    start = datetime(date_obj.year, date_obj.month, date_obj.day, tzinfo=tz)
    out = []
    for i in range(96):
        t0 = start + timedelta(minutes=15 * i)
        t1 = t0 + timedelta(minutes=15)
        out.append({"time_start": t0.isoformat(), "time_end": t1.isoformat(), "sek_per_kwh": None})
    return out


def get_elpris_48h():
    """Real past days (ELPRIS_PAST_DAYS back) + today + tomorrow's published
    day-ahead electricity prices for the configured zone - a continuous real
    history running into a forecast, not just a forward-looking schedule.
    Past days and today are always real (elprisetjustnu.se keeps every past
    day's file permanently); only tomorrow can still be a placeholder if the
    next day's auction hasn't cleared yet. Cached for ELPRIS_POLL_INTERVAL_SEC
    per zone; tomorrow_available flips to true the moment elprisetjustnu.se
    publishes the next day's prices (usually early-to-mid afternoon local
    time), which is what makes this chart update itself as soon as a new
    day's prices are released."""
    zone = get_elpris_zone()
    now = time.time()
    with _elpris_cache_lock:
        cached, fetched_at, cached_zone = _elpris_cache["data"], _elpris_cache["fetched_at"], _elpris_cache["zone"]
        if cached is not None and cached_zone == zone and (now - fetched_at) < ELPRIS_POLL_INTERVAL_SEC:
            # Surcharge is user-editable and cheap to read - always attach the
            # current value rather than baking it into the 15-min price cache,
            # so changing it reflects immediately instead of waiting on a refetch.
            result = dict(cached)
            result["surcharge"] = {**get_elpris_surcharge(), "vat_pct": ELPRIS_VAT_PCT}
            return result

    today = datetime.now().date()
    tomorrow = today + timedelta(days=1)
    usd_rate = _latest_usd_sek_rate()

    all_prices = []
    for offset in range(-ELPRIS_PAST_DAYS, 0):
        all_prices += _fetch_elpris_day(today + timedelta(days=offset), zone)
    all_prices += _fetch_elpris_day(today, zone)
    tomorrow_prices = _fetch_elpris_day(tomorrow, zone)
    tomorrow_available = len(tomorrow_prices) > 0
    all_prices += tomorrow_prices if tomorrow_available else _placeholder_day_prices(tomorrow)

    for p in all_prices:
        p["usd_per_kwh"] = (p["sek_per_kwh"] / usd_rate) if (usd_rate and p["sek_per_kwh"] is not None) else None
    for p, earnings_usd in zip(all_prices, _earnings_by_bucket(all_prices)):
        p["earnings_usd"] = earnings_usd
    for p, cost_usd in zip(all_prices, _cost_by_bucket(all_prices)):
        p["cost_usd"] = cost_usd
        p["net_usd"] = p["earnings_usd"] - cost_usd

    result = {
        "zone": zone,
        "valid_zones": ELPRIS_VALID_ZONES,
        "prices": all_prices,
        "tomorrow_available": tomorrow_available,
        "usd_sek_rate": usd_rate,
        "surcharge": {**get_elpris_surcharge(), "vat_pct": ELPRIS_VAT_PCT},
    }
    with _elpris_cache_lock:
        _elpris_cache["data"] = result
        _elpris_cache["fetched_at"] = now
        _elpris_cache["zone"] = zone
    return result


def get_energy_series():
    if not CSV_PATH.exists():
        return {"rows": [], "latest": None, "power_monitoring_active": False}

    rows = []
    with open(CSV_PATH, newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(r)

    if not rows:
        return {"rows": [], "latest": None, "power_monitoring_active": False}

    latest = rows[-1]
    power_active = any(float(r.get("avg_power_w", 0) or 0) > 0 for r in rows[-20:])

    # downsample evenly if there are too many points - bucket-averaged, with a
    # separate peak-power track so brief spikes survive the downsampling.
    peak_total_power_w = None
    if len(rows) > MAX_POINTS:
        rows, peak_total_power_w = _bucket_downsample(rows, MAX_POINTS)

    # Electricity price is sourced in SEK (Swedish spot market) but the dashboard
    # displays USD throughout - convert per-row using that row's own exchange
    # rate rather than a single current rate, so historical points stay accurate.
    def usd_rate(r):
        return float(r.get("usd_sek", 0) or 0) or 9.5

    cum_cost_usd = [float(r.get("cum_cost_sek", 0) or 0) / usd_rate(r) for r in rows]
    est_revenue_usd = [float(r.get("est_revenue_usd_approx", 0) or 0) for r in rows]
    series = {
        "timestamps": [r["timestamp"] for r in rows],
        "avg_power_w": [float(r.get("avg_power_w", 0) or 0) for r in rows],
        "total_power_w": [float(r.get("total_power_w", 0) or 0) for r in rows],
        "peak_total_power_w": peak_total_power_w,
        "cum_wh": [float(r.get("cum_wh", 0) or 0) for r in rows],
        "cum_cost_usd": cum_cost_usd,
        "est_revenue_usd": est_revenue_usd,
        "net_usd": [rev - cost for rev, cost in zip(est_revenue_usd, cum_cost_usd)],
        "elpris_usd_kwh": [float(r.get("elpris_sek_kwh", 0) or 0) / usd_rate(r) for r in rows],
        "usd_sek": [usd_rate(r) for r in rows],
    }
    return {
        "rows": series,
        "latest": latest,
        "power_monitoring_active": power_active,
        "total_rows": len(rows),
    }


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # quiet - avoid stdout spam

    def _send_json(self, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/data":
            data = {
                "status": get_darkbloom_status(),
                "energy": get_energy_series(),
                "live_power": get_live_power(),
                "account": get_account_data(),
                "utilization": get_utilization(),
                "disk": get_disk_usage(),
                "daemon_state": get_daemon_state(),
                "doctor": get_doctor_report(),
                "inference_durations": get_inference_duration_stats(),
                "elpris_48h": get_elpris_48h(),
            }
            self._send_json(data)
        elif self.path == "/api/power":
            # Cheap, fast-poll-friendly: just a file tail, no subprocess spawns.
            # Kept separate from /api/live_power so polling this at 5Hz doesn't
            # also spawn top/sysctl/ollama ps five times a second.
            self._send_json({"power": get_live_power()})
        elif self.path == "/api/live_power":
            self._send_json({
                "power": get_live_power(),
                "ram": get_ram_status(),
                "ollama": get_ollama_status(),
                "fan_temp": get_fan_temp(),
            })
        elif self.path == "/api/warmup":
            cfg = read_warmup_config()
            cfg["log_tail"] = WARMUP_LOG.read_text().splitlines()[-10:] if WARMUP_LOG.exists() else []
            cfg["configured_models"] = get_configured_models()
            self._send_json(cfg)
        elif self.path == "/api/price_guard":
            cfg = read_price_guard_config()
            live = _evaluate_price_guard(cfg)
            cfg["live"] = live
            cfg["daemon_running"] = (get_darkbloom_status().get("daemon") or "").startswith("running")
            cfg["log_tail"] = PRICE_GUARD_LOG.read_text().splitlines()[-10:] if PRICE_GUARD_LOG.exists() else []
            self._send_json(cfg)
        elif self.path in ("/", "/index.html"):
            html_path = Path(__file__).parent / "index.html"
            body = html_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/api/warmup/toggle":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            cfg = read_warmup_config()
            if "enabled" in body:
                cfg["enabled"] = bool(body["enabled"])
                log_warmup(f"{'enabled' if cfg['enabled'] else 'disabled'} via dashboard")
            if "interval_min" in body:
                cfg["interval_min"] = max(1, int(body["interval_min"]))
            if "models" in body:
                cfg["models"] = body["models"] or None  # empty list/[] -> back to "all configured"
                log_warmup(f"warmup target set to: {cfg['models'] or 'all configured models'}")
            write_warmup_config(cfg)
            self._send_json(cfg)
        elif self.path == "/api/price_guard/toggle":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            cfg = read_price_guard_config()
            if "mode" in body and body["mode"] in ("manual", "auto"):
                cfg["mode"] = body["mode"]
                log_price_guard(f"mode set to {cfg['mode']} via dashboard")
            if "margin_pct" in body:
                cfg["margin_pct"] = max(0, float(body["margin_pct"]))
            if "min_running_min" in body:
                cfg["min_running_min"] = max(0, float(body["min_running_min"]))
            if "min_stopped_min" in body:
                cfg["min_stopped_min"] = max(0, float(body["min_stopped_min"]))
            write_price_guard_config(cfg)
            self._send_json(cfg)
        elif self.path == "/api/price_guard/action":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            action = body.get("action")
            if action not in ("start", "stop"):
                self.send_response(400)
                self.end_headers()
                return
            cfg = read_price_guard_config()
            reason = f"manual {action} via dashboard"
            ok = _apply_price_guard_action(action, reason)
            if ok:
                cfg["last_action"] = action
                cfg["last_action_at"] = time.time()
                cfg["last_reason"] = reason
                write_price_guard_config(cfg)
            self._send_json({"ok": ok})
        elif self.path == "/api/chat":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            result = _proxy_local_chat(body.get("model"), body.get("messages") or [])
            self._send_json(result)
        elif self.path == "/api/elpris_zone":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            zone = (body.get("zone") or "").upper()
            if set_elpris_zone(zone):
                self._send_json(get_elpris_48h())
            else:
                self.send_response(400)
                self.end_headers()
        elif self.path == "/api/elpris_surcharge":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            try:
                grid_fee = float(body.get("grid_fee_ore_per_kwh", 0) or 0)
                energy_tax = float(body.get("energy_tax_ore_per_kwh", 0) or 0)
            except (TypeError, ValueError):
                self.send_response(400)
                self.end_headers()
            else:
                set_elpris_surcharge(grid_fee, energy_tax)
                self._send_json(get_elpris_48h())
        else:
            self.send_response(404)
            self.end_headers()


class ReusableTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    # Plain TCPServer handles one request at a time - fine at the old 10s/2s
    # poll rates, but the 200ms power poll (potentially from multiple tabs)
    # would queue up and stall behind slower requests like /api/data (which
    # spawns several subprocesses, one with up to a 15s timeout).
    # ThreadingMixIn serves each request on its own thread instead.
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    threading.Thread(target=warmup_loop, daemon=True).start()
    threading.Thread(target=trust_monitor_loop, daemon=True).start()
    threading.Thread(target=inference_duration_tracker_loop, daemon=True).start()
    threading.Thread(target=price_guard_loop, daemon=True).start()
    with ReusableTCPServer(("127.0.0.1", PORT), Handler) as httpd:
        print(f"Darkbloom Live & Stats running at http://127.0.0.1:{PORT}")
        httpd.serve_forever()
