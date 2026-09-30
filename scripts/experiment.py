#!/usr/bin/env python3
"""Model-choice experiment for a Darkbloom provider.

Runs a pre-registered, randomized schedule of 3-hour blocks, each hosting one
"arm" (a fixed set of models), and records what happened so questions like
these can be answered with data instead of impressions:

  H1  Does hosting a second model next to gpt-oss-20b change how many
      gpt-oss jobs this Mac gets (per hour, and as a share of network demand)?
  H2  Which other downloaded models get any traffic here at all, relative
      to their published network demand (a sample, so an index, not a share)?
  H3  Zero-job stretches (like 2026-09-29 00:00-20:30): how often, under
      which arm, and what the provider reported meanwhile.
  H4  Does the base reward depend on what is hosted?

Learning comes first, income second: blocks run their full length even when
an arm earns nothing. The schedule is a Latin square over 8 days, so every
arm entry lands in every 3-hour slot of the day exactly once, and
time-of-day demand cannot masquerade as a model effect.

Subcommands:
  plan [--days-offset N] [--seed S] [--force]   write protocol.json
  tick                                          run by launchd every 5 min
  report [--hours H]                            markdown summary to stdout
  pause [reason] | resume                       stop/restart switching
  status                                        one-line JSON state

Files live in ~/.darkbloom/experiment/. Switching goes through the
dashboard's /api/model_set, so purge/switch/load/plist-sync logic lives in
one place (server.py).
"""
import datetime as dt
import fcntl
import json
import random
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
    return rows


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


def summarize_block(block, state):
    info = state.get("blocks", {}).get(block["id"], {})
    applied_at = info.get("applied_at") or block["start"]
    settle_end = applied_at + SETTLE_MIN * 60
    per_model = {}
    base_usd = 0
    settle_jobs = 0
    last_job = None
    longest_gap = 0.0
    prev = max(settle_end, block["start"])
    for t, e in sorted(ledger_between(block["start"], block["end"]), key=lambda x: x[0]):
        usd = (e.get("amount_micro_usd") or 0) / 1e6
        if e.get("model") == "base_reward":
            base_usd += usd
            continue
        if t < settle_end:
            settle_jobs += 1
            continue
        m = per_model.setdefault(e["model"], {"jobs": 0, "usd": 0.0, "completion_tokens": 0, "prompt_tokens": 0})
        m["jobs"] += 1
        m["usd"] += usd
        m["completion_tokens"] += e.get("completion_tokens") or 0
        m["prompt_tokens"] += e.get("prompt_tokens") or 0
        longest_gap = max(longest_gap, t - prev)
        prev = t
        last_job = t
    longest_gap = max(longest_gap, block["end"] - prev)
    hours = (block["end"] - max(settle_end, block["start"])) / 3600
    wh, cost = energy_between(block["start"], block["end"])
    return {
        "id": block["id"],
        "arm": block["arm"],
        "models": ARMS[block["arm"]]["models"],
        "start": block["start"],
        "end": block["end"],
        "start_local": iso(block["start"]),
        "applied_ok": info.get("ok"),
        "applied_at": iso(applied_at),
        "apply_sec": info.get("apply_sec"),
        "fell_back": info.get("fell_back", False),
        "paused": info.get("paused", False),
        "measured_hours": round(hours, 3),
        "per_model": {k: dict(v, usd=round(v["usd"], 6)) for k, v in per_model.items()},
        "paid_jobs": sum(v["jobs"] for v in per_model.values()),
        "paid_usd": round(sum(v["usd"] for v in per_model.values()), 6),
        "base_usd": round(base_usd, 6),
        "settle_jobs": settle_jobs,
        "longest_gap_min": round(longest_gap / 60, 1),
        "last_job_at": iso(last_job) if last_job else None,
        "energy_wh": wh,
        "energy_cost_sek": cost,
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


def _tick():
    protocol = read_json(PROTOCOL, None)
    state = read_json(STATE, {})
    state.setdefault("blocks", {})
    archive_network_demand(state)
    if not protocol:
        write_json(STATE, state)
        return 0

    ts = now()
    snap = daemon_snapshot()
    block = current_block(protocol, ts)

    # Summarize every finished block exactly once.
    done = set(state.get("summarized", []))
    for b in protocol["blocks"]:
        if b["end"] <= ts and b["id"] not in done and b["id"] in state["blocks"]:
            append_jsonl(BLOCKS, summarize_block(b, state))
            done.add(b["id"])
    state["summarized"] = sorted(done)

    if block is None:
        if ts >= protocol["blocks"][-1]["end"] and not state.get("finished"):
            state["finished"] = True
            ok, msg, sec = apply_models(ARMS[BASELINE]["models"], "experiment finished")
            event("finished", back_to_baseline=ok, message=msg)
        write_json(STATE, state)
        return 0

    info = state["blocks"].setdefault(block["id"], {"arm": block["arm"], "attempts": 0})
    wanted = ARMS[block["arm"]]["models"]
    append_jsonl(SAMPLES, dict(at=iso(ts), block=block["id"], arm=block["arm"],
                               paused=bool(state.get("paused")), **snap))

    if state.get("paused"):
        info["paused"] = True
        write_json(STATE, state)
        return 0

    target = ARMS[BASELINE]["models"] if info.get("fell_back") else wanted
    hosted = sorted(snap["hosted"])

    # Someone changed the models by hand after we applied them: step aside.
    if info.get("ok") and snap["pid"] and hosted and hosted != sorted(target) \
            and ts - info.get("applied_at", ts) > 120:
        state["paused"] = True
        state["pause_reason"] = f"manual change detected: hosting {hosted}, block wants {sorted(target)}"
        info["paused"] = True
        event("paused", reason=state["pause_reason"], block=block["id"])
        write_json(STATE, state)
        return 0

    if info.get("ok") or info.get("fell_back_ok"):
        write_json(STATE, state)
        return 0

    if not info.get("fell_back") and info["attempts"] < MAX_APPLY_ATTEMPTS:
        info["attempts"] += 1
        write_json(STATE, state)
        ok, msg, sec = apply_models(wanted, f"experiment {block['id']} arm {block['arm']}")
        event("apply", block=block["id"], arm=block["arm"], models=wanted, ok=ok,
              message=msg, seconds=sec, attempt=info["attempts"])
        state = read_json(STATE, state)
        info = state["blocks"][block["id"]]
        if ok:
            info.update(ok=True, applied_at=now(), apply_sec=sec)
        elif info["attempts"] >= MAX_APPLY_ATTEMPTS:
            info["fell_back"] = True
        write_json(STATE, state)
        if ok or not info.get("fell_back"):
            return 0

    # The arm could not be served: run the baseline for the rest of the block
    # (recorded as fell_back, so the report can leave it out of the arm).
    ok, msg, sec = apply_models(ARMS[BASELINE]["models"], f"experiment {block['id']} fallback")
    event("fallback", block=block["id"], arm=block["arm"], ok=ok, message=msg, seconds=sec)
    state = read_json(STATE, state)
    state["blocks"][block["id"]]["fell_back_ok"] = ok
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
    """Network requests for a model over [start, end), prorating the partial
    hours at both ends. Returns None if any covered hour is unpublished."""
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


def fmt(x, digits=1):
    return "–" if x is None else f"{x:.{digits}f}"


def cmd_report(args):
    protocol = read_json(PROTOCOL, None)
    state = read_json(STATE, {})
    if not protocol:
        print("No experiment planned.")
        return 0
    hours_back = float(args[args.index("--hours") + 1]) if "--hours" in args else None
    blocks = {b["id"]: b for b in read_jsonl(BLOCKS)}  # last summary wins
    net = network_by_hour()
    ts = now()
    lines = [f"# Model experiment report ({iso(ts)[:16]})", ""]
    total = len(protocol["blocks"])
    lines.append(f"Blocks done: {len(blocks)}/{total}. "
                 f"Paused: {'yes - ' + state.get('pause_reason', '') if state.get('paused') else 'no'}.")
    cur = current_block(protocol, ts)
    if cur:
        lines.append(f"Now: {cur['id']} arm {cur['arm']} ({ARMS[cur['arm']]['label']}) until {iso(cur['end'])[11:16]}.")
    lines.append("")

    # Per-arm table over all valid blocks.
    lines += ["## Per arm (valid blocks, after the settle period)", "",
              "| Arm | Blocks | Paid jobs/h | Paid $/h | Base $/h | Jobs per 100 published network requests | Longest gap (median, min) |",
              "|---|---|---|---|---|---|---|"]
    for arm, spec in ARMS.items():
        bs = [b for b in blocks.values() if b["arm"] == arm and b.get("applied_ok") and not b.get("fell_back") and not b.get("paused")]
        if not bs:
            lines.append(f"| {arm} {spec['label']} | 0 | – | – | – | – | – |")
            continue
        h = sum(b["measured_hours"] for b in bs)
        jobs = sum(b["paid_jobs"] for b in bs)
        paid = sum(b["paid_usd"] for b in bs)
        base = sum(b["base_usd"] for b in bs)
        shares = []
        for model in spec["models"]:
            ours = theirs = 0.0
            complete = True
            for b in bs:
                start = b["end"] - b["measured_hours"] * 3600
                n = network_requests(net, model, start, b["end"])
                if n is None:
                    complete = False
                    continue
                theirs += n
                ours += sum(v["jobs"] for k, v in b["per_model"].items() if same_model(k, model))
            share = f"{ours / theirs * 100:.1f}" if theirs else "–"
            shares.append(f"{model.split('-')[0]} {share}{'' if complete else '*'}")
        gaps = sorted(b["longest_gap_min"] for b in bs)
        lines.append(f"| {arm} {spec['label']} | {len(bs)} | {jobs / h:.0f} | {paid / h:.4f} | {base / h:.4f} | "
                     f"{'; '.join(shares)} | {gaps[len(gaps) // 2]:.0f} |")
    lines += ["", "The published network counts are a privacy-filtered sample, not all traffic (on 2026-09-29 21-24 h they showed 872 gpt-oss requests while this Mac alone served 2829), so the index can exceed 100. Compare it between arms, not as a market share.",
              "\\* some hours not yet published by Darkbloom (≥1 h lag); the index covers the published part.", ""]

    # H1: gpt-oss with and without a companion.
    def gpt_rate(arm):
        bs = [b for b in blocks.values() if b["arm"] == arm and b.get("applied_ok") and not b.get("fell_back") and not b.get("paused")]
        rates = [sum(v["jobs"] for k, v in b["per_model"].items() if k.startswith("gpt-oss")) / b["measured_hours"] for b in bs]
        return rates
    a, b_ = gpt_rate("A"), gpt_rate("B")
    if a and b_:
        med = lambda xs: sorted(xs)[len(xs) // 2]
        lines += ["## H1: gpt-oss jobs/h alone vs. with Qwen3.5-9B", "",
                  f"A (alone): n={len(a)}, median {med(a):.0f}/h, mean {sum(a) / len(a):.0f}/h",
                  f"B (with companion): n={len(b_)}, median {med(b_):.0f}/h, mean {sum(b_) / len(b_):.0f}/h",
                  "Too early to conclude while n < 6 per arm." if min(len(a), len(b_)) < 6 else "", ""]

    # Recent blocks.
    recent = sorted(blocks.values(), key=lambda b: b["start"])
    if hours_back:
        recent = [b for b in recent if b["end"] >= ts - hours_back * 3600]
    lines += ["## Blocks" + (f" (last {hours_back:.0f} h)" if hours_back else ""), "",
              "| Block | Start | Arm | Jobs | Paid $ | Base $ | Longest gap min | kWh | Note |", "|---|---|---|---|---|---|---|---|---|"]
    for b in recent:
        note = "fell back to A" if b.get("fell_back") else ("paused" if b.get("paused") else ("apply failed" if not b.get("applied_ok") else ""))
        models = ", ".join(f"{k.split('-')[0]}:{v['jobs']}" for k, v in b["per_model"].items())
        lines.append(f"| {b['id']} | {b['start_local'][5:16].replace('T', ' ')} | {b['arm']} | {b['paid_jobs']} ({models}) | "
                     f"{b['paid_usd']:.4f} | {b['base_usd']:.4f} | {b['longest_gap_min']:.0f} | {b['energy_wh'] / 1000:.2f} | {note} |")

    evs = [e for e in read_jsonl(EVENTS) if e["kind"] in ("apply", "fallback", "paused", "finished")]
    fails = [e for e in evs if e["kind"] in ("apply", "fallback") and not e.get("ok")]
    if fails:
        lines += ["", "## Failed switches", ""] + [f"- {e['at'][5:16]} {e.get('block')} {e['kind']}: {e.get('message', '')}" for e in fails[-10:]]
    print("\n".join(lines))
    return 0


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
        # The current block was disturbed; re-apply its arm, but keep it
        # flagged so the report leaves it out.
        state["blocks"][cur["id"]].update(ok=False, attempts=0, paused=True)
    write_json(STATE, state)
    event("resumed")
    print("resumed")
    return 0


def cmd_status(_args):
    protocol = read_json(PROTOCOL, None)
    state = read_json(STATE, {})
    cur = current_block(protocol, now()) if protocol else None
    nxt = next((b for b in protocol["blocks"] if b["start"] > now()), None) if protocol else None
    print(json.dumps({
        "planned": bool(protocol),
        "paused": bool(state.get("paused")),
        "pause_reason": state.get("pause_reason"),
        "finished": bool(state.get("finished")),
        "current": cur and dict(cur, label=ARMS[cur["arm"]]["label"], models=ARMS[cur["arm"]]["models"],
                               applied=state.get("blocks", {}).get(cur["id"])),
        "next": nxt and dict(nxt, label=ARMS[nxt["arm"]]["label"]),
        "blocks_done": len(state.get("summarized", [])),
        "blocks_total": len(protocol["blocks"]) if protocol else 0,
    }, indent=2))
    return 0


COMMANDS = {"plan": cmd_plan, "tick": cmd_tick, "report": cmd_report,
            "pause": cmd_pause, "resume": cmd_resume, "status": cmd_status}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(2)
    sys.exit(COMMANDS[sys.argv[1]](sys.argv[2:]) or 0)
