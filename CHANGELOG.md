# Changelog

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
