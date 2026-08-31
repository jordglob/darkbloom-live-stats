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
import urllib.parse
import urllib.request
from pathlib import Path

HOME = Path.home()
CSV_PATH = HOME / ".darkbloom" / "energy-log.csv"
DARKBLOOM_BIN = HOME / ".darkbloom" / "bin" / "darkbloom"
RAW_POWER_LOG = Path("/tmp/darkbloom-pm-raw.log")
ROTATE_STATE = HOME / ".darkbloom" / "model-rotate.state"
ROTATE_LOG = HOME / ".darkbloom" / "model-rotate.log"
ROTATE_INTERVAL_SEC = 14400  # 4h of continuous hardware-trust time, see model-rotate.sh
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
LIVE_POWER_TAIL_BYTES = 20000  # plenty for a few samples of lookback
LOCAL_JSON = HOME / ".darkbloom" / "local.json"
WARMUP_CONFIG = HOME / ".darkbloom" / "warmup.json"
WARMUP_LOG = HOME / ".darkbloom" / "warmup.log"
WARMUP_DEFAULT_INTERVAL_MIN = 20  # same cadence as SplittyDev/darkbloom-dashboard

# Real account data from console.darkbloom.dev (api/me/providers + api/me/earnings).
# We NEVER extract the session cookie - the data comes in via a small snippet
# that runs in your already-logged-in browser tab (see README/bookmarklet).
# If no sync has arrived in a while, "not connected" is shown instead of guessed data.
ACCOUNT_DATA_PATH = HOME / ".darkbloom" / "account-data.json"
ACCOUNT_DATA_STALE_SEC = 300  # >5 min without an update = "not connected"

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
UTIL_IDLE_W = 1.5  # SoC power above this = actively computing, not just sitting warm in RAM
UTIL_RAW_TAIL_BYTES = 400_000  # powermetrics samples every 60s; enough tail to cover >60 samples


def get_utilization():
    """Answers 'is the fan silent because nothing is coming in, or is
    something broken': % of the last ~60 min where SoC power was above idle,
    plus real request/token throughput over the same window. A 0% reading
    with normal trust/daemon status just means genuinely no traffic, not a fault."""
    samples = []
    if RAW_POWER_LOG.exists():
        try:
            size = RAW_POWER_LOG.stat().st_size
            with open(RAW_POWER_LOG, "rb") as f:
                f.seek(max(0, size - UTIL_RAW_TAIL_BYTES))
                chunk = f.read().decode("utf-8", errors="ignore")
            cpu_mw = gpu_mw = None
            for line in chunk.splitlines():
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
                    if cpu_mw is not None and gpu_mw is not None:
                        samples.append((cpu_mw + gpu_mw) / 1000)
                        cpu_mw = gpu_mw = None
        except Exception:
            pass
    samples = samples[-UTIL_WINDOW_MIN:]  # ~1 sample/min -> roughly the last hour
    active_pct = (sum(1 for s in samples if s > UTIL_IDLE_W) / len(samples) * 100) if samples else None

    req_per_hour = tok_per_hour = None
    if CSV_PATH.exists():
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
            if len(rows) >= 2:
                span_hr = (rows[-1][0] - rows[0][0]) / 3600
                if span_hr > 0:
                    req_per_hour = (rows[-1][1] - rows[0][1]) / span_hr
                    tok_per_hour = (rows[-1][2] - rows[0][2]) / span_hr
        except Exception:
            pass

    if active_pct is None and req_per_hour is None:
        return None
    return {
        "active_pct": round(active_pct, 1) if active_pct is not None else None,
        "sample_count": len(samples),
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
    """The model currently configured, per model-rotate.state if it exists,
    otherwise 'Warm models'/'Configured model' from darkbloom status."""
    if ROTATE_STATE.exists():
        for line in ROTATE_STATE.read_text().splitlines():
            if line.startswith("CURRENT_MODEL="):
                return line.split("=", 1)[1].strip()
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
                        notify_mac("Darkbloom Monitor", f"Trust dropped: {last_trust} -> {trust}")
                        log_trust_change(f"DROP {last_trust} -> {trust}")
                    elif not was_hw and now_hw:
                        notify_mac("Darkbloom Monitor", f"Trust recovered: {trust}")
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


def get_account_data():
    """Real account data (already aggregated per model), last received via a
    local POST - not directly from the browser (mixed-content/CORS makes that
    impractical from an https page to an http server). See the README for how
    to grab a snapshot from the logged-in browser tab and post it here.
    Returns connected=False if it's too old or missing."""
    if not ACCOUNT_DATA_PATH.exists():
        return {"connected": False, "reason": "no account data received yet"}
    try:
        raw = json.loads(ACCOUNT_DATA_PATH.read_text())
    except Exception as e:
        return {"connected": False, "reason": f"could not read cached data: {e}"}

    received_at = raw.get("received_at", 0)
    age_sec = time.time() - received_at
    if age_sec > ACCOUNT_DATA_STALE_SEC:
        return {
            "connected": False,
            "reason": f"last sync is {int(age_sec)}s old (>{ACCOUNT_DATA_STALE_SEC}s)",
        }

    raw["connected"] = True
    raw["age_sec"] = age_sec

    # "DB cut": compare what our own naive flat-rate formula would have guessed
    # for the SAME real tokens against what Darkbloom's ledger actually paid.
    # This is not an official platform fee (alpha is advertised as 0% fee) - it's
    # just the gap between our guess and reality, whatever is driving it.
    total_local_est = 0.0
    total_real = 0.0
    per_model = raw.get("per_model", {})
    for model, d in per_model.items():
        tokens = (d.get("prompt_tokens", 0) or 0) + (d.get("completion_tokens", 0) or 0)
        local_est = tokens * LOCAL_BLENDED_USD_PER_TOKEN
        real = d.get("amount_usd", 0) or 0
        d["local_estimate_usd"] = local_est
        d["cut_pct"] = ((local_est - real) / local_est * 100) if local_est > 0 else None
        # Profitability per model: the REAL rate Darkbloom actually paid per
        # million tokens for this model - lets you compare models by efficiency,
        # not just raw job/token volume (a model with fewer jobs can still pay
        # a higher effective rate per token).
        d["real_usd_per_m_tokens"] = (real / tokens * 1_000_000) if tokens > 0 else None
        # Only fold token-based models into the aggregate cut - a flat, non-token
        # reward like base_reward has no comparable local estimate and would
        # otherwise skew the overall percentage meaninglessly.
        if tokens > 0:
            total_local_est += local_est
            total_real += real

    raw["cut_summary"] = {
        "total_local_estimate_usd": total_local_est,
        "total_real_usd": total_real,
        "cut_pct": ((total_local_est - total_real) / total_local_est * 100) if total_local_est > 0 else None,
    }
    return raw


def get_rotation_info():
    if not ROTATE_STATE.exists():
        return None
    state = {}
    for line in ROTATE_STATE.read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            state[k] = v

    current_model = state.get("CURRENT_MODEL")
    trust_achieved_at = int(state.get("TRUST_ACHIEVED_AT", "0") or 0)
    now = int(time.time())
    if trust_achieved_at:
        next_switch_in = max(0, ROTATE_INTERVAL_SEC - (now - trust_achieved_at))
        waiting_for_trust = False
    else:
        next_switch_in = None
        waiting_for_trust = True

    history = []
    if ROTATE_LOG.exists():
        with open(ROTATE_LOG, newline="") as f:
            reader = csv.DictReader(f)
            for r in reader:
                history.append(r)
        history = history[-10:]  # last 10 switches is plenty for comparison

    return {
        "current_model": current_model,
        "next_switch_in_sec": next_switch_in,
        "waiting_for_trust": waiting_for_trust,
        "history": history,
    }


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

    # downsample evenly if there are too many points
    if len(rows) > MAX_POINTS:
        step = len(rows) / MAX_POINTS
        sampled = [rows[int(i * step)] for i in range(MAX_POINTS)]
        sampled[-1] = rows[-1]
        rows = sampled

    latest = rows[-1]
    power_active = any(float(r.get("avg_power_w", 0) or 0) > 0 for r in rows[-20:])

    series = {
        "timestamps": [r["timestamp"] for r in rows],
        "avg_power_w": [float(r.get("avg_power_w", 0) or 0) for r in rows],
        "total_power_w": [float(r.get("total_power_w", 0) or 0) for r in rows],
        "cum_wh": [float(r.get("cum_wh", 0) or 0) for r in rows],
        "cum_cost_sek": [float(r.get("cum_cost_sek", 0) or 0) for r in rows],
        "est_revenue_sek": [float(r.get("est_revenue_sek_approx", 0) or 0) for r in rows],
        "net_sek": [float(r.get("net_sek_approx", 0) or 0) for r in rows],
        "elpris_sek_kwh": [float(r.get("elpris_sek_kwh", 0) or 0) for r in rows],
    }
    return {
        "rows": series,
        "latest": latest,
        "power_monitoring_active": power_active,
        "total_rows": len(rows),
    }


SYNC_OK_HTML = b"""<!doctype html><html><body style="font-family:-apple-system,sans-serif;padding:24px;color:#0a0">
<div style="font-size:20px">Darkbloom Monitor: synced</div>
<div style="font-size:13px;color:#888">This tab closes itself...</div>
<script>setTimeout(function(){window.close();}, 500);</script>
</body></html>"""


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
        if self.path.startswith("/api/account_data_get"):
            # Bookmarklet sync (see dashboard footer): an https page can't fetch()
            # OR <img src=...> a plain http:// endpoint - Chrome now blocks both as
            # mixed content. Top-level NAVIGATION isn't restricted, though, so the
            # bookmarklet opens this URL in a new tab (window.open), which we
            # immediately close again via the returned page's own script.
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            raw = qs.get("d", [None])[0]
            if raw:
                try:
                    body = json.loads(urllib.parse.unquote(raw))
                    body["received_at"] = time.time()
                    ACCOUNT_DATA_PATH.write_text(json.dumps(body))
                except Exception:
                    pass
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(SYNC_OK_HTML)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(SYNC_OK_HTML)
        elif self.path == "/api/data":
            data = {
                "status": get_darkbloom_status(),
                "energy": get_energy_series(),
                "live_power": get_live_power(),
                "rotation": get_rotation_info(),
                "account": get_account_data(),
                "utilization": get_utilization(),
                "disk": get_disk_usage(),
            }
            self._send_json(data)
        elif self.path == "/api/live_power":
            self._send_json({
                "power": get_live_power(),
                "ram": get_ram_status(),
                "ollama": get_ollama_status(),
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
        # Note: these are POSTed locally (curl on the same machine), not from the
        # browser - no CORS needed. See README for the account_data flow.
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
        elif self.path == "/api/account_data":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            body["received_at"] = time.time()
            ACCOUNT_DATA_PATH.write_text(json.dumps(body))
            self._send_json({"ok": True})
        else:
            self.send_response(404)
            self.end_headers()


class ReusableTCPServer(socketserver.TCPServer):
    allow_reuse_address = True


if __name__ == "__main__":
    threading.Thread(target=warmup_loop, daemon=True).start()
    threading.Thread(target=trust_monitor_loop, daemon=True).start()
    with ReusableTCPServer(("127.0.0.1", PORT), Handler) as httpd:
        print(f"Darkbloom Monitor running at http://127.0.0.1:{PORT}")
        httpd.serve_forever()
