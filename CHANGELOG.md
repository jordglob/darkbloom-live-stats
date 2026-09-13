# Changelog

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
