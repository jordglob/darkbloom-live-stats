# Changelog

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
