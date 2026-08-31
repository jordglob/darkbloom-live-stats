#!/usr/bin/env python3
"""Local live dashboard for the Darkbloom provider: status + energy/electricity cost.
Reads ~/.darkbloom/energy-log.csv and runs `darkbloom status` on demand.
Binds to 127.0.0.1 (this machine only) on port 8787.
"""
import csv
import http.server
import json
import re
import socketserver
import subprocess
import threading
import time
import urllib.error
import urllib.request
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
LOCAL_BLENDED_USD_PER_TOKEN = 0.125 / 1_000_000
LIVE_POWER_TAIL_BYTES = 40000  # a few complete samples of lookback at ~5Hz (~5-8KB/sample)
LOCAL_JSON = HOME / ".darkbloom" / "local.json"
WARMUP_CONFIG = HOME / ".darkbloom" / "warmup.json"
WARMUP_LOG = HOME / ".darkbloom" / "warmup.log"
WARMUP_DEFAULT_INTERVAL_MIN = 20  # same cadence as SplittyDev/darkbloom-dashboard
DAEMON_STATE_PATH = HOME / ".darkbloom" / "daemon-state.json"
DOCTOR_POLL_INTERVAL_SEC = 300  # doctor makes a real network call + ~2 dozen checks - too heavy for the 10s cadence

# Real account data pulled directly from Darkbloom's own API, using the same
# device token the `darkbloom` CLI already stores locally after `darkbloom
# login` - we never touch or store any credential ourselves, just read the
# same file the CLI reads. No browser step needed.
AUTH_TOKEN_PATH = HOME / ".darkbloom" / "auth_token"
ACCOUNT_API_URL = "https://api.darkbloom.dev/v1/provider/account-earnings"
ACCOUNT_API_LIMIT = 1000  # the API's practical max per request; plenty for per-model stats
ACCOUNT_POLL_INTERVAL_SEC = 30  # how often we actually hit Darkbloom's API

SAMPLE_HEADER_RE = re.compile(r"\*\*\* Sampled system activity \((.+?)\) \((.+?)\) \*\*\*")


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

    if cpu_mw is None or gpu_mw is None:
        return None
    soc_w = (cpu_mw + gpu_mw) / 1000
    return {
        "cpu_w": round(cpu_mw / 1000, 3),
        "gpu_w": round(gpu_mw / 1000, 3),
        "total_w": round(soc_w + BASELINE_W, 3),  # incl. estimated system baseline
        "baseline_w": BASELINE_W,
        "sample_time": sample_time,
    }

UTIL_WINDOW_MIN = 60  # how far back "recent" looks


def get_utilization():
    """Answers 'is the fan silent because nothing is coming in, or is
    something broken': real request/token throughput over the last ~60 min,
    plus what fraction of the 5-min energy-log slices in that window actually
    saw new requests arrive (duty cycle). Tied directly to the same
    requests_served counter shown elsewhere on the dashboard - NOT a SoC
    power-draw threshold, which turned out to be unreliable: idle power on
    this Mac floats around 2W just from background OS/dashboard load, so a
    naive wattage cutoff read as "100% active" even during genuinely quiet
    stretches with zero new requests."""
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
                rows.append((ts, int(parts[8]), int(parts[9])))
            except Exception:
                continue
        rows = [r for r in rows if now - r[0] <= UTIL_WINDOW_MIN * 60]
    except Exception:
        return None

    if len(rows) < 2:
        return None

    intervals = len(rows) - 1
    active_intervals = sum(1 for i in range(1, len(rows)) if rows[i][1] > rows[i - 1][1])
    active_pct = (active_intervals / intervals * 100) if intervals > 0 else None

    span_hr = (rows[-1][0] - rows[0][0]) / 3600
    req_per_hour = (rows[-1][1] - rows[0][1]) / span_hr if span_hr > 0 else None
    tok_per_hour = (rows[-1][2] - rows[0][2]) / span_hr if span_hr > 0 else None

    return {
        "active_pct": round(active_pct, 1) if active_pct is not None else None,
        "sample_count": len(rows),
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
    """Reads back the local duration log the tracker thread above writes."""
    empty = {"count": 0, "avg_sec": None, "min_sec": None, "max_sec": None}
    if not INFERENCE_DURATIONS_LOG.exists():
        return empty
    try:
        with open(INFERENCE_DURATIONS_LOG, newline="") as f:
            reader = csv.DictReader(f)
            durations = [float(r["duration_sec"]) for r in reader if r.get("duration_sec")]
    except Exception:
        return empty
    if not durations:
        return empty
    return {
        "count": len(durations),
        "avg_sec": sum(durations) / len(durations),
        "min_sec": min(durations),
        "max_sec": max(durations),
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
    default = {"enabled": False, "interval_min": WARMUP_DEFAULT_INTERVAL_MIN, "last_run": None, "last_ok": None}
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


def get_active_model():
    """The model currently configured, from 'Warm models' in darkbloom status."""
    status = get_darkbloom_status()
    if status.get("warm_models"):
        return status["warm_models"].split(",")[0].strip()
    return None


def send_warmup_ping():
    """Sends a minimal chat completion to the provider's local endpoint to force
    the active model into memory - the same daemon that serves the network."""
    if not LOCAL_JSON.exists():
        log_warmup("ERROR: local.json missing - start the provider with --local-endpoint")
        return False
    try:
        conf = json.loads(LOCAL_JSON.read_text())
    except Exception as e:
        log_warmup(f"ERROR: could not read local.json: {e}")
        return False

    model = get_active_model()
    if not model:
        log_warmup("ERROR: no active model found to warm up")
        return False

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
        return True
    except Exception as e:
        log_warmup(f"ERROR: warmup against {model} failed: {e}")
        return False


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


def _build_account_view(raw, age_sec):
    """Aggregates the raw earnings list per model and computes the same
    'local estimate vs. real payout' comparison as before. base_reward-style
    entries (no model tag) naturally fall into their own 'unknown' bucket and
    are excluded from the aggregate cut, since they aren't token-based."""
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
    nerdy_stats = {
        "real_jobs": real_jobs,
        "base_reward_jobs": per_model.get("base_reward", {}).get("jobs", 0),
        "avg_prompt_tokens": (real_prompt_tokens / real_jobs) if real_jobs else None,
        "avg_completion_tokens": (real_completion_tokens / real_jobs) if real_jobs else None,
    }

    return {
        "connected": True,
        "age_sec": round(age_sec, 1),
        "balance_usd": raw.get("available_balance_usd"),
        "withdrawable_balance_usd": raw.get("withdrawable_balance_usd"),
        "lifetime_usd": raw.get("total_usd"),
        "total_jobs": raw.get("count"),
        "sample_size": len(raw.get("earnings", [])),
        "per_model": per_model,
        "nerdy_stats": nerdy_stats,
        "cut_summary": {
            "total_local_estimate_usd": total_local_est,
            "total_real_usd": total_real,
            "cut_pct": ((total_local_est - total_real) / total_local_est * 100) if total_local_est > 0 else None,
        },
    }


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

    active_model = get_active_model()
    models = []
    unused_total_bytes = 0
    for m in data.get("models", []):
        size_bytes = m.get("size_bytes", 0) or 0
        is_active = m.get("id") == active_model
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
        "active_model": active_model,
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
            write_warmup_config(cfg)
            self._send_json(cfg)
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
    with ReusableTCPServer(("127.0.0.1", PORT), Handler) as httpd:
        print(f"Darkbloom Live & Stats running at http://127.0.0.1:{PORT}")
        httpd.serve_forever()
