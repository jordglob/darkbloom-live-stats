#!/usr/bin/env python3
"""Model-choice experiment for a Darkbloom provider.

Runs a pre-registered, randomized schedule of 3-hour blocks, each starting
with one "arm" (a fixed set of models), and records what happened so
questions like these can be answered with data instead of impressions:

  H1  Does hosting a second model next to gpt-oss-20b change how many
      gpt-oss jobs this Mac gets (per hour, and against network demand)?
  H2  Which other models get any traffic here at all, relative to their
      published network demand (a sample, so an index, not a share)?
  H3  Zero-job stretches (like 2026-09-29 00:00-20:30): how often, under
      which arm, and what the provider reported meanwhile.
  H4  Does the base reward depend on what is hosted?

Learning comes first, income second. The schedule is a Latin square over 8
days, so every arm entry lands in every 3-hour slot of the day exactly once,
and time-of-day demand cannot masquerade as a model effect.

A block is a list of segments. The scheduled arm runs first; it is ended
early (amendment 2026-09-30, asked for by the owner) when this Mac is not
completing its jobs properly - the provider crashes, a model fails to load,
or requests are served locally but never show up as paid in the ledger - or
when an arm without gpt-oss has had no paid job for NO_TRAFFIC_STOP_MIN.
gpt-oss arms are never ended for lack of traffic: their zero stretches are
H3 data. Freed time goes to candidate models new to this Mac (from
Darkbloom's catalog, if they fit in memory and on disk, downloaded ahead of
time), least-tested first, else to the baseline.

Subcommands:
  plan [--seed S] [--force]      write protocol.json
  tick                           run by launchd every 5 min
  report [--hours H]             markdown summary to stdout
  pause [reason] | resume        stop/restart switching
  status                         JSON state

Files live in ~/.darkbloom/experiment/. Switching goes through the
dashboard's /api/model_set, so purge/switch/load/plist-sync logic lives in
one place (server.py).
"""
import datetime as dt
import fcntl
import json
import random
import re
import sys
import time
import urllib.request
from pathlib import Path

HOME = Path.home()
DB = HOME / ".darkbloom"
EXP = DB / "experiment"
PROTOCOL = EXP / "protocol.json"
STATE = EXP / "state.json"
EVENTS = EXP / "events.jsonl"
SAMPLES = EXP / "samples.jsonl"
BLOCKS = EXP / "blocks.jsonl"
NETWORK_DIR = EXP / "network-demand"
LOCK = EXP / ".tick.lock"
DAEMON_STATE = DB / "daemon-state.json"
EARNINGS_ARCHIVE = DB / "earnings-archive"
ENERGY_LOG = DB / "energy-log.csv"

DASHBOARD = "http://127.0.0.1:8787"
DEMAND_URL = "https://api.darkbloom.dev/v1/network/model-demand?window=24h"
DEMAND_FETCH_EVERY_SEC = 50 * 60
BLOCK_HOURS = 3
SETTLE_MIN = 10  # jobs in the first minutes after a switch are reported apart
APPLY_TIMEOUT_SEC = 25 * 60
MAX_APPLY_ATTEMPTS = 2
NO_TRAFFIC_STOP_MIN = 90
UNPAID_CHECK_MIN = 30      # served-but-unpaid is judged after this long
UNPAID_MIN_REQUESTS = 20
DAEMON_STALE_SEC = 10 * 60
PROVIDER_DOWN_STOP_MIN = 20
CANDIDATE_CHECK_SEC = 3600
CANDIDATE_MAX_NEED_GB = 36   # size x LOAD_NEED_FACTOR must stay under this
LOAD_NEED_FACTOR = 1.45      # qwen3.6: 21.3 GB on disk, 30.3 GB to load
DISK_KEEP_FREE_GB = 20
DOWNLOAD_GIVE_UP_SEC = 3 * 3600
MAX_DOWNLOAD_ATTEMPTS = 3
DARKBLOOM_BIN = DB / "bin" / "darkbloom"
JOBS_DIR = DB / "jobs"             # one row per job seen in the provider log
JOB_GIVE_UP_SEC = 15 * 60          # received but never completed after this = incomplete

BASELINE = "A"
ARMS = {
    "A": {"models": ["gpt-oss-20b"], "label": "gpt-oss alone (baseline)"},
    "B": {"models": ["gpt-oss-20b", "Qwen3.5-9B"], "label": "gpt-oss + Qwen3.5-9B"},
    "C": {"models": ["qwen3.6-35b-a3b-vl-mtp-mxfp8"], "label": "qwen3.6 alone"},
    "D": {"models": ["qwen3.5-35b-a3b"], "label": "qwen3.5-35b alone"},
    "E": {"models": ["gemma-4-26b-qat-4bit"], "label": "gemma-4-26b alone"},
    "F": {"models": ["Qwen3.5-9B"], "label": "Qwen3.5-9B alone"},
}
# One day = 8 blocks. A and B twice each: H1 is the main question and needs
# the most blocks; A also keeps a steady baseline to compare everything with.
DAY_ARMS = ["A", "A", "B", "B", "C", "D", "E", "F"]


# --- small helpers ----------------------------------------------------------

def now():
    return time.time()


def iso(ts):
    return dt.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def parse_ts(s):
    """ISO timestamp with optional Z and any number of fraction digits."""
    s = s.strip().replace("Z", "+00:00")
    if "." in s:
        head, rest = s.split(".", 1)
        frac, tz = rest, ""
        for sep in ("+", "-"):
            if sep in rest:
                frac, tz = rest.split(sep, 1)
                tz = sep + tz
                break
        s = head + "." + (frac + "000000")[:6] + tz
    t = dt.datetime.fromisoformat(s)
    if t.tzinfo is None:
        t = t.astimezone()
    return t.timestamp()


def read_json(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def write_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def append_jsonl(path, row):
    with open(path, "a") as f:
        f.write(json.dumps(row) + "\n")


def read_jsonl(path):
    out = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except FileNotFoundError:
        pass
    return out


def event(kind, **fields):
    append_jsonl(EVENTS, dict(at=iso(now()), kind=kind, **fields))


def same_model(local_id, public_id):
    """Network stats use aliases (`gemma-4-26b`), the provider the exact build
    (`gemma-4-26b-qat-4bit`). Same rule as server.py."""
    a, b = local_id.lower(), public_id.lower()
    return a == b or a.startswith(b + "-") or a.split("/")[-1] == b.split("/")[-1]


# --- protocol ---------------------------------------------------------------

def make_schedule(start_ts, seed):
    """8-day Latin square: day d is DAY_ARMS (shuffled once) rotated by a
    random, never-repeating shift, so each entry of DAY_ARMS occupies each
    of the 8 daily slots exactly once over the 8 days."""
    rng = random.Random(seed)
    base = DAY_ARMS[:]
    rng.shuffle(base)
    shifts = list(range(len(base)))
    rng.shuffle(shifts)
    blocks = []
    for day, shift in enumerate(shifts):
        row = base[shift:] + base[:shift]
        for slot, arm in enumerate(row):
            s = start_ts + (day * len(row) + slot) * BLOCK_HOURS * 3600
            blocks.append({
                "id": f"d{day + 1}s{slot + 1}",
                "arm": arm,
                "start": s,
                "end": s + BLOCK_HOURS * 3600,
                "start_local": iso(s),
            })
    return blocks


def next_slot_start(ts):
    """Next local wall-clock boundary divisible by BLOCK_HOURS (00, 03, ...)."""
    t = dt.datetime.fromtimestamp(ts).astimezone()
    t = t.replace(minute=0, second=0, microsecond=0)
    while True:
        t += dt.timedelta(hours=1)
        if t.hour % BLOCK_HOURS == 0:
            return t.timestamp()


def cmd_plan(args):
    if PROTOCOL.exists() and "--force" not in args:
        print(f"{PROTOCOL} exists; pass --force to replace it")
        return 1
    seed = int(args[args.index("--seed") + 1]) if "--seed" in args else random.SystemRandom().randrange(1, 10**6)
    EXP.mkdir(parents=True, exist_ok=True)
    start = next_slot_start(now())
    protocol = {
        "created_at": iso(now()),
        "seed": seed,
        "block_hours": BLOCK_HOURS,
        "settle_min": SETTLE_MIN,
        "arms": ARMS,
        "baseline": BASELINE,
        "hypotheses": [l.strip() for l in __doc__.split("Subcommands")[0].splitlines() if l.strip().startswith("H")],
        "blocks": make_schedule(start, seed),
    }
    write_json(PROTOCOL, protocol)
    event("plan", seed=seed, first_block=iso(start), blocks=len(protocol["blocks"]))
    print(f"Planned {len(protocol['blocks'])} blocks from {iso(start)} (seed {seed})")
    return 0


# --- data collection --------------------------------------------------------

def archive_network_demand(state):
    """Hourly buckets from the public stats API, appended per month. A
    bucket is appended again only if its counts changed (late revisions);
    readers keep the last row per (model, hour)."""
    if now() - state.get("demand_fetched_at", 0) < DEMAND_FETCH_EVERY_SEC:
        return
    try:
        req = urllib.request.Request(DEMAND_URL, headers={"User-Agent": "darkbloom-live-stats/1"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
    except Exception as e:
        event("demand_fetch_failed", error=str(e)[:200])
        return
    NETWORK_DIR.mkdir(parents=True, exist_ok=True)
    seen = {}
    for f in sorted(NETWORK_DIR.glob("*.jsonl")):
        for row in read_jsonl(f):
            seen[(row["model"], row["hour"])] = row["counts"]
    fetched = iso(now())
    added = 0
    for m in data.get("models") or []:
        for b in m.get("time_series") or []:
            counts = b.get("counts")
            if not counts:
                continue
            key = (m["model"], b["timestamp"])
            if seen.get(key) == counts:
                continue
            append_jsonl(NETWORK_DIR / f"{b['timestamp'][:7]}.jsonl",
                         {"model": m["model"], "hour": b["timestamp"], "counts": counts, "fetched_at": fetched})
            added += 1
    state["demand_fetched_at"] = now()
    if added:
        event("demand_archived", rows=added)


def daemon_snapshot():
    d = read_json(DAEMON_STATE, {})
    trust = d.get("trust") or {}
    return {
        "pid": d.get("pid"),
        "hosted": d.get("advertised_models") or [],
        "warm": d.get("warm_models") or [],
        "trust": trust.get("trust_level"),
        "status": trust.get("status"),
        "requests_served": (d.get("stats") or {}).get("requests_served"),
        "inference_active": d.get("inference_active"),
        "version": d.get("version"),
        "written_at": d.get("written_at"),
    }


def post_json(path, body, timeout):
    req = urllib.request.Request(DASHBOARD + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def apply_models(models, reason):
    started = now()
    try:
        res = post_json("/api/model_set", {"models": models, "reason": reason}, APPLY_TIMEOUT_SEC)
        ok, msg = bool(res.get("ok")), res.get("message", "")
    except Exception as e:
        ok, msg = False, str(e)
    return ok, msg[:300], round(now() - started, 1)



# --- candidates -------------------------------------------------------------

def run_cli(*args, timeout=30):
    import subprocess
    r = subprocess.run([str(DARKBLOOM_BIN), *args], capture_output=True, text=True, timeout=timeout)
    return r.stdout


def local_models():
    try:
        return [m["id"] for m in json.loads(run_cli("models", "list", "--json", "--all")).get("models", [])]
    except Exception:
        return None


def arm_models():
    return {m for a in ARMS.values() for m in a["models"]}


def maintain_candidates(state):
    """Find catalog models new to this Mac that fit, and download one at a
    time ahead of need. Never deletes anything."""
    cands = state.setdefault("candidates", {})
    # A download runs as a child of the dashboard, so restarting the
    # dashboard (install.sh) kills it silently - seen 2026-09-30, when the
    # bonsai download died at a deploy and sat "downloading" for hours.
    # Check on every tick and start it again, up to MAX_DOWNLOAD_ATTEMPTS.
    for mid, c in cands.items():
        if c.get("status") == "downloading" and not download_running(mid) \
                and now() - c.get("download_started", 0) > 60:
            local = local_models() or []
            if any(same_model(l, mid) for l in local):
                c["status"] = "ready"
                c["local_id"] = next(l for l in local if same_model(l, mid))
                event("candidate_ready", model=mid)
            elif c.get("download_attempts", 1) >= MAX_DOWNLOAD_ATTEMPTS:
                c["status"] = "download_failed"
                event("candidate_download_failed", model=mid, attempts=c.get("download_attempts", 1))
            else:
                c["download_attempts"] = c.get("download_attempts", 1) + 1
                start_download(mid, c)
    if now() - state.get("candidates_checked_at", 0) < CANDIDATE_CHECK_SEC:
        return
    state["candidates_checked_at"] = now()
    try:
        catalog = json.loads(run_cli("models", "catalog", "--json"))
    except Exception as e:
        event("catalog_failed", error=str(e)[:200])
        return
    local = local_models()
    if local is None:
        return
    total_gb = (read_json(DAEMON_STATE, {}).get("capacity") or {}).get("total_memory_gb") or 48
    taken = arm_models()
    for m in catalog:
        mid = m.get("id")
        if not mid or any(same_model(t, mid) or same_model(mid, t) for t in taken):
            continue
        size = m.get("size_gb") or 0
        why_not = None
        if not m.get("active", True):
            why_not = "inactive"
        elif m.get("required_provider_capabilities"):
            why_not = "needs " + ", ".join(m["required_provider_capabilities"])
        elif (m.get("min_ram_gb") or 0) > total_gb:
            why_not = f"needs {m['min_ram_gb']} GB RAM"
        elif size * LOAD_NEED_FACTOR > CANDIDATE_MAX_NEED_GB:
            why_not = f"~{size * LOAD_NEED_FACTOR:.0f} GB to load"
        c = cands.get(mid)
        if c is None:
            c = cands[mid] = {"first_seen": iso(now()), "size_gb": round(size, 1), "tested_min": 0}
            event("candidate_seen", model=mid, size_gb=round(size, 1), excluded=why_not)
        if why_not:
            c["status"] = "excluded: " + why_not
            continue
        if c.get("status") in ("load_failed", "unpaid"):
            continue
        if any(same_model(l, mid) for l in local):
            if c.get("status") != "ready":
                event("candidate_ready", model=mid)
            c["status"] = "ready"
            c["local_id"] = next(l for l in local if same_model(l, mid))
        elif c.get("status") == "downloading":
            if now() - c.get("download_started", 0) > DOWNLOAD_GIVE_UP_SEC:
                c["status"] = "download_failed"
                event("candidate_download_failed", model=mid)
        elif c.get("status") != "download_failed":
            c["status"] = "not_downloaded"
    import shutil
    if any(c.get("status") == "downloading" for c in cands.values()):
        return
    for mid, c in sorted(cands.items(), key=lambda kv: kv[1]["size_gb"]):
        if c.get("status") != "not_downloaded":
            continue
        free_gb = shutil.disk_usage(HOME).free / 1e9
        if free_gb - c["size_gb"] < DISK_KEEP_FREE_GB:
            c["status"] = f"skipped: only {free_gb:.0f} GB free"
            event("candidate_no_disk", model=mid, free_gb=round(free_gb, 1))
            continue
        c["download_attempts"] = 1
        start_download(mid, c)
        break


def download_running(mid):
    import subprocess
    r = subprocess.run(["pgrep", "-f", f"darkbloom models download {mid}"], capture_output=True, text=True)
    return r.returncode == 0


def start_download(mid, c):
    try:
        res = post_json("/api/model_action", {"action": "download", "model": mid}, 30)
    except Exception as e:
        res = {"ok": False, "message": str(e)}
    event("candidate_download", model=mid, ok=res.get("ok"), message=res.get("message"),
          attempt=c.get("download_attempts", 1))
    if res.get("ok"):
        c.update(status="downloading", download_started=now())


def next_segment_arm(state, info):
    """Ready candidate least tested so far and not yet tried in this block,
    else the baseline."""
    tried = {s["arm"] for s in info["segments"]}
    ready = [(c.get("tested_min", 0), mid) for mid, c in state.get("candidates", {}).items()
             if c.get("status") == "ready" and "X:" + mid not in tried]
    if ready:
        return "X:" + min(ready)[1]
    return BASELINE


def arm_spec(state, arm):
    if arm.startswith("X:"):
        mid = arm[2:]
        c = state.get("candidates", {}).get(mid, {})
        return {"models": [c.get("local_id") or mid], "label": f"{mid} alone (candidate)"}
    return ARMS[arm]


# --- provider log ---------------------------------------------------------

# Order matters: first match wins. "Failed to parse coordinator message"
# arrives every 30 s on its own (seen 2026-10-01, provider 0.9.14), says
# nothing about jobs, and would otherwise land in "failed".
JOB_KINDS = ("rejected", "timeout", "cancelled", "failed")
LOG_KINDS = (
    ("unparsed_coordinator_msg", ("failed to parse coordinator message",)),
    # Connection drops say "failed"/"error" too but are not job failures
    # (four at 2026-10-02 04:25, reconnected within seconds).
    ("connection", ("coordinator connection", "disconnected from coordinator", "reconnect")),
    ("update", ("auto-update",)),
    ("rejected", ("429", "reject", "capacity", "busy", "overload")),
    ("timeout", ("timeout", "timed out", "deadline")),
    ("cancelled", ("cancel",)),
    ("failed", ("fail", "error", "abort", "fatal", "panic")),
)


def provider_log_counts(state):
    """Error/Fault-level provider log messages since the last tick, summed
    from the per-minute counts provider_log_stream.py writes (it follows the
    log continuously and also records every job). Only whole minutes are
    read, so a minute is never counted twice. Only counts are stored - never
    message text, which could carry request content."""
    since = state.get("log_counts_until") or (int(now() // 60) * 60 - 300)
    until = int(now() // 60) * 60 - 60  # the streamer writes a minute once it has passed
    counts = {}
    for row in read_jsonl(DB / "provider-log-counts.jsonl"):
        if since <= row.get("minute", 0) < until:
            for k, v in row.items():
                if k.startswith("log_"):
                    counts[k] = counts.get(k, 0) + v
    state["log_counts_until"] = max(since, until)
    counts.setdefault("log_lines", 0)
    counts.setdefault("log_errors", 0)
    return counts


# Per-job lifecycle, readable since private log data was enabled for the
# provider (2026-10-01). The request id equals the ledger's job_id, so a job
# can be followed from arrival to payment. Only ids, times and token counts
# are kept - never prompt or reply text.
RECEIVED_RE = re.compile(r"^Received inference request: (\S+)")
PROCESSING_RE = re.compile(r"^Processing inference request: (\S+)")
COMPLETE_RE = re.compile(r"^\[(\S+)\] Complete: (\d+) prompt \+ (\d+) completion tokens")
ID_IN_MSG_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


def write_job(row):
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    month = time.strftime("%Y-%m", time.localtime(row.get("received") or row.get("completed") or now()))
    append_jsonl(JOBS_DIR / f"jobs-{month}.jsonl", row)


def track_job_line(open_jobs, d, ts, hosted):
    msg = d.get("eventMessage") or ""
    m = RECEIVED_RE.match(msg)
    if m:
        open_jobs[m.group(1)] = {"received": ts, "hosted": hosted}
        return
    m = PROCESSING_RE.match(msg)
    if m:
        open_jobs.setdefault(m.group(1), {"received": ts, "hosted": hosted})["processing"] = ts
        return
    m = COMPLETE_RE.match(msg)
    if m:
        j = open_jobs.pop(m.group(1), {"received": None, "hosted": hosted})
        write_job(dict(j, id=m.group(1), completed=ts, prompt_tokens=int(m.group(2)),
                       completion_tokens=int(m.group(3)), outcome="completed",
                       seconds=round(ts - j["received"], 3) if j.get("received") else None))
        return
    # Anything else that names an open job: remember the last error kind.
    if d.get("messageType") in ("Error", "Fault"):
        m = ID_IN_MSG_RE.search(msg)
        if m and m.group(1) in open_jobs:
            low = msg.lower()
            open_jobs[m.group(1)]["error_kind"] = next(
                (k for k, words in LOG_KINDS if any(w in low for w in words)), "other")


# --- block summaries --------------------------------------------------------

def ledger_between(start, end):
    rows, ids = [], set()
    for f in sorted(EARNINGS_ARCHIVE.glob("earnings-*.jsonl")):
        for e in read_jsonl(f):
            if e.get("id") in ids or not e.get("created_at"):
                continue
            try:
                t = parse_ts(e["created_at"])
            except Exception:
                continue
            if start <= t < end:
                ids.add(e["id"])
                rows.append((t, e))
    return sorted(rows, key=lambda x: x[0])


def energy_between(start, end):
    import csv
    wh = cost = 0.0
    try:
        with open(ENERGY_LOG) as f:
            for r in csv.DictReader(f):
                try:
                    t = parse_ts(r["timestamp"])
                except Exception:
                    continue
                if start < t <= end:
                    wh += float(r.get("interval_wh") or 0)
                    cost += float(r.get("interval_cost_sek") or 0)
    except FileNotFoundError:
        pass
    return round(wh, 1), round(cost, 3)


# "Batch-like": the near-identical small gpt-oss jobs that make up most of
# this Mac's traffic peaks (seen 2026-09-29..10-01: 340-360 prompt tokens,
# a few dozen completion tokens). If one bulk client sends them, their share
# says how much of the income hangs on that one client.
BATCH_PROMPT = (330, 370)
BATCH_MAX_COMPLETION = 60


def is_batch_like(e):
    return (str(e.get("model", "")).startswith("gpt-oss")
            and BATCH_PROMPT[0] <= (e.get("prompt_tokens") or 0) <= BATCH_PROMPT[1]
            and (e.get("completion_tokens") or 0) <= BATCH_MAX_COMPLETION)


def paid_jobs(models, start, end):
    return sum(1 for _, e in ledger_between(start, end)
               if e.get("model") != "base_reward" and any(same_model(m, e["model"]) or same_model(e["model"], m) for m in models))


def summarize_segment(block, seg, state):
    start = seg.get("applied_at") or seg["requested_at"]
    end = seg.get("end") or block["end"]
    settle_end = start + SETTLE_MIN * 60
    per_model, base_usd, settle_jobs = {}, 0.0, 0
    prev, longest_gap, last_job = settle_end, 0.0, None
    for t, e in ledger_between(start, end):
        usd = (e.get("amount_micro_usd") or 0) / 1e6
        if e.get("model") == "base_reward":
            base_usd += usd
            continue
        if t < settle_end:
            settle_jobs += 1
            continue
        m = per_model.setdefault(e["model"], {"jobs": 0, "usd": 0.0, "completion_tokens": 0, "prompt_tokens": 0})
        m["jobs"] += 1
        if is_batch_like(e):
            m["batch_like"] = m.get("batch_like", 0) + 1
        m["usd"] += usd
        m["completion_tokens"] += e.get("completion_tokens") or 0
        m["prompt_tokens"] += e.get("prompt_tokens") or 0
        longest_gap = max(longest_gap, t - prev)
        prev = last_job = t
    longest_gap = max(longest_gap, end - prev)
    log = {}
    for row in read_jsonl(SAMPLES):
        t = row.get("ts")
        if t is None or not (start < t <= end):
            continue
        for k, v in row.items():
            if k.startswith("log_") and isinstance(v, int):
                log[k] = log.get(k, 0) + v
    jobs = {"received": 0, "completed": 0, "incomplete": 0}
    secs = []
    for f in sorted(JOBS_DIR.glob("jobs-*.jsonl")):
        for j in read_jsonl(f):
            t = j.get("received") or j.get("completed")
            if t is None or not (start <= t < end):
                continue
            jobs["received"] += 1
            if j.get("outcome") in jobs:
                jobs[j["outcome"]] += 1
            if j.get("seconds") is not None:
                secs.append(j["seconds"])
    if secs:
        jobs["median_sec"] = round(sorted(secs)[len(secs) // 2], 1)
    measured = max(0.0, (end - settle_end) / 3600) if seg.get("ok") else 0.0
    wh, cost = energy_between(start, end)
    base_hours = max(1e-9, (end - start) / 3600)
    return {
        "arm": seg["arm"],
        "models": seg["models"],
        "scheduled": seg.get("scheduled", False),
        "ok": bool(seg.get("ok")),
        "start": start,
        "end": end,
        "start_local": iso(start),
        "apply_sec": seg.get("apply_sec"),
        "stop_reason": seg.get("stop_reason"),
        "crashes": seg.get("crashes", 0),
        "local_requests": seg.get("req_total", 0),
        "measured_hours": round(measured, 3),
        "per_model": {k: dict(v, usd=round(v["usd"], 6)) for k, v in per_model.items()},
        "paid_jobs": sum(v["jobs"] for v in per_model.values()),
        "paid_usd": round(sum(v["usd"] for v in per_model.values()), 6),
        "base_usd": round(base_usd, 6),
        "base_usd_per_h": round(base_usd / base_hours, 6),
        "settle_jobs": settle_jobs,
        "longest_gap_min": round(longest_gap / 60, 1) if seg.get("ok") else None,
        "last_job_at": iso(last_job) if last_job else None,
        "energy_wh": wh,
        "energy_cost_sek": cost,
        "log": log,
        "jobs": jobs if jobs["received"] else None,
        "batch_counted": True,  # summaries written before v85 lack batch_like
    }


def summarize_block(block, state):
    info = state["blocks"][block["id"]]
    return {
        "id": block["id"],
        "arm": block["arm"],
        "start": block["start"],
        "end": block["end"],
        "start_local": iso(block["start"]),
        "paused": bool(info.get("paused")),
        "segments": [summarize_segment(block, s, state) for s in info.get("segments", [])],
        "summarized_at": iso(now()),
    }


# --- tick -------------------------------------------------------------------

def current_block(protocol, ts):
    for b in protocol["blocks"]:
        if b["start"] <= ts < b["end"]:
            return b
    return None


def cmd_tick(_args):
    EXP.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0  # a previous tick is still switching
        return _tick()


def new_segment(state, info, arm, reason, scheduled=False):
    spec = arm_spec(state, arm)
    seg = {"arm": arm, "models": spec["models"], "requested_at": now(), "attempts": 0,
           "ok": False, "scheduled": scheduled, "reason": reason}
    info["segments"].append(seg)
    event("segment", arm=arm, models=spec["models"], reason=reason)
    return seg


def close_segment(state, seg, reason):
    seg["end"] = now()
    seg["stop_reason"] = reason
    if seg["arm"].startswith("X:") and seg.get("applied_at"):
        c = state.get("candidates", {}).get(seg["arm"][2:])
        if c is not None:
            c["tested_min"] = c.get("tested_min", 0) + round((seg["end"] - seg["applied_at"]) / 60)
    event("segment_stop", arm=seg["arm"], reason=reason)


def track_requests(seg, snap):
    """Local requests_served since the segment started, across daemon
    restarts (the counter restarts from 0)."""
    cur = snap.get("requests_served")
    if cur is None:
        return
    last = seg.get("req_last")
    if last is not None:
        seg["req_total"] = seg.get("req_total", 0) + (cur - last if cur >= last else cur)
    seg["req_last"] = cur


def fault(state, seg, snap, ts):
    """Reason to end the segment early, or None."""
    if not seg.get("ok"):
        return None
    has_gpt = any(m.startswith("gpt-oss") for m in seg["models"])
    since = ts - seg["applied_at"]
    written = snap.get("written_at") or 0
    # Provider down or restarted.
    if ts - written > DAEMON_STALE_SEC:
        seg["down_since"] = seg.get("down_since") or ts
        if seg["arm"] != BASELINE and ts - seg["down_since"] > PROVIDER_DOWN_STOP_MIN * 60:
            return f"provider not reporting for {PROVIDER_DOWN_STOP_MIN}+ min"
        return None
    seg.pop("down_since", None)
    if snap.get("pid") and seg.get("pid") and snap["pid"] != seg["pid"]:
        seg["pid"] = snap["pid"]
        proc = ((read_json(DAEMON_STATE, {}).get("app_attest") or {}).get("process") or {})
        if proc.get("start_reason") == "update" and proc.get("previous_exit") == "clean":
            # Darkbloom's auto-update drains and restarts the provider cleanly
            # (0.9.12 -> 0.9.13 at 2026-09-30 20:31 was first counted as a
            # crash and ended a working nemotron segment). Not the model's fault.
            seg["updates"] = seg.get("updates", 0) + 1
            event("provider_updated", arm=seg["arm"], version=snap.get("version"))
            return None
        seg["crashes"] = seg.get("crashes", 0) + 1
        event("provider_restarted", arm=seg["arm"], crashes=seg["crashes"])
        if seg["arm"] != BASELINE and (not has_gpt or seg["crashes"] >= 2):
            return f"provider restarted {seg['crashes']}x while serving {', '.join(seg['models'])}"
    # A model of this arm failed to load after we applied it.
    err = read_json(DAEMON_STATE, {}).get("last_model_load_error") or {}
    if err.get("model") in seg["models"] and (err.get("at") or 0) > seg.get("reloaded_at", seg["applied_at"]) \
            and err["model"] not in snap.get("warm", []) and seg["arm"] != BASELINE:
        # Darkbloom unloads idle models and reloads on the next job; that
        # reload can fail on file cache alone (gemma, 2026-09-30 18:06, five
        # minutes after loading fine). Free the cache and load once more
        # before blaming the model.
        if seg.get("reloads", 0) < 1:
            seg["reloads"] = seg.get("reloads", 0) + 1
            ok, msg, sec = apply_models(seg["models"], "experiment reload after load error")
            seg["reloaded_at"] = now()
            event("reload", arm=seg["arm"], ok=ok, message=msg, error=str(err.get("message", ""))[:150])
            if ok:
                return None
        return "load error: " + str(err.get("message", ""))[:150]
    if since < SETTLE_MIN * 60:
        return None
    paid = paid_jobs(seg["models"], seg["applied_at"], ts)
    # Served here but never paid: jobs are not completing properly.
    local = seg.get("req_total", 0)
    if since >= UNPAID_CHECK_MIN * 60 and local >= UNPAID_MIN_REQUESTS and paid < 0.5 * local \
            and seg["arm"] != BASELINE:
        return f"{local} requests served locally but only {paid} paid"
    # No traffic at all (never for gpt-oss arms: those gaps are H3 data).
    if not has_gpt and paid == 0 and since >= (SETTLE_MIN + NO_TRAFFIC_STOP_MIN) * 60:
        return f"no paid job in {NO_TRAFFIC_STOP_MIN} min"
    return None


# --- autopilot comparison ---------------------------------------------------
#
# Owner's request 2026-10-03: pause the block experiment and compare
# Darkbloom's own Autopilot (shadow rollout at first) with gpt-oss alone, in
# alternating 24 h phases, 3 + 3, so time of day and the bulk client's habits
# hit both sides equally. Autopilot phases enroll with every downloaded model
# selectable; baseline phases disable Autopilot and host gpt-oss alone. While
# the comparison runs the block experiment stays paused.

COMPARE_PHASE_HOURS = 24
COMPARE_PHASES = 6


def autopilot_status():
    try:
        out = run_cli("autopilot", "status", "--json", timeout=30)
        return json.loads(out)
    except Exception:
        return {}


def autopilot_enable():
    import subprocess
    r = subprocess.run([str(DARKBLOOM_BIN), "autopilot", "enable"], input="all\n",
                       capture_output=True, text=True, timeout=600)
    return r.returncode == 0, (r.stdout + r.stderr).strip().splitlines()[-1:] or [""]


def autopilot_disable():
    import subprocess
    r = subprocess.run([str(DARKBLOOM_BIN), "autopilot", "disable"], capture_output=True,
                       text=True, timeout=600)
    return r.returncode == 0, (r.stdout + r.stderr).strip().splitlines()[-1:] or [""]


PROVIDER_PLIST = HOME / "Library" / "LaunchAgents" / "io.darkbloom.provider.plist"
LOCAL_ENDPOINT_ARGS = ["--local-endpoint", "--port", "8000", "--bind", "127.0.0.1"]


def ensure_local_endpoint():
    """`darkbloom autopilot enable` rewrites the provider plist without
    --local-endpoint (seen 2026-10-03), which silently breaks warm-up, the
    chat panel and the dashboard's model loads (no local.json). Put the
    flags back and restart the provider, draining first. Returns a note for
    the event log, or None when nothing was needed."""
    import plistlib
    import subprocess
    try:
        with open(PROVIDER_PLIST, "rb") as f:
            pl = plistlib.load(f)
    except Exception as e:
        return f"plist unreadable: {e}"
    args = pl.get("ProgramArguments", [])
    if "--local-endpoint" in args and (DB / "local.json").exists():
        return None
    if "--local-endpoint" not in args:
        pl["ProgramArguments"] = args + LOCAL_ENDPOINT_ARGS
        with open(PROVIDER_PLIST, "wb") as f:
            plistlib.dump(pl, f)
    r = subprocess.run([str(DARKBLOOM_BIN), "restart"], capture_output=True, text=True, timeout=600)
    for _ in range(60):
        if (DB / "local.json").exists() and daemon_snapshot().get("pid"):
            return "local endpoint restored, provider restarted"
        time.sleep(5)
    # `darkbloom restart` has failed to bootstrap before ("5: Input/output
    # error"); bootstrap the job directly.
    uid = subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()
    subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(PROVIDER_PLIST)], capture_output=True, text=True)
    return "local endpoint restored; restart needed a manual bootstrap: " + (r.stderr or r.stdout).strip()[-120:]


def current_phase(cmp, ts):
    for ph in cmp.get("phases", []):
        if ph["start"] <= ts < ph["end"]:
            return ph
    return None


def compare_tick(state):
    """Runs the autopilot comparison if one is planned. Returns extra fields
    for this tick's sample."""
    cmp = state.get("compare")
    if not cmp:
        return {}
    ts = now()
    ph = current_phase(cmp, ts)
    if ph is None:
        if ts >= cmp["phases"][-1]["end"] and not cmp.get("finished"):
            ok, msg = autopilot_disable()
            res = apply_models(ARMS[BASELINE]["models"], "autopilot comparison finished")
            cmp["finished"] = True
            event("compare_finished", autopilot_disabled=ok, message=msg, baseline_ok=res[0])
        return {}
    if cmp.get("applied") != ph["id"]:
        if ph["arm"] == "AP":
            ok, msg = autopilot_enable()
            note = ensure_local_endpoint() if ok else None
            if note:
                msg = msg + [note]
        else:
            ok, msg = autopilot_disable()
            note = ensure_local_endpoint() if ok else None
            if note:
                msg = msg + [note]
            if ok:
                ok, msg2, _ = apply_models(ARMS[BASELINE]["models"], f"compare {ph['id']} baseline")
                msg = [msg2]
        event("compare_phase", phase=ph["id"], arm=ph["arm"], ok=ok, message=msg)
        if ok:
            cmp["applied"] = ph["id"]
            ph["applied_at"] = now()
    ap = (autopilot_status().get("configured") or {})
    return {"phase": ph["id"], "phase_arm": ph["arm"],
            "autopilot_enabled": ap.get("enabled"), "autopilot_paused": ap.get("paused")}


def short_model(m):
    for full, short in (("gpt-oss", "gpt-oss"), ("qwen3.6", "qwen3.6"), ("qwen3.5-35b", "qwen3.5-35b"),
                        ("Qwen3.5-9B", "qwen3.5-9b"), ("gemma", "gemma"), ("nemotron", "nemotron"),
                        ("bonsai", "bonsai")):
        if full.lower() in m.lower():
            return short
    return m


def compare_report(state):
    """Autopilot (AP) vs gpt-oss alone (A), per phase and matched by hour of
    day, since the two arms run in alternating 24 h phases."""
    cmp = state["compare"]
    ts = now()
    lines = ["## Autopilot comparison (alternating 24 h phases)", "",
             "| Phase | Start | Arm | Hours | Paid jobs/h | Paid $/h | Base $/h | Jobs in / not done | kWh/h | Loaded in memory (share of time) | Changes |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    samples = [r for r in read_jsonl(SAMPLES) if r.get("phase")]
    by_arm_hour = {}
    for ph in cmp["phases"]:
        if ph["start"] > ts:
            lines.append(f"| {ph['id']} | {ph['start_local'][5:16].replace('T', ' ')} | {ph['arm']} | – | | | | | | planned | |")
            continue
        start = ph.get("applied_at") or ph["start"]
        end = min(ph["end"], ts)
        seg = {"arm": ph["arm"], "models": [], "requested_at": start, "applied_at": start,
               "ok": True, "end": end}
        sm = summarize_segment({"start": ph["start"], "end": end}, seg, state)
        h = sm["measured_hours"]
        if h < 0.1:
            lines.append(f"| {ph['id']} | {ph['start_local'][5:16].replace('T', ' ')} | {ph['arm']} | {h:.1f} | – | – | – | – | – | just started | – |")
            continue
        own = [r for r in samples if r["phase"] == ph["id"]]
        sets = {}
        switches, prev = 0, None
        for r in own:
            # Loaded in memory, not advertised: enrolled, every downloaded
            # model is advertised all the time.
            key = ", ".join(short_model(m) for m in sorted(r.get("warm") or [])) or "none loaded"
            sets[key] = sets.get(key, 0) + 1
            if prev is not None and key != prev:
                switches += 1
            prev = key
        mix = "; ".join(f"{k} {v / len(own) * 100:.0f}%" for k, v in sorted(sets.items(), key=lambda kv: -kv[1])[:3]) if own else "–"
        jobs = sm.get("jobs") or {}
        lines.append(f"| {ph['id']} | {ph['start_local'][5:16].replace('T', ' ')} | {ph['arm']} | {h:.1f} | "
                     f"{sm['paid_jobs'] / h:.0f} | {sm['paid_usd'] / h:.4f} | {sm['base_usd'] / h:.4f} | "
                     f"{jobs.get('received', 0)} / {jobs.get('incomplete', 0)} | {sm['energy_wh'] / 1000 / h:.3f} | {mix} | {switches} |")
        # Hour-of-day buckets for the matched comparison.
        for t, e in ledger_between(start, end):
            if e.get("model") == "base_reward":
                continue
            hr = dt.datetime.fromtimestamp(t).hour
            b = by_arm_hour.setdefault((ph["arm"], hr), [0, 0.0, 0.0])
            b[0] += 1
            b[1] += (e.get("amount_micro_usd") or 0) / 1e6
        t = start
        while t < end:
            nxt = min(end, (int(t // 3600) + 1) * 3600)
            hr = dt.datetime.fromtimestamp(t).hour
            by_arm_hour.setdefault((ph["arm"], hr), [0, 0.0, 0.0])[2] += (nxt - t) / 3600
            t = nxt
    both = [hr for hr in range(24) if by_arm_hour.get(("AP", hr), [0, 0, 0])[2] > 0.25
            and by_arm_hour.get(("A", hr), [0, 0, 0])[2] > 0.25]
    lines.append("")
    if both:
        rate = lambda arm, i: sum(by_arm_hour[(arm, hr)][i] / by_arm_hour[(arm, hr)][2] for hr in both) / len(both)
        lines += [f"Matched over the {len(both)} hours of day both arms have covered: "
                  f"AP {rate('AP', 0):.0f} paid jobs/h, ${rate('AP', 1):.4f}/h; "
                  f"A {rate('A', 0):.0f} paid jobs/h, ${rate('A', 1):.4f}/h. "
                  "Each hour of day weighs the same, so a busy evening in one arm only cannot tip it.", ""]
    else:
        lines += ["Matched comparison: not enough overlapping hours of day yet.", ""]
    return lines


def cmd_compare(args):
    """compare start | status"""
    state = read_json(STATE, {})
    if args and args[0] == "start":
        if state.get("compare") and not state["compare"].get("finished") and "--force" not in args:
            print("a comparison is already running")
            return 1
        first = "AP" if "--first-ap" in args else random.SystemRandom().choice(["AP", "A"])
        start = int(now() // 3600) * 3600
        arms = [first, "A" if first == "AP" else "AP"] * (COMPARE_PHASES // 2)
        phases = [{"id": f"c{i + 1}", "arm": a, "start": start + i * COMPARE_PHASE_HOURS * 3600,
                   "end": start + (i + 1) * COMPARE_PHASE_HOURS * 3600,
                   "start_local": iso(start + i * COMPARE_PHASE_HOURS * 3600)}
                  for i, a in enumerate(arms)]
        state["compare"] = {"created_at": iso(now()), "phases": phases}
        state["paused"] = True
        state["pause_reason"] = state.get("pause_reason") or "autopilot comparison running"
        write_json(STATE, state)
        event("compare_planned", first=first, phases=len(phases), start=iso(start))
        print(f"Planned {len(phases)} phases from {iso(start)}, first {first}")
        return 0
    print(json.dumps(state.get("compare"), indent=2))
    return 0


def _tick():
    protocol = read_json(PROTOCOL, None)
    state = read_json(STATE, {})
    state.setdefault("blocks", {})
    archive_network_demand(state)
    log_counts = provider_log_counts(state)
    try:
        maintain_candidates(state)
    except Exception as e:
        event("candidates_error", error=str(e)[:200])
    if not protocol:
        write_json(STATE, state)
        return 0

    ts = now()
    cmp_fields = compare_tick(state)
    snap = daemon_snapshot()
    block = current_block(protocol, ts)

    # Close and summarize every finished block exactly once.
    done = set(state.get("summarized", []))
    for b in protocol["blocks"]:
        if b["end"] <= ts and b["id"] not in done and b["id"] in state["blocks"]:
            for s in state["blocks"][b["id"]].get("segments", []):
                if not s.get("end"):
                    s["end"] = b["end"]
                    if s["arm"].startswith("X:") and s.get("applied_at"):
                        c = state.get("candidates", {}).get(s["arm"][2:])
                        if c is not None:
                            c["tested_min"] = c.get("tested_min", 0) + round((s["end"] - s["applied_at"]) / 60)
            append_jsonl(BLOCKS, summarize_block(b, state))
            done.add(b["id"])
    state["summarized"] = sorted(done)

    comparing = bool(state.get("compare")) and not state["compare"].get("finished")
    if block is None:
        if comparing:
            # The comparison outlives the block schedule: keep sampling, and
            # leave the models to it.
            append_jsonl(SAMPLES, dict(at=iso(ts), ts=ts, block=None, arm=None, paused=True,
                                       **snap, **log_counts, **cmp_fields))
        elif ts >= protocol["blocks"][-1]["end"] and not state.get("finished"):
            state["finished"] = True
            ok, msg, _ = apply_models(ARMS[BASELINE]["models"], "experiment finished")
            event("finished", back_to_baseline=ok, message=msg)
        write_json(STATE, state)
        return 0

    info = state["blocks"].setdefault(block["id"], {"arm": block["arm"], "segments": []})
    seg = info["segments"][-1] if info["segments"] else None
    append_jsonl(SAMPLES, dict(at=iso(ts), ts=ts, block=block["id"], arm=seg["arm"] if seg else block["arm"],
                               paused=bool(state.get("paused")), **snap, **log_counts, **cmp_fields))

    if state.get("paused"):
        info["paused"] = True
        write_json(STATE, state)
        return 0

    if seg is None:
        seg = new_segment(state, info, block["arm"], "scheduled", scheduled=True)

    if seg.get("ok"):
        track_requests(seg, snap)
        hosted = sorted(snap["hosted"])
        # Someone changed the models by hand after we applied them: step aside.
        if snap["pid"] and hosted and hosted != sorted(seg["models"]) and ts - seg["applied_at"] > 120:
            state["paused"] = True
            state["pause_reason"] = f"manual change detected: hosting {hosted}, segment wants {sorted(seg['models'])}"
            info["paused"] = True
            event("paused", reason=state["pause_reason"], block=block["id"])
            write_json(STATE, state)
            return 0
        reason = fault(state, seg, snap, ts)
        if not reason:
            write_json(STATE, state)
            return 0
        close_segment(state, seg, reason)
        c = state.get("candidates", {}).get(seg["arm"][2:]) if seg["arm"].startswith("X:") else None
        if c is not None and ("load error" in reason or "restarted" in reason):
            c["status"] = "load_failed"
        elif c is not None and "only" in reason:
            c["status"] = "unpaid"
        seg = new_segment(state, info, next_segment_arm(state, info), "after: " + reason)

    # Apply the (new) segment's models.
    seg["attempts"] += 1
    write_json(STATE, state)
    ok, msg, sec = apply_models(seg["models"], f"experiment {block['id']} {seg['arm']}")
    event("apply", block=block["id"], arm=seg["arm"], models=seg["models"], ok=ok,
          message=msg, seconds=sec, attempt=seg["attempts"])
    state = read_json(STATE, state)
    info = state["blocks"][block["id"]]
    seg = info["segments"][-1]
    if ok:
        s2 = daemon_snapshot()
        seg.update(ok=True, applied_at=now(), apply_sec=sec, pid=s2.get("pid"),
                   req_last=s2.get("requests_served"), req_total=0)
    elif seg["attempts"] >= MAX_APPLY_ATTEMPTS and seg["arm"] != BASELINE:
        close_segment(state, seg, "apply failed: " + msg)
        if seg["arm"].startswith("X:"):
            state["candidates"][seg["arm"][2:]]["status"] = "load_failed"
        new_segment(state, info, next_segment_arm(state, info), "after apply failure")
    write_json(STATE, state)
    return 0


# --- report -----------------------------------------------------------------

def network_by_hour():
    out = {}
    for f in sorted(NETWORK_DIR.glob("*.jsonl")):
        for row in read_jsonl(f):
            out[(row["model"], row["hour"])] = row["counts"]
    return out


def network_requests(net, local_model, start, end):
    """Network requests for a model over [start, end), prorating partial
    hours. Returns None if any covered hour is unpublished."""
    total, t = 0.0, start
    while t < end:
        hour_start = t - (t % 3600)
        hour_end = min(hour_start + 3600, end)
        key_hour = dt.datetime.fromtimestamp(hour_start, dt.timezone.utc).strftime("%Y-%m-%dT%H:00:00Z")
        counts = [c for (m, h), c in net.items() if h == key_hour and same_model(local_model, m)]
        if not counts:
            return None
        total += counts[0].get("requests", 0) * (hour_end - t) / 3600
        t = hour_end
    return total


def demand_by_hour():
    """Per local hour of day: published network requests per model (mean over
    the days that hour was published - unpublished hours fall under
    Darkbloom's privacy threshold and are unknown, not zero), and this Mac's
    gpt-oss jobs per hour while gpt-oss was hosted (from 5-min samples)."""
    net = {}
    for (model, hour), counts in network_by_hour().items():
        t = parse_ts(hour)
        h = dt.datetime.fromtimestamp(t).hour
        net.setdefault(h, {}).setdefault(model, []).append(counts.get("requests", 0))
    hosted_slots = set()
    for row in read_jsonl(SAMPLES):
        ts = row.get("ts") or (parse_ts(row["at"]) if row.get("at") else None)
        if ts and any(m.startswith("gpt-oss") for m in row.get("hosted") or []):
            hosted_slots.add(int(ts // 300))
    ours, cover = {}, {}
    for slot in hosted_slots:
        h = dt.datetime.fromtimestamp(slot * 300).hour
        cover[h] = cover.get(h, 0) + 300
    if hosted_slots:
        first = min(hosted_slots) * 300
        for t, e in ledger_between(first, now()):
            if e.get("model", "").startswith("gpt-oss") and int(t // 300) in hosted_slots:
                h = dt.datetime.fromtimestamp(t).hour
                ours[h] = ours.get(h, 0) + 1
                if is_batch_like(e):
                    ours[("batch", h)] = ours.get(("batch", h), 0) + 1
    return net, ours, cover


def all_segments(state):
    """Summaries of finished blocks plus the running block's segments so far."""
    out = []
    finished = {}
    for b in read_jsonl(BLOCKS):
        finished[b["id"]] = b  # last summary wins
    for b in finished.values():
        for s in b.get("segments", []):
            out.append(dict(s, block=b["id"], paused=b.get("paused", False)))
    protocol = read_json(PROTOCOL, None)
    cur = current_block(protocol, now()) if protocol else None
    if cur and cur["id"] in state.get("blocks", {}) and cur["id"] not in finished:
        info = state["blocks"][cur["id"]]
        for s in info.get("segments", []):
            live = dict(s)
            live.setdefault("end", None)
            live_block = dict(cur, end=min(cur["end"], now()))
            out.append(dict(summarize_segment(live_block, live, state), block=cur["id"],
                            paused=info.get("paused", False), running=not s.get("end")))
    return sorted(out, key=lambda s: s["start"])


def arm_label(state, arm):
    return arm_spec(state, arm)["label"] if arm in ARMS or arm.startswith("X:") else arm


def cmd_report(args):
    protocol = read_json(PROTOCOL, None)
    state = read_json(STATE, {})
    if not protocol:
        print("No experiment planned.")
        return 0
    hours_back = float(args[args.index("--hours") + 1]) if "--hours" in args else None
    segs = all_segments(state)
    valid = [s for s in segs if s["ok"] and not s["paused"] and s["measured_hours"] > 0]
    net = network_by_hour()
    ts = now()
    lines = [f"# Model experiment report ({iso(ts)[:16]})", ""]
    lines.append(f"Blocks done: {len(state.get('summarized', []))}/{len(protocol['blocks'])}. "
                 f"Paused: {'yes - ' + state.get('pause_reason', '') if state.get('paused') else 'no'}.")
    cur = current_block(protocol, ts)
    if cur and state.get("blocks", {}).get(cur["id"], {}).get("segments"):
        s = state["blocks"][cur["id"]]["segments"][-1]
        lines.append(f"Now: block {cur['id']}, {arm_label(state, s['arm'])} until {iso(cur['end'])[11:16]}.")
    lines.append("")

    lines += ["## Per arm (after the settle period)", "",
              "| Arm | Segments (scheduled) | Hours | Paid jobs/h | Paid $/h | Base $/h | Jobs per 100 published network requests | Longest gap, median min | Provider log errors/h | Jobs in / done / not done (tracked) |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for arm in list(ARMS) + sorted({s["arm"] for s in valid if s["arm"].startswith("X:")}):
        ss = [s for s in valid if s["arm"] == arm]
        if not ss:
            lines.append(f"| {arm} {arm_label(state, arm)} | 0 | – | – | – | – | – | – | – | – |")
            continue
        h = sum(s["measured_hours"] for s in ss)
        idx = []
        for model in ss[0]["models"]:
            ours = theirs = 0.0
            complete = True
            for s in ss:
                n = network_requests(net, model, s["end"] - s["measured_hours"] * 3600, s["end"])
                if n is None:
                    complete = False
                    continue
                theirs += n
                ours += sum(v["jobs"] for k, v in s["per_model"].items() if same_model(k, model) or same_model(model, k))
            idx.append(f"{model.split('-')[0]} {ours / theirs * 100:.1f}" if theirs else f"{model.split('-')[0]} –")
            if not complete:
                idx[-1] += "*"
        gaps = sorted(s["longest_gap_min"] for s in ss if s["longest_gap_min"] is not None)
        logged = [s for s in ss if (s.get("log") or {}).get("log_lines") is not None and s.get("log")]
        log_h = sum((s.get("end") or ts) - s["start"] for s in logged) / 3600
        job_errs = sum(s["log"].get("log_" + k, 0) for s in logged for k in JOB_KINDS)
        # Job-related errors are rare, so show their count, not a rate that
        # rounds to 0 (2 cancellations in 8 h read "job-related 0").
        err_rate = (f"{sum(s['log'].get('log_errors', 0) for s in logged) / log_h:.0f}"
                    f" (job-related: {job_errs} total)") if log_h > 0.05 else "–"
        lines.append(f"| {arm} {arm_label(state, arm)} | {len(ss)} ({sum(1 for s in ss if s['scheduled'])}) | {h:.1f} | "
                     f"{sum(s['paid_jobs'] for s in ss) / h:.0f} | {sum(s['paid_usd'] for s in ss) / h:.4f} | "
                     f"{sum(s['base_usd'] for s in ss) / h:.4f} | {'; '.join(idx)} | {gaps[len(gaps) // 2] if gaps else 0:.0f} | {err_rate} | {jobs_cell(ss)} |")
    lines += ["", "Provider log errors: Error/Fault-level lines in the provider's own log (counted since 2026-10-01). Since 2026-10-01 11:17 private log data is enabled for the provider (a configuration profile), so they are also sorted by keyword; job-related = rejected, timeout, cancelled or failed. The ~120/h baseline is 'Failed to parse coordinator message' every 30 s, unrelated to jobs. Only counts are kept.", ""]
    lines += ["", "Published network counts are a privacy-filtered sample (872 gpt-oss requests in an evening window where this Mac alone served 2829), so the index can exceed 100: compare it between arms, not as a market share. \\* some hours not published yet (≥1 h lag).", ""]

    def gpt_rates(arm):
        return [sum(v["jobs"] for k, v in s["per_model"].items() if k.startswith("gpt-oss")) / s["measured_hours"]
                for s in valid if s["arm"] == arm and s["scheduled"] and s["measured_hours"] >= 1]
    a, b = gpt_rates("A"), gpt_rates("B")
    if a and b:
        med = lambda xs: sorted(xs)[len(xs) // 2]
        lines += ["## H1: gpt-oss jobs/h alone vs. with Qwen3.5-9B (scheduled segments only)", "",
                  f"- A alone: n={len(a)}, median {med(a):.0f}/h, mean {sum(a) / len(a):.0f}/h",
                  f"- B with companion: n={len(b)}, median {med(b):.0f}/h, mean {sum(b) / len(b):.0f}/h"]
        if min(len(a), len(b)) < 6:
            lines.append("- Too early to conclude while n < 6 per arm.")
        lines.append("")

    net_h, ours_h, cover_h = demand_by_hour()
    if net_h:
        top = sorted({m for h in net_h.values() for m in h},
                     key=lambda m: -sum(sum(v) for h in net_h.values() for k, v in h.items() if k == m))[:3]
        if not any(m.startswith("gpt-oss") for m in top):
            top = [m for m in {m for h in net_h.values() for m in h} if m.startswith("gpt-oss")][:1] + top[:2]
        lines += ["## Demand by hour of day (local time)", "",
                  "Network: mean published requests in that hour (days published in brackets; unpublished hours are below Darkbloom's privacy threshold, not zero). Here: this Mac's gpt-oss jobs per hour while gpt-oss was hosted (hours observed in brackets), and the share of them that are batch-like (330-370 prompt tokens, at most 60 completion tokens - the near-identical small jobs behind the traffic peaks).", "",
                  "| Hour | " + " | ".join(m.split("-")[0] + " " + (m.split("-")[1] if "-" in m else "") for m in top) + " | All models | Here: gpt-oss jobs/h | Here: batch-like share |",
                  "|---|" + "---|" * (len(top) + 3)]
        for h in range(24):
            row = net_h.get(h, {})
            cells = []
            for m in top:
                v = row.get(m)
                cells.append(f"{sum(v) / len(v):.0f} ({len(v)})" if v else "–")
            days = max((len(v) for v in row.values()), default=0)
            total = f"{sum(sum(v) for v in row.values()) / days:.0f}" if days else "–"
            here = f"{ours_h.get(h, 0) / (cover_h[h] / 3600):.0f} ({cover_h[h] / 3600:.1f})" if cover_h.get(h) else "–"
            batch = f"{ours_h.get(('batch', h), 0) / ours_h[h] * 100:.0f}%" if ours_h.get(h) else "–"
            lines.append(f"| {h:02d} | " + " | ".join(cells) + f" | {total} | {here} | {batch} |")
        lines.append("")

    if state.get("compare"):
        lines += compare_report(state)

    shown = [s for s in segs if not hours_back or (s["end"] or ts) >= ts - hours_back * 3600]
    lines += ["## Segments" + (f" (last {hours_back:.0f} h)" if hours_back else ""), "",
              "| Block | Start | Arm | Min | Jobs | gpt-oss batch-like | Paid $ | Base $ | Longest gap | Log errors | Stopped because |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for s in shown:
        models = ", ".join(f"{k.split('-')[0]}:{v['jobs']}" for k, v in s["per_model"].items())
        why = s.get("stop_reason") or ("running" if s.get("running") else "")
        if not s["ok"]:
            why = why or "not applied"
        if s["paused"]:
            why = (why + "; paused").strip("; ")
        lines.append(f"| {s['block']} | {s['start_local'][5:16].replace('T', ' ')} | {s['arm']} | "
                     f"{((s['end'] or ts) - s['start']) / 60:.0f} | {s['paid_jobs']} {('(' + models + ')') if models else ''} | "
                     f"{batch_cell(s)} | "
                     f"{s['paid_usd']:.4f} | {s['base_usd']:.4f} | {fmt(s['longest_gap_min'], 0)} | {log_cell(s.get('log'))} | {why} |")

    cands = state.get("candidates", {})
    if cands:
        lines += ["", "## Candidate models", ""]
        for mid, c in sorted(cands.items()):
            lines.append(f"- {mid} ({c.get('size_gb')} GB): {c.get('status', '?')}, tested {c.get('tested_min', 0)} min")
    fails = [e for e in read_jsonl(EVENTS) if e["kind"] == "apply" and not e.get("ok")]
    if fails:
        lines += ["", "## Failed switches", ""] + [f"- {e['at'][5:16]} {e.get('block')} {e.get('arm')}: {e.get('message', '')}" for e in fails[-10:]]
    print("\n".join(lines))
    return 0


def batch_cell(seg):
    if not seg.get("batch_counted"):
        return "–"
    g = [v for k, v in seg["per_model"].items() if k.startswith("gpt-oss")]
    jobs = sum(v["jobs"] for v in g)
    if not jobs:
        return "–"
    b = sum(v.get("batch_like", 0) for v in g)
    return f"{b / jobs * 100:.0f}% ({b})"


def jobs_cell(segs):
    tracked = [s["jobs"] for s in segs if s.get("jobs")]
    if not tracked:
        return "–"
    tot = {k: sum(t.get(k, 0) for t in tracked) for k in ("received", "completed", "incomplete")}
    return f"{tot['received']} / {tot['completed']} / {tot['incomplete']}"


def log_cell(log):
    if not log:
        return "–"
    kinds = [f"{k[4:]} {v}" for k, v in sorted(log.items())
             if k not in ("log_lines", "log_errors", "log_private") and v]
    hidden = f", {log.get('log_private', 0)} hidden" if log.get("log_private") else ""
    return f"{log.get('log_errors', 0)}{hidden}" + (f" ({', '.join(kinds)})" if kinds else "")


def fmt(x, digits=1):
    return "–" if x is None else f"{x:.{digits}f}"


def cmd_pause(args):
    state = read_json(STATE, {})
    state["paused"] = True
    state["pause_reason"] = " ".join(args) or "paused by hand"
    write_json(STATE, state)
    event("paused", reason=state["pause_reason"])
    print("paused")
    return 0


def cmd_resume(_args):
    state = read_json(STATE, {})
    state["paused"] = False
    state.pop("pause_reason", None)
    protocol = read_json(PROTOCOL, None)
    cur = current_block(protocol, now()) if protocol else None
    if cur and cur["id"] in state.get("blocks", {}):
        # The current block was disturbed: it stays flagged as paused (left
        # out of the numbers) and restarts from its scheduled arm.
        info = state["blocks"][cur["id"]]
        for s in info.get("segments", []):
            s.setdefault("end", now())
        info["segments"].append({"arm": cur["arm"], "models": ARMS[cur["arm"]]["models"], "requested_at": now(),
                                 "attempts": 0, "ok": False, "scheduled": True, "reason": "resumed"})
    write_json(STATE, state)
    event("resumed")
    print("resumed")
    return 0


def cmd_status(_args):
    protocol = read_json(PROTOCOL, None)
    state = read_json(STATE, {})
    cur = current_block(protocol, now()) if protocol else None
    nxt = next((b for b in protocol["blocks"] if b["start"] > now()), None) if protocol else None
    seg = None
    if cur and state.get("blocks", {}).get(cur["id"], {}).get("segments"):
        s = state["blocks"][cur["id"]]["segments"][-1]
        seg = {"arm": s["arm"], "label": arm_label(state, s["arm"]), "models": s["models"],
               "ok": s.get("ok"), "since": iso(s.get("applied_at") or s["requested_at"]), "reason": s.get("reason")}
    print(json.dumps({
        "planned": bool(protocol),
        "paused": bool(state.get("paused")),
        "pause_reason": state.get("pause_reason"),
        "finished": bool(state.get("finished")),
        "current": cur and dict(cur, label=ARMS[cur["arm"]]["label"], models=ARMS[cur["arm"]]["models"], segment=seg),
        "next": nxt and dict(nxt, label=ARMS[nxt["arm"]]["label"]),
        "blocks_done": len(state.get("summarized", [])),
        "blocks_total": len(protocol["blocks"]) if protocol else 0,
        "candidates": {k: v.get("status") for k, v in state.get("candidates", {}).items()},
    }, indent=2))
    return 0


COMMANDS = {"plan": cmd_plan, "tick": cmd_tick, "report": cmd_report, "compare": cmd_compare,
            "pause": cmd_pause, "resume": cmd_resume, "status": cmd_status}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(2)
    sys.exit(COMMANDS[sys.argv[1]](sys.argv[2:]) or 0)
