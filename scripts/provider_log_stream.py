#!/usr/bin/env python3
"""Follows the Darkbloom provider's log continuously (LaunchAgent
io.darkbloom.log-stream, KeepAlive).

Why a stream: the provider logs each job's arrival and completion at Info
level, and macOS keeps Info messages only briefly in memory, never on disk.
Polling every 5 minutes (experiment.py until v82) mostly caught them, but
not always - a paid job was once counted as never completed because its
Complete line was gone before the next poll. `log stream` sees every line.

Writes, using the parsing in experiment.py:
  ~/.darkbloom/jobs/jobs-YYYY-MM.jsonl        one row per job (ids, times,
                                              token counts - never text)
  ~/.darkbloom/provider-log-counts.jsonl      per-minute counts of log lines
                                              and Error/Fault messages by kind
  ~/.darkbloom/provider-log-stream.json       open jobs and last position,
                                              so a restart resumes cleanly

On start it catches up on what is still in memory since the last line it
saw (`darkbloom logs --last ...`), then streams.
"""
import datetime as dt
import json
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import experiment as x  # noqa: E402

STATE_PATH = x.DB / "provider-log-stream.json"
COUNTS_PATH = x.DB / "provider-log-counts.jsonl"
PREDICATE = 'subsystem == "dev.darkbloom.provider"'
SAVE_EVERY_SEC = 30
HOSTED_CACHE_SEC = 10


class Tracker:
    def __init__(self):
        st = x.read_json(STATE_PATH, {})
        self.open_jobs = st.get("open_jobs", {})
        self.last_ts = st.get("last_ts") or time.time() - 600
        self.minute = None
        self.counts = {}
        self.saved_at = 0.0
        self.hosted, self.hosted_at = [], 0.0

    def current_hosted(self):
        if time.time() - self.hosted_at > HOSTED_CACHE_SEC:
            self.hosted = x.daemon_snapshot().get("hosted") or []
            self.hosted_at = time.time()
        return self.hosted

    def flush_minute(self):
        if self.minute is not None and self.counts:
            x.append_jsonl(COUNTS_PATH, dict(minute=self.minute, **self.counts))
        self.counts = {}

    def handle(self, d):
        try:
            ts = dt.datetime.strptime(d["timestamp"], "%Y-%m-%d %H:%M:%S.%f%z").timestamp()
        except Exception:
            return
        if ts <= self.last_ts:
            return
        self.last_ts = ts
        minute = int(ts // 60) * 60
        if minute != self.minute:
            self.flush_minute()
            self.minute = minute
        c = self.counts
        c["log_lines"] = c.get("log_lines", 0) + 1
        x.track_job_line(self.open_jobs, d, ts, self.current_hosted())
        if d.get("messageType") in ("Error", "Fault"):
            c["log_errors"] = c.get("log_errors", 0) + 1
            msg = (d.get("eventMessage") or "").lower()
            if msg == "<private>":
                key = "log_private"
            else:
                key = "log_" + next((k for k, words in x.LOG_KINDS if any(w in msg for w in words)), "other")
            c[key] = c.get(key, 0) + 1
        self.maybe_save()

    def sweep(self):
        for jid, j in list(self.open_jobs.items()):
            if time.time() - (j.get("received") or 0) > x.JOB_GIVE_UP_SEC:
                x.write_job(dict(j, id=jid, outcome="incomplete"))
                del self.open_jobs[jid]

    def maybe_save(self, force=False):
        if force or time.time() - self.saved_at > SAVE_EVERY_SEC:
            self.sweep()
            x.write_json(STATE_PATH, {"open_jobs": self.open_jobs, "last_ts": self.last_ts,
                                      "saved_at": time.time()})
            self.saved_at = time.time()


def catch_up(tr):
    span = int(min(max(time.time() - tr.last_ts, 60), 6 * 3600)) + 5
    try:
        out = subprocess.run([str(x.DARKBLOOM_BIN), "logs", "--last", f"{span}s", "--debug"],
                             capture_output=True, text=True, timeout=120).stdout
    except Exception:
        return
    for line in out.splitlines():
        try:
            tr.handle(json.loads(line))
        except Exception:
            continue


def main():
    tr = Tracker()
    stop = {"now": False}

    def on_term(*_):
        stop["now"] = True
    signal.signal(signal.SIGTERM, on_term)

    catch_up(tr)
    proc = subprocess.Popen(["/usr/bin/log", "stream", "--predicate", PREDICATE,
                             "--level", "debug", "--style", "ndjson"],
                            stdout=subprocess.PIPE, text=True, bufsize=1)
    try:
        for line in proc.stdout:
            if stop["now"]:
                break
            if not line.startswith("{"):
                continue
            try:
                tr.handle(json.loads(line))
            except Exception:
                continue
    finally:
        proc.terminate()
        tr.flush_minute()
        tr.maybe_save(force=True)
    # launchd (KeepAlive) restarts us if the stream ever ends.
    return 0 if stop["now"] else 1


if __name__ == "__main__":
    sys.exit(main())
