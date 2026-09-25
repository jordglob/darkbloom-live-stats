# Changelog

## v41

**Auto acts the moment it's switched on**, instead of at the next five-minute tick, and its first decision ignores the hysteresis timers. Those timers reference an action taken under a different regime — possibly by hand, possibly half an hour ago — so enforcing them means turning Auto on and watching nothing happen, which is indistinguishable from it being broken. Once Auto has acted once, its own hysteresis resumes normally. Verified against the real `_evaluate_price_guard()`: Auto just switched on → starts; Auto's own stop two minutes ago → waits; manual stop → starts.

**New serving mode: local only.** A fourth option in Price Guard that declines paid work but keeps the models loaded and the local OpenAI endpoint answering, so the chat box still works. It restarts the provider with `darkbloom start --local`, which serves the same models over the same endpoint and never connects to the coordinator. Price Guard's Auto is blocked from starting network serving underneath it.

That took three attempts, and the failures are worth recording:

1. **`darkbloom start --local` never returns.** Unlike a normal start it runs in the foreground and registers no launchd service, so `subprocess.run(timeout=60)` killed it — leaving the provider stopped and the switch reporting failure. It now launches detached and polls the endpoint for readiness.
2. **`darkbloom stop` timed out while draining.** Its own default allows 600s to drain accepted requests; the call allowed 30. The stop is now bounded explicitly (`--timeout 45`) with a longer wait around it — which also fixes the same latent hazard in Price Guard's ordinary stop path.
3. **The readiness check reported a false positive.** With the stop having silently failed, the *old* network provider was still on port 8000, so polling the endpoint found an answer and declared the new local process up. Two providers running, and a status file that lied about which. `set_serving_mode()` now checks the stop actually succeeded and waits for the daemon to really be down before starting anything.

Verified end to end in both directions: local-only leaves one process owning port 8000 with `Trust: awaiting coordinator status`, inference answers both directly and through the dashboard's chat proxy, and switching back restores hardware trust with the model warm.

## v40

**Fixed: Auto refused to resume for up to 30 minutes after a manual stop.** Caught live. The sequence:

```
06:22:41  mode set to manual via dashboard     (picked "Manual — stop")
06:22:43  STOPPED: manual stop via dashboard
06:28:12  mode set to auto via dashboard       (switched back to Auto)
          ... nothing for five minutes ...
06:33:00  STARTED: manual start via dashboard  (started by hand)
```

Auto wasn't holding off because of price — electricity was at 1.344 against a 7.494 break-even, deeply profitable. It was `min_stopped_min` (30 by default), which blocks a start until that long has passed *since the last action*. The last action was the user's own manual stop, so Auto sat idle until 06:52 waiting out a timer meant for something else.

Those guards exist so Auto can't flap either side of the break-even price — they're Auto's own hysteresis. A manual start or stop is a human decision and can't oscillate, so making Auto wait one out just parks the provider for no reason, earning nothing, with only a reason string buried in the Price Guard panel to say why.

`last_action_source` has been recorded since v19; `_evaluate_price_guard()` now consults it and zeroes both timers when the previous action was manual. Same blind spot as the v19 banner bug, in fact: the timer knew *when* the last action happened but not *who* made it.

## v39

**The chat now explains itself when serving is stopped, and offers the way back.** It was already disabling correctly and showing "unavailable - the provider daemon is not running right now" — the chat talks to the same local inference endpoint paid work goes through, so no daemon means no model loaded. But two things made a correct state read as a broken page:

- The textarea kept its inviting placeholder, *"Ask the loaded model something..."*, inside a greyed-out box. It now states the actual reason: *"Serving is stopped, so the model isn't loaded - start it above to chat."*
- There was no way to act on it. Everywhere else a stopped provider is reported, the banner carries a Start link; here you were told what was wrong and left to find the switch in a different panel. The status line now offers **Start serving**, wired to the same `postPriceGuardAction('start')` the Price Guard controls use.

The link only appears when the daemon is the reason — being busy with real paid traffic is a different state, and one nobody should be offered a button to interrupt.

## v38

**Removed the "Net (Estimated) Over Time" chart.** It was the chart above it with its two lines subtracted — server-side, `net_usd` is literally `[rev - cost for rev, cost in zip(est_revenue_usd, cum_cost_usd)]`. Same data, same axis, stacked directly beneath its own source. There was no reading it supported that the pair above didn't.

**"Cumulative Electricity Cost vs. Estimated Token Revenue" is now "Tokens generated — full history".** Its dollar figure multiplied tokens by a flat guessed rate and excluded the base reward — which is 56% of this account's income — so it read far worse than reality and carried a paragraph of apology underneath. But it *was* the only long-range series: the real-earnings chart is capped at about two days because Darkbloom's API only returns its most recent batch of ledger entries.

So the axis changed rather than the chart being deleted. Token volume was always the part worth plotting over 26 days; the dollar conversion was the part that was wrong. Reads `27M tokens · ≈ 21M words since Aug 29`.

The counter needed care: `tokens` in the CSV is the daemon's own lifetime count and resets to zero on every daemon restart, so plotting it raw gives a sawtooth. `get_energy_series()` now accumulates per-row deltas, treating a decrease as a restart where the whole new value is newly earned — the same rule `get_utilization()` already applied to its window. A restart shows as a flat step, never a drop. Verified monotonic across the full 300-point series.

**The price chart's per-slot Net overlay is off by default, behind a checkbox.** It answers a real question — which hours were actually worth running — but it crosses zero constantly and was the noisiest element on the page, drawn over the two price lines it was meant to annotate.

## v37

The four remaining points from the first-time-reader review.

**Serving load is paired with the base-reward share.** A bare busy-ness percentage reads as a score you're failing, and a newcomer can't know there's nothing to fail at — the number reflects how much traffic the network sent, which the provider doesn't control. It now reads `143 requests/h · 102k tokens/h · 56% of earnings comes from being online regardless`, which is the fact that makes a quiet hour unalarming.

**Warnings have two tiers instead of one colour.** Yellow was doing double duty: "known quirk, ignore it" and "go look at this" appeared identically, so three yellow lines made a working machine look broken. Health-check warnings now follow the same rule the top banner already used — if requests are visibly succeeding, a *warning* is by definition not blocking anything, so it renders grey with an explicit `note, no action needed —` prefix and turns yellow only if serving stops. The spam-heuristic line is demoted the same way below 1% of jobs, since 0% is noise rather than a pattern.

**The load-test caption is permanent.** It warned about burning unpaid power only *after* you moved the slider — the wrong order for the one control on this page that costs money. A grey line now sits under the checkbox at all times: *"Load test burns power nobody is paying for and competes with real paid inference — it is off unless you tick it."*

**"will NOT match the Net card, read why" now states the reason inline.** Leading with what a number *isn't* makes it sound untrustworthy. The heading reads "Estimated cost vs. revenue over full history (excludes the base reward — that's why it reads lower than Net)", so the discrepancy is explained where it's encountered rather than deferred to a footnote.

## v36

Changes from reading the dashboard as someone opening it for the first time.

**Panel order follows what a newcomer needs, not what was built first.** "Real Earnings vs. Electricity Cost" — green line above blue, readable without explanation, answers the only question a new provider has — was at position 10, about 3.5 screens down. It's now directly under the live gauges. The log-age chart moves down to sit with the other diagnostics: it's the best view *if you already know what you're looking at*, but leading with a logarithmic axis, decade bands, a load-test slider and four series toggles asks a lot of someone on their first visit.

**The Net card gives a daily rate.** The balance is a lifetime figure and doesn't answer "is leaving this Mac on worth it". It now reads `balance $19.93 − electricity · ≈ $0.6625/day`, measured from the same real earnings-minus-cost window the 48h chart already sums — not projected from a quoted rate.

**Gauges reordered and "GPU Headroom" renamed to "GPU Busy".** Headroom was shown inverted, which reads as wasted capacity when it means the opposite, and it carried a red zone implying that idling was a fault to fix. It isn't: idle means the network sent no work, and the base reward pays either way. The colour warning is gone with it. The order is now serving load, temperature, fan, total power, GPU busy — then CPU/GPU watts and RAM last, which are for diagnosing rather than deciding.

**Fixed: the log-age panel was never closed.** Its `</div>` went missing when the panel was moved in v35, so the Darkbloom Account panel was nested *inside* it. Browsers silently repair this, which is why nothing looked wrong — it surfaced only when reordering panels programmatically and the boundaries overlapped.

## v35

**The log-age chart is now the main chart** — moved directly under the live gauges, since it's the one that earns the position: it's the only view that fits a 30-second fan oscillation and a 27-day trend in one frame.

**Series toggles.** A row of checkboxes with colour swatches turns each layer on and off independently: GPU temp, peak band, CPU temp, fan %. Toggle state lives in JS rather than being read from the DOM per frame, so the chart can still redraw at the live cadence without touching the document.

**CPU temperature added** as a togglable series. macmon has been logging `cpu_temp_avg` all along next to the GPU reading and nothing used it. It shares the left axis — same unit — and answers "which of the two is actually producing the heat". It stops where macmon's coverage does rather than being faked from the CSV.

**Peak band on the log-age chart.** It had the data but drew only the average line, which is why the older log-time chart looked richer. The band's *width* is the interesting part here: bins near "now" span seconds, so peak collapses onto the mean and the band closes to a line; bins at the old end span days and it opens up. Measured: 148 h back the spread is 40.4 °C, at 5.6 s it's 0.0. On an axis that deliberately distorts time, that gap shows how much each point is summarising.

Plus the three fixes from the chart review:

- **Removed the log-time chart (v23).** Demonstrably the weakest of the three: its right half was ~2 hours of flat interpolation across half the width, because equal-duration buckets stretched on a log axis add width without adding data. The log-age chart does what it was reaching for, with a real resolution pyramid behind it. Removing it also retired the `logTime` option, `nearestIdxToX`, and the `*_full` server fields that existed only to feed it — `renderLineChart` is back to roughly its pre-v23 shape.
- **The earnings chart stops at "now".** It borrowed the price panel's grid, which runs into tomorrow so a forecast has somewhere to go — but you cannot have earned tomorrow's money, so ~40 % of it was permanently blank. Now ends at the last elapsed bucket; verified the data reaches the exact plot edge (914 of 914 px).
- **The power chart's peak is a band**, matching the temp chart. Two charts showing the same relationship shouldn't speak two different visual languages on one page; a dashed line above the data reads as a limit, a band reads as spread.

Not split into separate commits as originally planned — the work was redirected partway through and the edits interleave in the same functions, so splitting afterwards would have been fictional.

## v34

**Decade banding on the log-age chart.** Alternate decades now carry a faint background tint. The one thing a reader has to grasp about this chart is that equal width is *not* equal time — near the right edge a centimetre is seconds, near the left it's weeks — and shading alternate decades makes that structural rather than something to infer from the tick labels. Same convention as log graph paper.

**Banded on true powers of ten, not on the tick labels.** The first attempt banded between adjacent `AGE_GRID` entries, which was wrong: those ticks are chosen to be human-readable (1h, 6h, 1d, 1w) and are deliberately *not* uniform steps — 1h→6h is ×6, 1d→7d is ×7. Banding them produced visibly unequal bands, which disproves the exact thing the banding exists to demonstrate. Now every interior band is one factor of ten and therefore identical in width: verified against the axis geometry at **126.1 px per decade across all five**.

Also fixes a label collision the banding made obvious: the long left-edge label ("27d since install") was rendering on top of the neighbouring `1w` tick. Ticks falling within the edge label's width are now skipped.

## v33

Three fixes to the load-test control:

- **A checkbox arms it.** The slider is disabled and dimmed until "Load test" is ticked, so its off-state is unmistakable rather than being inferred from a slider sitting at zero. Ticking the box arms the slider at 0 rather than starting anything — turning it on shouldn't silently begin burning power; the level stays a deliberate second choice.
- **The axis label reads "27d since install"**, not a bare duration or an ambiguous "· install".
- **The tooltip now names the tier a point actually came from.** It reported `~6s sample` for everything under 24h, which is wrong for points out of the browser's live buffer — and increasingly wrong during a load test, where that buffer runs at up to 0.4s. Three tiers now: `~0.4s live` (browser buffer, at its current cadence), `~6s sample` (macmon), `bucket avg` (CSV).

Verified the adaptive cache actually reaches the sensor at the faster rate, by probing `/api/live_power` every 0.5s: at level 0 the readings repeat in pairs (the 2s cache serving duplicates), at level 100 they change on nearly every probe and visibly climb. Without that change the faster polling would have redrawn the same stale number.

## v32

**Price Guard reduced to the three states it actually has.** The old UI split one decision across a mode pair (Manual/Auto) plus separate Start/Stop buttons plus, briefly, a pause panel of its own. That made "Manual" read like a state when it only ever meant "nothing automatic touches this". Now:

- **Auto** — price decides: stops when electricity costs more than serving earns, resumes when it doesn't.
- **Manual — start** — keep serving, whatever it costs.
- **Manual — stop** — stop and stay stopped.

The margin / min-running / min-stopped tuning only appears under Auto, since that's the only mode where it does anything.

**The pause feature is gone, and that's the simplification.** "Manual — stop" already is a pause: `price_guard_loop` only acts in Auto mode, so a manual stop is inherently sticky, and Darkbloom's watchdog leaves a CLI-issued stop alone. The separate `pause.json`, `pause_loop`, `/api/pause` and pause banner were all re-implementing something the mode switch gave for free. Deleted. The earnings warning survives, shown under "Manual — stop", still computed from this account's own measured base-reward and serving rates.

**Max power became a 0–100% slider, and moved onto the log-age chart** — where you can watch the result. `max-power.py` replaces the shell version: each worker duty-cycles inside a fixed window (busy for level% of it, asleep for the rest), which gives a smooth range instead of the coarse steps you'd get from varying worker count. The level lives in a file the workers re-read, so dragging the slider steers a running load without restarting anything, and the worker exits on its own when it reads 0.

Measured on this Mac:

| Slider | Power | GPU | GPU temp | Fan |
|---|---|---|---|---|
| 60% | 54.8W | 35% | 56.8°C | 2202 rpm (45%) |
| 100% | 86.9W | 93% | 77.4°C | 4286 rpm (87%) |

**Moving the slider also raises the sampling rate** — 2.0s at idle down to 0.4s at full load, with `get_fan_temp()`'s cache dropping from 2s to 0.35s to match (otherwise a faster client poll just re-reads a stale value), and the chart's axis floor following, so the view zooms into finer time as the load goes up. Watching the fan respond is the entire reason to turn the load up, so that's exactly when the resolution should be highest — and the extra polling cost is acceptable precisely because it's temporary and tied to a deliberate experiment.

**The slider warns, in watts and dollars**, whenever it's above zero: this burns power on work nobody is renting, earns nothing, and shares the GPU with real paid inference.

The log-age chart's left edge now reads "27d · install" rather than a bare duration, since that point is the first row ever logged rather than an arbitrary window edge.

## v31

**The fan now follows temperature.** This was the point of the whole exercise, and it wasn't fixed by v27 — that fix stopped the *oscillation*, but recovery still only engaged at 85°C. Below that the fan sat flat at its 1000rpm floor no matter how warm the GPU got, then slammed to 100%. Caught live: **GPU 75.3°C with the fan at 1000rpm, target 1000, "auto"** — while Darkbloom's own stated policy is "80.0% at 45.0 C".

Recovery now engages on a new, separate condition (`_fan_is_not_tracking()`): GPU ≥55°C with the fan still under 35%. That's a demonstrable "their helper isn't doing its job" signal rather than an emergency. `_is_running_hot()` stays at 85°C, because the Running Hot banner should keep meaning *emergency*. Release drops to ≤48°C to sit well below the new engage point.

The helper computes a single fan target from the temperature at that instant — it does not install a curve the SMC follows on its own — so **the poll interval IS the tracking resolution**. Dropped 30s → 5s. Measured under a real load run:

| GPU at engage | fan fraction applied |
|---|---|
| 55.6°C | 0.30 |
| 65.8°C | 0.55 |
| 68.1°C | 0.79 |

Proportional, and it released cleanly each time (down to 40°C after a full 3-minute hold). Previously: flat at the floor until 85°C, then `1.000000`, then thrash.

**Max power button** (in Price Guard, next to Pause). Saturates every CPU core and the GPU via an MLX/Metal matmul loop, time-boxed and stoppable, so thermal and fan behaviour can be watched under real heat instead of waiting for paid traffic to produce it — this is what validated the change above. Measured: idle 7.1W → **112W**, GPU 4% → 100%, 40.9°C → 84.9°C. The Neural Engine is deliberately **not** loaded: reaching it needs a compiled CoreML model and neither coremltools nor Xcode is present here, and faking it would be worse than leaving it out honestly. Audio is never touched. The UI states plainly that it competes with real paid inference on the same GPU.

**Monitoring overhead cut to 0.95% of the machine** (the stated budget was 1%). `/api/data` was taking **3.2 seconds per call** every 10s — 32% of a core — because four slow subprocess calls were being re-run at the page's refresh rate. `get_disk_usage()` (models on disk, ~1.1s) is now cached 300s and `get_darkbloom_status()` (~0.9s) 5s; `doctor` was already cached; `temp_age` was dropped from `/api/data` since the chart has its own endpoint. Result: 3.2s → 0.2s, and the dashboard process went from 24.4% of a core to 0.3%. What remains is dominated by `powermetrics` (10.3% of a core), which is the irreducible cost of sampling power at 200ms.

**Pause moved into Price Guard and simplified** — one duration dropdown and one button instead of a panel of its own, since it belongs with the other start/stop controls.

**Fixed: the log-age chart stopped rendering.** Removing `temp_age` from `/api/data` (above) also removed the call that bootstrapped its polling timer — the timer is set up *by* the first render, so nothing ever started it. Now kicked off explicitly at page load.

## v30

**A third, live tier on the log-age chart, collected in the browser.** The same 2s poll that already drives the live gauges now also feeds a ring buffer, spliced onto the right edge of the chart. So the newest stretch is real live readings rather than the server's coarsest-available summary, and the chart moves when the gauges do.

The real gain here is **fan speed, not temperature**. macmon carries no fan RPM at all, so server-side the fan curve is 5-minute CSV rows plus a single live point — this is roughly 150× finer, and that resolution exists nowhere else on the machine. For temperature the improvement is 5.6s → 2s, which is mostly about feel: both already sample the ~30s fan oscillation comfortably.

**2s is the floor, not 200ms.** The 200ms power poll carries watts only — it deliberately reuses the last fan/temp values rather than re-fetching, because those need a `darkbloom fan status` subprocess. Spawning one five times a second on a machine whose whole purpose is selling compute would be a poor trade. `/api/live_power` at 2s is the finest honest source for these two metrics.

Details that matter:
- **The tiers have an explicit handover.** Server points are kept only where the live buffer doesn't reach; without that the two overlap and the curve doubles up across the whole buffered window.
- **The live tier is log-binned too**, the same way the server bins, so raw 2s samples don't pile into the compressed end of the axis and starve the stretched one.
- **Redraw and fetch are decoupled.** The chart redraws at the live cadence against the cached server payload — no extra requests — while the server data is still fetched at its own ~5.6s rate. Redrawing at 2s by re-fetching would have meant 30 requests/minute for data that changes 10 times a minute.
- **The axis floor follows the live cadence** (2s instead of 5.6s), so those samples get their own room rather than being clamped into the server's last bin. The right edge is now labelled with the actual finest resolution being sampled, mirroring the install-date label on the left, since that value rarely lands on a decade tick.
- **Per-tab and lost on reload, by design** — it's a buffer, not a store. After a refresh the chart draws from the server's tiers and this refills as you watch.

Verified live: both curves reach the plot's exact right edge (x=754 of 754), fan point count rose from 137 to 152, newest sample 1.9s old.

## v29

**Fixed: a dashboard restart while holding the fan left it pinned at full speed indefinitely.** `_fan_recovery_state` lived only in memory, and the release branch only runs when the loop believes it is holding. So restarting the dashboard mid-hold reset `active` to `False` while the fan stayed in SMC manual mode — nothing ever handed it back, whatever the temperature.

This happened for real tonight, caused by deploying the other fixes in this session: `ENGAGED at GPU 99.5C` at 20:46:49 with no matching `RELEASED`, then roughly **55 minutes at 4900 rpm with the GPU at 29–34°C**. `darkbloom fan status` confirmed it: `Fan 0: actual 4903, target 4900, manual` against `GPU: 34.1 C`. Full blast, on a cold machine, for no reason.

Worth stating plainly: **pausing serving would not have fixed this.** The fan was pinned at the SMC level, independent of whether the provider was serving anything — a paused Mac would have been exactly as loud.

Three changes:
- **The hold is persisted** to `fan-recovery-state.json` on every engage and release, so a restart doesn't lose the fact that we have the fan.
- **Startup reconciles.** A hold found in the state file is resumed only if the fan is genuinely still in manual, so the normal release logic can finish the job; otherwise the stale flag is cleared (and the cleared state is written back, rather than left on disk).
- **Clean shutdown releases the fan.** `launchctl kickstart -k` — which is how this gets redeployed — sends SIGTERM, exactly the case that stranded it. A signal handler plus `atexit` now hands the fan back before exiting, so the situation doesn't arise in the first place.

## v28

**Pause serving.** A deliberate, time-boxed stop — 1h / 4h / 8h / 24h, or until you resume — in its own panel, because "stop renting this out for a bit" previously meant knowing that you had to switch Price Guard to Manual *first* and only then press Stop. Pressing Stop while in Auto isn't a pause at all: Auto starts back up as soon as the price drops below break-even.

A pause now outranks everything else on the machine that has an opinion about whether the provider should run. Price Guard's Auto mode may still issue a stop while paused (a no-op, it's already down) but is explicitly blocked from starting back into one, and the pause doesn't touch the Price Guard mode, so whatever was configured there resumes working untouched afterwards. Darkbloom's own watchdog already leaves a CLI-issued stop alone — it only restarts on an unexpected drop (three times ever in this log, all early September), which is why the Price Guard's own auto-stop on 16 Sep held for 48 minutes untouched.

**The earnings warning is the point of the panel**, and it's computed from this account's own measured rates rather than a quoted figure: *"This account earns $0.0222/h just for being online and trusted — that's 82% of its hourly income, and it does not depend on serving any requests. Actually serving adds $0.0049/h on top. A 24h pause costs roughly $0.65."* The base reward is the part that surprises people: an idle-but-connected Mac still earns it, a paused one doesn't, and it's four times the serving income per hour on this account.

**Log-age chart now refreshes at the sampling rate** (~5.6s) instead of the page's general 10s cycle — a chart whose right edge is live readings should move when they do. Getting there meant not re-reading 12MB of macmon log per poll: the parsed samples are cached and only topped up from the tail of the log, while the binning (cheap) is redone against a fresh `now` on every request. It has its own `/api/temp_age` endpoint so it isn't dragging the whole `/api/data` payload along at that cadence, and `get_fan_temp()` picked up a 2s cache since it spawns a subprocess and now has three callers on three different schedules. Request time: ~40ms.

## v27

**Fixed: fan recovery was oscillating 488 times a day.** The loop engaged and released on the *same* condition — `_is_running_hot()`, which requires a hot GPU **and** a fan that isn't responding. But engaging drives the fan up, which falsifies the second half of that condition immediately. So recovery let go on the very next poll and handed the fan back to a controller already proven broken; the GPU reheated, and it engaged again.

Measured across 3.1 days of `fan-recovery.log`: **1517 engage/release cycles, 488 per day**, median hold **30 seconds** (exactly one poll interval, 95% of holds ≤35s), median gap 31s. **74% of releases happened with the GPU still above 70°C, 30% still above 80°C — and one at 100.5°C.** The loop was releasing at full throttle temperature because its own intervention had made the release condition true.

This is also what made the fan line look like noise in every chart: the 5-minute CSV was sampling a ~30-second square wave. The v25 log-age chart, at macmon's 5s resolution, was the first view that showed the oscillation directly.

Fix: engage and release are now separate conditions with a real dead band.
- **Engage** unchanged: GPU ≥85°C and fan below 50%.
- **Release** on temperature alone — ≤70°C — and never within 180s of engaging. `_is_running_hot()` is deliberately not consulted while active, since recovery itself holds the fan up and so can't be used to judge whether letting go is safe. A missing temperature reading holds rather than releases blind.

The decision is now a pure function, `_fan_recovery_decision()`, rather than inline branching — two conditions accidentally being one expression is a subtle enough mistake to be worth testing instead of re-reasoning about. Eight cases cover it, including the exact historical failure (active, 100.5°C, held 30s → hold, not release).

## v26

**Log-age chart, second pass.** Six changes, all driven by looking at the rendered result:

- **Fan speed is on it** (right axis, 0-100%, drawn as an area under the temperature line). It was missing entirely in v25.
- **Buckets moved out to 24h.** Real ~5s macmon readings now carry the curve across everything the axis shows in minutes *and* hours; 5-minute CSV rows only take over beyond a day. A dashed marker shows where the handover happens, since the curve deliberately doesn't break there.
- **No gap in the curve.** Both metrics are log-binned onto a shared axis by `_log_bin()`, and empty bins are *skipped* rather than emitted as nulls. That detail is the whole fix: on a log axis the bins near "now" are inevitably narrower than the sampling interval, so a null-per-empty-bin scheme shreds the recent end of the line into disconnected specks. Binning in log space also keeps plotted density even instead of piling thousands of points into the compressed old end while starving the stretched recent end.
- **The axis starts at the install date** - the first row ever logged (`2026-08-29`), read from the CSV rather than hardcoded, so it widens by itself as history accumulates.
- **The axis ends at the real measured sampling interval**, floored at 200ms. The interval is measured (median gap between actual samples), not assumed, so a stalled or restarted macmon widens the frame honestly instead of implying resolution that isn't there. In practice this reads ~5.6s.
- **The live fan and temperature readings are appended as the newest points.** The CSV only gains a fan row every 5 minutes, and a log axis stretches that latency into a large visual gap between where the fan curve stops and "now"; `_log_bin` clamps anything fresher than the frame's right edge into the newest bin instead of discarding it.

The title no longer claims "ms → years" - the axis reports what the data actually spans, because the frame is now derived from the data rather than chosen.

## v25

**Log-age chart with a resolution pyramid.** A third GPU-temperature view: x is `log(age)`, so "now" is the right edge and each vertical gridline is one step further into the past — 1ms, 10ms … 1s, 10s, 1min, 10min, 1h, 6h, 1d, 1w, 30d, 1y — with horizontal steps every 10°C. That grid is the point: on a log axis you can't judge distance by eye, so the decades have to be drawn.

What makes it worth having isn't the axis, it's the data behind it. The earlier log-time experiment (v23) stretched recent time across half the chart and revealed nothing, because `_bucket_downsample()` produces buckets of *equal* duration — bucket #299 summarises exactly as much time as bucket #1, so zooming into "now" was stretching a single number. This chart is fed two resolutions instead: **macmon's own ~5s SMC readings for the last hour** (600 unbucketed samples, via the new `get_fine_temp_samples()`), then **5-minute CSV buckets beyond that**. Buckets only take over once the axis is showing hours; below that you're looking at real readings.

That immediately showed something no bucketed chart could: between roughly 1h and 10min ago, GPU temperature oscillates violently between ~40°C and ~100°C, over and over. That's the fan-recovery loop thrashing (`fan-recovery.log` has ~1500 engage/release cycles on a ~30s period), and at 5-minute sampling it was invisible — the 5-min CSV aliases a 30-second oscillation, which is also why the fan line looked so jagged in the other charts.

Reading the fine layer costs nothing new: `energy-monitor.sh` already runs `macmon pipe -i 5000`, so this just tails a log that's being written anyway (seeking from the end — it's ~12MB and truncated on every macmon restart), cached 10s. macmon reports no fan RPM, so the fine layer is temperature only; fan speed stays on the 5-minute cadence.

Note on the frame: it spans 1ms to 1 year as specified, and roughly 60% of that is structurally empty — nothing on this machine samples faster than ~5s (and a thermal sensor doesn't meaningfully change at millisecond scale), while the 1y/30d end is empty simply because the log is 26 days old. The left end fills in with time; the right end can't.

## v24

**GPU Temp & Fan chart, redrawn.** The data was right after v22; how it was drawn wasn't. Five changes, each checked against the rendered chart rather than reasoned about:

- **The temperature axis no longer starts at 0°C.** It's pinned to a fixed 35–105°C. An auto-scaled 0-based axis spent half its height on a range this GPU never enters, squashing the entire real signal into the top band. A *fixed* range also means a given line height always means the same heat, and the 85°C mark never moves between refreshes.
- **Fan speed is on a fixed 0–100% axis** instead of auto-scaling to whatever the observed maximum happened to be (63%). Auto-scaling made a fan loafing at a third of capacity look like it was working hard; the fixed scale shows the headroom that's actually left.
- **Peak temperature is a shaded band up from the average line**, not a dashed line above it. A dashed line above the data reads as a limit or a target; a band reads as what it is - the spread between the typical and the worst reading in each bucket.
- **The 85°C "running hot" threshold is drawn on the chart**, the same number `_is_running_hot()` and the fan-recovery loop actually trigger on. A chart about overheating should say where too hot is.
- **Fan speed is a filled area rather than a second line.** Two lines sharing one plot area wove through each other and were genuinely hard to trace apart; a line over a filled region separates instantly because they're different kinds of mark.

Plus the layering fixes those exposed: the threshold line now draws above the band instead of being washed out by it, and the left-axis lines are re-appended above any right-axis area fill (SVG has no z-index - document order is paint order), so the primary line can't be buried where the two cross. Axis ticks take an optional coarser format than the tooltip, so a fixed axis stops printing a decimal that never varies.

One idea was built and then cut: shading the stretches where the data said "hot while the fan stayed low". Checked against the real history first, and 91% of buckets matched - a chart tinted end to end says nothing. The per-bucket approximation (a 2-hour peak against a 2-hour average fan) simply can't separate "brief spike, fan ramping fine" from "sustained heat, fan dead", so it was dropped rather than shipped as a misleading alarm.

## v23

**Experimental log-time comparison chart for GPU Temp & Fan.** A second panel, "GPU Temp & Fan — full history, log time", sits right below the fixed chart from v22 - same data, but plotted over the *entire* CSV history (not the trimmed window) with the x-axis spaced by `log(time since now)` instead of evenly by index. The ~21-day stretch before temp/fan logging existed compresses into a small sliver on the left instead of being cut off, while the recent, data-dense days get most of the chart's width - and within that recent stretch, the very latest points get progressively more room than older ones (verified: pixel gaps between adjacent points grow from ~0.5px five days back to ~109px for the most recent pair, out of a ~700px plot area). The original linear chart is untouched and unaffected - this is a side-by-side comparison, not a replacement, since a log-time x-axis is a real design tradeoff (harder to read at a glance) that's worth evaluating before deciding whether to keep it, drop it, or use it elsewhere.

`renderLineChart()` gained a `logTime` option; hover/tooltip index lookup was updated to find the nearest point by actual pixel position instead of assuming even spacing, and the right-axis series (fan%) got the same null-breaks-the-line handling the left axis already had - needed once a right series could carry real historical gaps, not just gaps from downsampling.

Caught two more bugs by actually looking at the rendered chart (screenshots were unreliable earlier in this work, so the log-time math had only been checked numerically until now): the log-age normalization divided by the max only, not min-to-max, so "now" landed around the chart's midpoint instead of the right edge; and the x-axis's fixed first/middle/last-by-*index* label picks put two labels on top of each other (readable as literal garbled text) once most indices got crammed into a narrow pixel band. Both fixed: log-age is now normalized so the newest point is pinned to the right edge and the oldest to the left, and label positions are chosen by target pixel position (deduping if two land on the same point) instead of by array index.

## v22

**Fixed: GPU Temp & Fan chart was misleading in three separate ways.** Found while re-checking it after the v21 fan-recovery fix landed:

- **Fan speed % could read over 100%** (a transient SMC RPM reading briefly above the reported max during a fan-mode transition) - now clamped to `[0, 100]`.
- **Fan speed was the bucket's last raw sample, not an average** - each of the chart's ~300 buckets spans roughly 2 hours of history, so a bucket where the fan was stuck low for 110 minutes but happened to recover in the final sample showed 100%, same as one where it never recovered at all. It's now bucket-averaged the same way GPU temp already was, excluding missing readings rather than treating them as 0.
- **67 of 300 points had any data at all** - temp/fan logging only started 2026-09-18, but the chart's x-axis went all the way back to when energy logging itself began (2026-08-29), squeezing the only informative ~6 days into the rightmost fifth of the chart. This one chart now gets its own trimmed timestamp axis (`temp_timestamps`), starting at the first real reading, instead of sharing the full-history axis used by the power/cost charts.

## v21

**Automatic fan recovery (optional, external tool).** Traced the "Running hot" bug to a real, well-documented upstream defect: [Layr-Labs/d-inference#551](https://github.com/Layr-Labs/d-inference/issues/551), open since 2026-07-15 with a reviewed fix ([PR #599](https://github.com/Layr-Labs/d-inference/pull/599)) that's never been merged - a GitHub permissions snag with their review agent, not a technical blocker. One implausible GPU sensor reading permanently wedges Darkbloom's own fan helper; the fan sits near its floor regardless of real temperature.

Evaluated three independent fan-control projects to fix it: [MacFanControl](https://github.com/raminsharifi/MacFanControl) (confirms the same root-cause fix, the M3/M4 "Ftst unlock" retry sequence, but only via an interactive TUI - not automatable, and a single unmaintained commit), [ThermalForge](https://github.com/ProducerGuy/ThermalForge) (most mature of the three, 126 commits, real auto-adjust mode, but general-purpose and installs its own persistent daemon), and [Justin Schroeder's darkbloom-monitor fan helper](https://github.com/justin-schroeder/darkbloom-monitor) (purpose-built for Darkbloom, direct SMC access, fails closed to macOS auto on any rejected write). Built and live-tested all three on this machine while the bug was actively happening (GPU 96-100°C, fan stuck at ~20%) - Justin's helper brought the fan to ~90% and GPU down to the low 60s within seconds.

Integrated the winner the same way `macmon` is already handled: optional, externally built, auto-detected. A new `fan_recovery_loop()` polls every 30s using the exact same "hot GPU + unresponsive fan" condition the banner already computes; if `~/.darkbloom/bin/darkbloom-fan-helper` exists, it calls `apply gpu 45 85` while the condition holds and `automatic` once it clears. No code from any of the three projects is vendored (two of the three have no license permitting it anyway) - see the README's new "Optional: automatic fan recovery" section for the one-time build step. The Running Hot banner now says "auto-recovery engaged" (grey, not a warning) when the helper is actively handling it, instead of just pointing at `darkbloom fan status`.

## v20

**GPU Temp & Fan history.** New chart logging GPU temperature and fan speed every 5 minutes (same source as the live gauges - `darkbloom fan status`, since Apple Silicon exposes neither through powermetrics), with average/peak temp on the left axis and fan speed (% of max RPM) on the right - so a Running Hot banner from earlier can be checked against the actual trend instead of just the live snapshot: if temp climbs while the fan line stays flat, that's the fan-control helper not responding, visible after the fact. Existing history before this change shows as a gap, not a false 0°C dip - missing temp readings are excluded from bucket averages rather than counted as zero.

## v19

**Fixed: switching Price Guard from Auto to Manual made an auto-stop invisible while the provider stayed stopped.** Caught live: Auto paused the provider at 08:14 (price above break-even), the mode was switched to Manual soon after expecting that to mean "just keep it running" - but Manual doesn't restart anything, and the one banner that explained why the provider was down only rendered `if (mode === 'auto')`, so it vanished the moment the mode changed even though nothing about the actual stopped state did. The provider sat idle for over 30 minutes with no on-page explanation.

Fix: a new `last_action_source` ("auto" | "manual") is recorded on every start/stop - by the Auto loop and by the manual buttons - and the top banner now keys off *who* issued the last stop rather than the current mode. Auto-stopped-then-switched-to-Manual now shows: "Provider is stopped. Auto paused it (…) before you switched to Manual mode — Manual doesn't resume it for you." with a clickable **Start now** right in the banner. A stop the user issued manually is never nagged about. Existing installs get this retroactively with no migration - the field is inferred once from the reason text already logged (`"price … break-even"` → auto, `"manual …"` → manual) whenever it's missing.

Also renamed the mode radio labels from bare "Manual"/"Auto" to "Manual (you use Start/Stop)" / "Auto (pauses & resumes with price)", since "Manual" reads like a neutral default and was mistaken for "always on" - it isn't; it just means nothing here touches Start/Stop for you. The Price Guard status line now tags "Last action" with its source, e.g. "stop (auto) 34m ago".

## v18

**Live serving view in the chat box.** When the provider is busy with paid traffic (so the chat is locked), the same box now shows a simulated token stream - blocks appearing at the provider's real, measured rate, with "~N tokens/s", a "reading the prompt…" state when the counter isn't moving mid-request, and a running "tokens since this busy stretch began (≈ words/pages)" line. What's real is the rhythm: a new 1-second `/api/serving_pulse` poll reads the daemon's own `tokens_generated` counter. What isn't is the text - the actual words are the customer's private conversation and are never visible to a provider, by design (checked: no log level, config option or local endpoint exposes them; paid traffic arrives over the coordinator websocket straight into the daemon's memory). The header and tooltip say so plainly. The 1s pulse also makes the busy/idle switch on the chat box near-instant instead of up to 10s late. Chat messages are kept across busy stretches.

## v17

**Tokens shown as text.** Token counts now come with a rough "how much text is that" next to them - the Requests card ("254,557 tokens ≈ 191k words · 382 pages · 2.1 novels"), the per-model Tokens column in the Account table, the average prompt/reply lengths in Nerdy Stats, and the Utilization gauge's tokens/h (≈ pages/h). Rule of thumb ≈ 0.75 words per token, 500 words per page, 90,000 words per novel; the tooltip says so and that code/non-English text runs higher. Only counts exist anywhere - the actual prompt and reply text is never stored by Darkbloom or this Mac - so this is the closest the dashboard can get to "what did my Mac actually write".

## v16

**"Expected vs. actual payout" meter is back**, as its own two-bar tile in the Darkbloom Account panel (it had been folded into the hidden estimate columns in v14). Expected = tokens × this dashboard's own calibrated flat rate; Actual = what Darkbloom's ledger paid for the same jobs; a one-line verdict says whether the estimate is within 10%, ran high, or ran low. The tooltip spells out that it measures the accuracy of our guess, not a fee (Darkbloom advertises 0%), that base reward is excluded on both sides, and that a drifting gap means the rate needs recalibrating. Per-model detail stays behind the "Show estimate columns" checkbox.

## v15

**Less text on the page, same information on hover.** A read-through of every visible sentence with fresh eyes: anything a newcomer doesn't need at a glance moved into the tooltip of the thing it explains (marked "hover for details"), and anything that didn't earn its place went.

- Page footer is one line ("Power measured by the Mac's own sensor · updates every 10 s") - the 7W-baseline, PSU-efficiency, $0.048/M-token and poll-rate detail is in its tooltip.
- Status cards: the raw coordinator string and the provider's pid moved into the card tooltips; the Provider card just shows uptime. Net card rounds the balance.
- Account panel: "Available / Withdrawable / Lifetime / N most recent analyzed (covers ~48h: …)" is now "Balance · Earned all-time · jobs" with the rest on hover. The plain-language "80% of earnings is the base reward for being online" line moved here from Nerdy Stats - it's the most important sentence on the page and was under the wrong heading.
- Nerdy Stats: header says "(safe to ignore)"; the base-reward exclusion, the "tracked locally" caveat and the "no prompt text exists" note are all tooltips now.
- Price Guard: one-line what-it-does, details on hover; "Daemon" → "Provider"; the status line no longer repeats the same two numbers twice.
- Running-hot banner, Disk Usage note, 48h-chart coverage note, price-panel footer and legends all shortened the same way. Live gauge subtitle ("SMC sensor ÷ 90% PSU", "latest sample: Wed Sep 16 …") gone - the Total gauge tooltip explains measured vs. estimated.
- Fix: rows written after the `power_method` column was added weren't visible to the server until the CSV header was migrated; the energy monitor now adds the column name to an older header on start and the server tolerates unheadered extras.

## v14

**Works outside Sweden, and measures the whole Mac.** Second half of the newcomer pass, plus the two things that made the cost numbers untrustworthy.

- **Electricity price sources, picked in the UI.** One configured source now feeds everything price-related (48h chart, "$/kWh right now", Price Guard break-even, and cost tracking - `energy-monitor.sh` asks the dashboard for the current price instead of fetching its own). Sources, all free and key-less: elprisetjustnu.se (SE1-4, SEK), hvakosterstrommen.no (NO1-5, NOK), energy-charts.info (every European bidding zone, EUR - Fraunhofer ISE's API, the same data Home Assistant's ENTSO-E/EPEX integrations use), Octopus Agile (UK regions, GBP incl. VAT), ComEd hourly pricing (US Illinois, real-time), or a flat rate in any currency. FX via frankfurter.app hourly. Grid fee / energy tax / VAT are now in the source's currency with an editable VAT % (old öre config migrates automatically). Old `elpris-zone.json` is read once and carried over.
- **Real whole-system power via macmon.** `powermetrics` only sees CPU+GPU. Measured live on this M4 Pro Mac mini against the SMC's system-power sensor: the flat 7W guess was ~2W too high at idle and roughly 2.5x too low under sustained inference (SMC ~60W vs. CPU+GPU+RAM rails ~29W, no peripherals attached) - busy hours were being costed at less than half their real draw. With `macmon` on PATH the energy monitor now runs it as a background child and uses the real whole-system reading ÷ 90% assumed PSU efficiency; the live Total gauge and page footer say which method is active, and each CSV row logs `power_method`. Falls back to the old guess without macmon.
- **A chart that matches the Net card.** New "Real Earnings vs. Electricity Cost — last 48h": every real ledger entry (including the base reward) summed against measured cost. The two older estimate-based charts - which exclude the base reward and therefore showed a loss while the real balance showed a profit - are folded into a collapsed "nerdy" block with a plain explanation of why they disagree.
- **Status cards in plain language.** "Network trust: Trusted — receiving jobs" instead of "hardware / online, coordinator reason: continuity"; "Provider: Running, waiting for jobs" instead of "Daemon: running (pid …)". Raw strings kept as sub-lines.
- **Account table:** "base_reward" row is now "base reward (being online)" with a tooltip; the "Our local estimate" / "gap" columns are hidden by default behind a checkbox (they're about this dashboard's own guess, and were easy to misread as Darkbloom shortchanging you).
- **Ollama indicator hidden when Ollama isn't installed.** Price Guard gets a what-it-does line above its controls. Abbreviations spelled out (tokens/h, $ per million tokens).

## v13

**Newcomer pass.** Reviewed the dashboard as someone who just started renting out their Mac and fixed everything that would have scared them off or left them stuck:

- **Banners are now graded, and the scary one is grey.** When real requests are succeeding, a `darkbloom doctor` FAIL (or an old model-load error) is shown as a neutral grey info note that leads with **"Working: N requests served"** in bold, instead of a yellow warning that reads as "it's broken". doctor's stock "consider a machine with more unified memory" advice is dropped in that case - it's actively misleading when the box is serving fine. Yellow is reserved for things that are actually off, red for a load error in the last two minutes. The explainer section documents the three levels.
- **Banners no longer break into three columns at narrow widths.** All banner text is one flex item now; before, every text node and inline span became its own column.
- **Running-hot alert.** New yellow banner when the GPU is at 85°C+ but the fan is under 50% - which is what Darkbloom's fan-control helper failing to engage looks like (observed live: 100°C+ at ~20% fan, `darkbloom fan status` cycling into `Mode: error — fan 0 did not enter manual mode`). Says plainly that macOS throttling still protects the hardware, that it costs performance, what to run to check, and that it's Darkbloom's helper rather than this dashboard. The server now parses the helper's mode/error/policy lines so the fan gauge shows the helper's state underneath it, and the GPU Temp gauge turns orange at 85°C / red at 95°C.
- **"Active is 13% of floor" gets a plain-language line.** Nerdy Stats now says, in real dollars from the same window, what share of earnings came from the base-reward floor vs. actually serving - and that floor-dominated is normal on this network right now, not a fault. This is the single most misread number on the page.
- **Disk Usage explains why there are five models.** One sentence: Darkbloom downloaded them itself, unused ones only cost disk, removing is safe and reversible.
- **Fixes and additions to the explainer:** the Utilization entry described a request-duty-cycle that was replaced in v6 with real GPU busy-ness; now accurate. Added GPU Headroom and Fan/Temp entries.
- **Yellow banners now say what to do**, not just what's wrong: the power-monitoring-inactive banner names the exact setup step; the trust banner notes the known coordinator flicker usually clears itself.
- **README** leads with "built for Sweden, one edit if you're elsewhere" and a short orientation for newcomers, so nobody discovers the SEK defaults after installing.

## v12

**New: idle chat window.** A chat panel at the bottom of the dashboard talks directly to whichever model is currently loaded on this Mac - only enabled when the provider is running and not currently serving real paid traffic (`daemon-state.json`'s `inference_active` flag, the same signal the "running, idle" badge already uses; fails closed if that state can't be read at all). Proxies straight to the local endpoint the warmup pinger already uses, no new auth surface. Nothing is persisted - the conversation lives in the browser tab and resets on reload.

Also fixes a real readability bug found while building this: gpt-oss-20b's local endpoint doesn't parse its own "harmony" response format, so every reply came back with raw `<|channel|>analysis<|message|>...` internal reasoning text glued in front of the actual answer. Now split apart server-side - the clean answer shows by default, with an optional "Show reasoning" toggle to see the model's internal reasoning pass if you want it. Other models (e.g. gemma) don't use this format and pass through unaffected.

And a second fix caught immediately after shipping: the chat panel rebuilds its whole `innerHTML` on every periodic refresh (~10s), which was yanking keyboard focus and any in-progress typed text out of the input field mid-sentence. Now saves and restores the input's value, focus, and cursor position across every rebuild.

## v11

**New: Price Guard** — stops serving when electricity price makes it unprofitable, resumes when it isn't. Compares today's real electricity price against this account's own measured "$/hr actively serving" rate (Nerdy Stats), converted to a break-even SEK/kWh price using the current real power draw. Two modes:
- **Manual** (default): shows the live numbers and a recommendation, plus "Start now"/"Stop now" buttons that call the real `darkbloom start`/`stop` CLI directly - nothing happens automatically.
- **Auto**: a background loop (5 min cadence) applies the recommendation itself once it's been true for long enough.

Guard rails against flapping: an asymmetric margin (default 15%) so it only stops when price is meaningfully above break-even and only resumes when meaningfully below, plus minimum running/stopped durations (default 60/30 min) enforced regardless of mode - a manual action updates the same timer, so switching to Auto right after a manual start can't immediately undo it. `darkbloom start`'s args (models, idle-timeout, port, etc.) are read back from the live launchd plist each time rather than hardcoded, so this can never drift from however the provider is actually configured. Uses the real `darkbloom start`/`stop` CLI (not raw `launchctl`) so the coordinator sees an intentional disconnect, not something that could look like a crash. A dashboard banner explains it clearly if Auto has paused serving, so it doesn't read as an outage.

## v10

**Fixed: "Utilization (last hour)" could show negative requests/tokens per hour.** Same root cause as v9's revenue bug, different code path: `get_utilization()` computed throughput as (last row - first row) over a lookback window, using the daemon's own `requests_served`/`tokens` counters directly. Those counters reset to 0 on every daemon restart, so a restart landing inside the lookback window produced a negative delta (observed live: -4500.8 req/hr, -2,025,516 tok/hr, right after restarting the daemon to verify the v9 fix). Now sums consecutive deltas across the window instead of a single first-to-last subtraction, treating any decrease as a reset (the whole new value counts as newly earned, same logic as the bash-side fix).

## v9

**Fixed: "Estimated revenue" wasn't actually cumulative.** Found via another fresh-eyes pass over the dashboard: the green revenue line on the Cumulative Electricity Cost vs. Estimated Revenue chart kept sawtoothing back to near-$0 instead of climbing like the cost line next to it. Root cause: it was computed as `darkbloom status`'s own `tokens` counter × rate, logged directly every poll - and that counter resets to 0 every time the darkbloom daemon itself restarts, unrelated to this dashboard's own uptime. Electricity cost, by contrast, was already a real persisted running total. Now token counts are accumulated into a real lifetime total the same way the cost side already was (detects a daemon-restart reset and adds the new value instead of losing the running total), so the two lines are finally comparing the same kind of number. This also means "Net (Estimated) Over Time" was understating losses/overstating them incorrectly after every daemon restart - now corrected going forward (pre-fix history in the chart is not retroactively corrected, only new data).

## v8

**Warning banners now lead with reassurance, not alarm.** The load-error and `darkbloom doctor` banners both buried their "actually fine, N requests already succeeded" context at the end of a paragraph of alarming technical text (e.g. "insufficient memory", "doesn't fit in RAM"). A first-time visitor had to read past the scary part to learn nothing was actually broken. Now that context leads each banner instead of trailing it.

## v7

**New: local earnings-history accumulation, extending real coverage from ~4h toward 36-48h.** Confirmed empirically that Darkbloom's earnings API is hard-capped at 1000 entries per call, with no working pagination (`limit=5000` still returns exactly 1000; `offset`/`before_id`/`cursor`/`page` params are all silently ignored). On a busy day that single-call window can be under 4 hours - visible as the real Net line on the Electricity Price & Profitability chart only ever showing a short stretch of real shape against an otherwise-flat 72h span. Now every 30-second account poll appends newly-seen entries (deduped by ID) to a local log, pruned to a 48-hour rolling window, which the Account panel's stats and the chart's Net line both read from instead of the API's own narrow single-call response. Starts from zero on first run - real 36-48h coverage builds up over that many hours of actual uptime, nothing can backfill history that was never locally recorded before now.

**Warmup 429s no longer log as errors.** A 429 from the local endpoint during a warmup ping specifically means the model was already busy serving real paid traffic - i.e. already warm, which is exactly why there was nothing to warm up. Now logs "already busy with real traffic, no warmup needed" instead of "ERROR: ... failed", and no longer flips the warmup status badge to a red ✗ for what was never really a failure.

## v6

**Utilization (last hour) now measures real GPU load**, not request-arrival frequency. Previously it showed the % of 5-min windows that saw any request arrive - a duty cycle that couldn't tell one tiny request from the GPU pegged at 100% the whole time. Now `energy-monitor.sh` logs the real "GPU HW active residency" figure every 5 minutes (the same one the Headroom gauge reads live), and this gauge shows the genuine average over the last hour of real logged samples.

**Spam detection in Nerdy Stats.** A network-wide spam flood against Gemma, confirmed and rate-limited by Darkbloom's own team on 2026-09-10, was found to be silently skewing the average prompt/completion length stat by ~35% (uniform 25-token spam requests diluting the real average). Now flagged directly: shows both the raw average and a spam-excluded one, plus a note that the "active $/hr" pay-rate figure is affected by the same dilution.

**Revenue-estimate rate recalibrated** from $0.044/M to $0.048/M tokens, this time explicitly excluding spam-signature jobs from the calculation. Documented as needing periodic recalibration rather than being trusted indefinitely - the previous $0.125/M guess had quietly drifted 2.8x stale before anyone checked it.

**Doctor's persistent "model doesn't fit in RAM" false positive gets an explanation, not just a workaround.** Found the likely mechanism: the check appears to compare a model's requirement against *currently-free* RAM rather than the total budget, so whichever model is already loaded (and using its own share) looks like it can't fit itself - consistent with this FAIL always landing on the currently-active model and rotating whenever that changes. Surfaced as a working theory in the banner, not asserted as fact.

**Account panel now shows its real data window.** The earnings API only ever returns the most recent 1000 entries, not a fixed time range - on a busy day that's under 4 hours of history, not the stable "recent activity" the sample count alone implied. Now shown explicitly (e.g. "covers ~3.8h").

**Warmup model-targeting now has a UI control** instead of requiring a manual API call - checkboxes per configured model, with "all checked" correctly falling back to "every configured model" rather than a stale hardcoded list.

**Clarified two different "Net" figures that share a label.** The original chart (renamed "Net (Estimated) Over Time") is a full-history estimate from a flat $/token rate; the newer Electricity Price & Profitability panel's Net is real earnings minus real cost per 15-minute slot, only over its own recent window. Cross-referenced in both places so they're not mistaken for the same number.

## v5

**New: GPU Headroom gauge**, replacing an earlier attempt that showed free system RAM. Now sourced directly from `powermetrics`' own "GPU HW active residency" line (already being logged every ~200ms, just never parsed before) — a real 0-100% GPU busy-ness measurement, not derived/estimated from power draw. Reads ~75-85% at idle (background OS activity keeps GPU residency nonzero even at rest) and drops toward 0% under real inference load, directly answering "how far from full hardware operation" the Mac is.

**Selective warmup targeting**: the warmup loop can now be told to keep only specific model(s) warm instead of every configured model. Needed because some model combinations can't actually stay resident together on this hardware (loading a second model evicts the first even when the combined catalog size looks like it should fit the RAM budget - real overhead runs higher than the static per-model estimate) - warming all of them on a fixed interval was otherwise just thrashing between evictions, paying a real cold-load cost each swap for no benefit.

**Load-error banner now shows real age** instead of looking like an active emergency indefinitely. `darkbloom status`'s own "Last model-load error" text has no expiry - a failure from hours ago read exactly like one happening right now. Now sourced from `daemon-state.json`'s timestamped version instead, shows "(Xm ago)", and only stays red if genuinely recent (under 2 minutes); older, it downgrades to a calm warning, matching the same reconciliation-over-raw-alarm treatment already given to the `darkbloom doctor` FAIL banner.

**Warmup log now shows the newest entry first** instead of requiring a scroll to find it.

**README/install.sh cleanup**: removed a stale "timed model rotation" mention (dropped from the tool ages ago, still lingering in the GitHub repo description and docs) and rewrote the entire account-sync section, which still described the old browser bookmarklet workaround replaced back in v2 - now documents the real live API integration. Fixed a few other stale references along the way (old "DB cut" label, an uninstall file list missing newer config files).

## v4

**Renamed to "Electricity Price & Profitability"** and reworked to always show real data looking both backward and forward in time, instead of a forecast-only chart that shrank to ~24h whenever tomorrow's prices weren't published yet. Now shows yesterday (always real) + today (always real) + tomorrow (real once published, otherwise left as a clean gap rather than guessed) - elprisetjustnu.se keeps every past day's file permanently, so the backward-looking side never has to be empty.

**The right-axis overlay now shows Net (real earnings minus this Mac's own real electricity cost) per matching 15-minute slot**, not just raw earnings - answers "was this actually profitable" directly in the same graph, including negative stretches, with its own zero-line reference when it crosses zero independently of the price axis. Electricity cost is read from the same energy-log.csv the Power/Cost charts already use (this Mac's real zone), kept separate from whatever zone is selected for the price display above.

`renderLineChart` gained proper null-value handling (breaks the drawn line into segments at gaps instead of plotting bogus points, excludes nulls from axis domain/min/max, keeps hover/crosshair working across a gap) and an optional right-hand axis that can independently allow negative values - both opt-in via `opts`, so the existing power/cost/net charts are unaffected.

## v3

**Multi-model warmup fix**: the warmup loop only ever kept the *first* configured model warm. Now pings every model the provider is configured to serve (read straight from the launchd plist's `--model` flags, not from `darkbloom status`'s "Warm models" line, which only lists what's already loaded). The disk-usage panel's active/unused split was fixed the same way.

**Recalibrated the "estimated revenue" rate** from a never-measured $0.125/M-token guess (based on Darkbloom's quoted alpha pricing) to $0.044/M, computed straight from this account's own real ledger. The old guess turned out to be ~2.8x too high even after correcting for the real prompt:completion token mix — collapsed the "Darkbloom gap" comparison from ~65% down to ~0%, confirming that number was entirely measuring our own guess's error, never anything about Darkbloom's actual pricing.

**New: Active vs. floor pay rate.** A genuinely local, non-advertised $/hour comparison — real earnings during actual active-serving time (tracked via our own `inference_active`-flag watcher) vs. base-reward floor earnings during idle time (floor-slot width derived from the `floor:<minute>Z:...` timestamp Darkbloom itself embeds in every base-reward `job_id`, not assumed). Shown in Nerdy Stats with a full "how is this computed?" hover breakdown.

**New: Electricity Price Forecast (48h) panel.** Today's (and tomorrow's, once published) day-ahead spot prices at Sweden's real 15-minute settlement granularity, with a zone picker (SE1-SE4) that also drives the header's "$/kWh right now" figure. Auto-extends to 48h the moment the next day's prices are published (usually early afternoon) — no restart needed. Includes:
- A "now" marker on the chart.
- A second line for price *including* grid fee + energy tax + 25% VAT, both fields user-editable (öre/kWh) and defaulting to 0 rather than a guessed "typical" Swedish rate, since actual grid fees vary enormously by operator/subscription.
- A dual right-axis overlay showing real earnings per matching 15-minute slot, for a rough visual read on price vs. earnings correlation.
- A prominent code comment (and matching UI note) on exactly what to swap for adapting this dashboard to a non-Swedish electricity market.

**Extended `renderLineChart`** (the hand-rolled SVG chart function) to support an optional secondary right-hand y-axis and a "now" time marker, both opt-in via `opts` so the existing power/cost/net charts are unaffected.

## v2

**Renamed** from `darkbloom-monitor` to `darkbloom-live-stats` — a different, unrelated tool (justin-schroeder/darkbloom-monitor) shares the old name, and the new one better reflects what this tool actually does versus other community dashboards: tracks real electricity cost against real earnings, not just live status.

**Live account data, no more bookmarklet.** The "Darkbloom Account" panel now polls Darkbloom's real earnings API directly every 30s, server-side, using the same device token `darkbloom login` already stores locally (`~/.darkbloom/auth_token`). The whole drag-to-bookmarks-bar workaround — built earlier to route around a mixed-content restriction — turned out to be unnecessary; the server could reach Darkbloom's API the entire time. Available/withdrawable/lifetime balance are now shown separately.

**USD throughout**, replacing SEK. Electricity price is still sourced from Sweden's SE3 spot market (that's genuinely where the power is priced) but converted per-datapoint using that point's own exchange rate. Current $/kWh is now shown prominently in the header instead of buried in a footnote.

**Two new live gauges**: Fan Speed and GPU Temp, read via `darkbloom fan status` (read-only — doesn't enable Darkbloom's fan-control helper). Apple Silicon exposes neither through `powermetrics` at all.

**A `Utilization (last hour)` gauge**, later fixed from a power-draw-threshold heuristic (which misread idle background load as "100% active") to a duty-cycle computed from the real `requests_served` counter — accurate and directly verifiable against the same numbers shown elsewhere on the page.

**Daemon card** now distinguishes "running, idle" from "running, actively serving" (pulsing dot), read from the daemon's own live state file (`daemon-state.json`'s `inference_active` flag).

**New "Nerdy Stats" panel**: average prompt/completion token length (correctly excluding synthetic `base_reward` floor-payment rows), GPU memory active/cached vs. total unified memory, per-slot KV-backend/MTP state, `darkbloom doctor`'s health-check findings, and locally-tracked real request duration — Darkbloom exposes no timing data anywhere, so a background thread watches `inference_active` transitions twice a second and logs real observed durations, clearly labeled as our own tracking rather than an official number.

**`darkbloom doctor` FAIL findings** now surface as a banner — shown as a caution with the situation spelled out plainly (e.g. "N requests have already succeeded, so treat this as worth a look, not a confirmed outage"), since the RAM-fit check this hardware trips has already been observed as a false positive.

**Clearer charts**: labeled Y/X axes (there were none before), a legend and dashed peak-per-bucket overlay on the power chart, bucket-averaged downsampling that preserves spikes instead of a stride-pick that could silently drop them, and a new "Net (Revenue − Cost) Over Time" chart with correct negative-value/zero-line handling.

**Other fixes along the way**: the dashboard server switched to `ThreadingMixIn` after 5Hz power polling could stall behind slower requests; power sampling itself bumped from 60s to ~5Hz; a disk-usage panel with one-click "copy remove command" for unused downloaded models; trust-drop alerts (native macOS notification + banner); raw powermetrics log self-rotation; model rotation removed entirely (had already been stopped in practice); a comprehensive in-dashboard "what does everything mean" explainer; and the account "DB cut" comparison renamed to "Darkbloom gap" with softer framing, since it measures the accuracy of this tool's own estimate, not a claim about Darkbloom's pricing.

Confirmed during this work that two originally-hoped-for features are simply not possible with anything Darkbloom exposes: which IP/country a request comes from, and the actual prompt/response text. Neither exists anywhere accessible — checked exhaustively against the earnings API, unified logging, and every local state file.

## v1

Initial release — local web dashboard for a Darkbloom provider Mac: live CPU/GPU/RAM gauges, electricity-cost vs. revenue tracking (Swedish SE3 spot price), automatic model warmup, timed model rotation, and a real-vs-estimated payout comparison via a browser bookmarklet. Runs as `launchd` LaunchAgents with a narrowly-scoped `sudoers` rule for `powermetrics`. MIT licensed.
