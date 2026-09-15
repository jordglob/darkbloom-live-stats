# Darkbloom Live & Stats

A local dashboard + background services for a [Darkbloom](https://darkbloom.dev)
provider Mac: live CPU/GPU/RAM/fan/temp gauges, disk usage for downloaded
models, electricity-cost vs. revenue tracking (including a real-time
electricity price forecast with a profitability overlay), automatic
multi-model warmup, trust-drop alerts, and a real-vs-estimated payout
comparison pulled directly from Darkbloom's own account API - no browser
step, no bookmarklet, just the same local device token `darkbloom login`
already stores on this Mac.

**Not affiliated with or endorsed by Darkbloom, Eigen Labs, or EigenLayer.**
Community tool, use at your own risk. It only reads local system state and
your own Darkbloom account data - it does not modify your provider's behavior.

**Built in Sweden, for a Swedish electricity market.** Everything works
anywhere, but the cost tracking defaults to Swedish spot prices in SEK and the
price-forecast chart is Sweden-only. If you're elsewhere, one edit in
`energy-monitor.sh` gives you a flat price in your own currency - see
[Electricity price](#electricity-price) below. Do that before reading too much
into the cost numbers.

**New to running a provider?** The dashboard's top banners are graded: grey
means "a check flagged something but requests are succeeding, no rush",
yellow means "something is actually off, here's what to check", red means
"a model failed to load in the last two minutes". Open the
"❓ What does everything on this dashboard mean?" section on the page itself
for a walkthrough of every panel - including the one economic fact that
surprises most newcomers (most of your income comes from being online and
trusted, not from serving).

![status](https://img.shields.io/badge/status-community%20project-blue)

## What it looks like

A single-page dashboard at `http://127.0.0.1:8787`, only reachable from the
machine it runs on:

- **Status cards** - trust level, daemon state (idle vs. actively serving),
  requests served, warm models, accumulated electricity cost, net (real
  balance or estimate)
- **Darkbloom account** - real jobs/tokens/payout per model, polled directly
  from Darkbloom's own earnings API every 30s using the local device token
  `darkbloom login` already stores (`~/.darkbloom/auth_token`) - no browser
  step. Compared against a naive flat per-token estimate, recalibrated from
  your own real ledger data, labeled "Darkbloom gap" (**not** an official
  platform fee - just the gap between that local guess and reality)
- **Nerdy Stats** - average prompt/completion token length, GPU memory
  active/cached, per-slot KV-backend state, `darkbloom doctor` health checks,
  and a genuinely local "active vs. floor pay rate" comparison ($/hour while
  actively serving vs. $/hour on the base-reward floor, both derived from
  real local tracking, no advertised pricing involved)
- **Multi-model warmup** - keeps *every* model your provider is configured to
  serve warm (not just the first one), pinging each on an interval you set
- **Disk usage** - downloaded models and their sizes, with a one-click "copy
  remove command" for anything not currently active (never deletes for you)
- **Live gauges** - CPU / GPU / Total watts (with an estimated non-SoC
  baseline added in) / RAM used / Fan speed / GPU temp / recent utilization,
  updating at ~5Hz (matching powermetrics' own sampling rate)
- **Trust-drop alerts** - a native macOS notification plus an in-page banner
  the moment trust drops below hardware-level, even if the tab isn't open
- **Running-hot alert** - flags when the GPU is hot but the fan is barely
  spinning, i.e. Darkbloom's own fan-control helper isn't engaging (seen in
  the wild: 100°C+ at 20% fan with the helper reporting "did not enter
  manual mode"). Explains it's a performance cost, not a safety issue, and
  what to check
- **Ollama indicator** - flags when Ollama has a model loaded, since that's a
  common cause of memory contention with the Darkbloom provider on the same
  machine
- **History charts** - power over time, cumulative electricity cost vs.
  estimated revenue, with an expandable explainer for exactly how the kWh
  price and net figure are calculated
- **Electricity Price & Profitability** - Sweden's real day-ahead spot prices
  (Nord Pool, zone picker for SE1-SE4) shown both backward (yesterday, always
  real) and forward (today + tomorrow once published, never guessed), with an
  optional grid-fee/energy-tax/VAT line and a real Net (earnings minus this
  Mac's own electricity cost) overlay per 15-minute slot - see at a glance
  whether a given stretch was actually profitable, all currency-converted to
  USD throughout

## Prerequisites

- macOS, Apple Silicon, with [Darkbloom](https://darkbloom.dev) already
  installed and enrolled (`~/.darkbloom/bin/darkbloom` should exist and
  `darkbloom status` should work)
- `python3`, `bc`, `jq`, `curl` - all present on a stock macOS install except
  `jq`, which you may need to install (`brew install jq`)

## Install

```bash
git clone https://github.com/jordglob/darkbloom-live-stats.git
cd darkbloom-live-stats
./install.sh
```

This copies the dashboard and scripts into `~/.darkbloom/`, writes LaunchAgent
plists to `~/Library/LaunchAgents/`, and starts two of the three background
services (dashboard, energy-monitor). It will **not** touch `/etc/sudoers` or
ask for your password - see below.

Open **http://127.0.0.1:8787** - the dashboard should already be showing
status and warmup info at this point. Power gauges and electricity-cost
tracking will show "no data yet" until you do the next step.

### Enabling electricity-cost tracking (one-time, needs your password)

CPU/GPU power sampling (`powermetrics`) requires root. Rather than storing
your password or asking for it repeatedly, the installer prepares a **scoped**
sudoers rule that only ever allows running `powermetrics` itself - nothing
else - without a password:

```bash
bash ~/.darkbloom/setup-powermetrics-sudoers.sh
```

It validates the rule's syntax with `visudo -c` before touching the real
sudoers config, so a typo can't lock you out of sudo. Then start the third
service:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/io.darkbloom.powermetrics.plist
```

From here on, everything (all three services) survives reboots and
auto-restarts if killed - `launchd`'s `KeepAlive` is the watchdog, nothing runs
in a terminal or `tmux` session that needs to stay open. `powermetrics` samples
at ~5Hz by default (edit the `-i` value, in milliseconds, in
`io.darkbloom.powermetrics.plist` to change it) - the raw log self-rotates
past 300MB by restarting the LaunchAgent, since `powermetrics -o` truncates
its target file on start.

## Electricity price

The real cost-tracking (Accumulated electricity cost card, its chart)
defaults to Sweden's free spot-price API (`elprisetjustnu.se`, zone SE3) -
this Mac's actual location, hardcoded on purpose since it's real incurred
cost. If you're elsewhere, open `~/.darkbloom/energy-monitor.sh` and set
`FIXED_PRICE_PER_KWH` to a flat rate in your own currency - the rest of the
math doesn't care what currency it is, it just needs a number per kWh.

Separately, the **Electricity Price & Profitability** panel is a Sweden-only
day-ahead forecast/history view with its own zone picker (SE1-SE4) in the UI
- switching it only changes that chart and the header's "$/kWh right now"
figure, never the real cost accounting above. If you're adapting this
dashboard for a non-Swedish market, `server.py`'s `_fetch_elpris_day()` has a
docstring spelling out exactly what to replace and a few starting-point APIs
(ENTSO-E, aWATTar, Elexon/N2EX).

## Live account data (Darkbloom's own API)

The "Darkbloom Account" panel is a real live connection - no browser step, no
bookmarklet, nothing to click. The dashboard server polls Darkbloom's real
earnings API (`https://api.darkbloom.dev/v1/provider/account-earnings`)
directly every 30 seconds, authenticating with the same local device token
`darkbloom login` already wrote to `~/.darkbloom/auth_token` on this machine.
If that file exists (it does on any Mac that's already run `darkbloom login`),
this just works out of the box - nothing to configure.

## Security notes

- The dashboard binds to `127.0.0.1` only - not reachable from other devices
  on your network.
- The sudoers rule installed by `setup-powermetrics-sudoers.sh` is scoped to
  `/usr/bin/powermetrics` **only**. It cannot be used to run any other command
  as root.
- No credentials are ever created or duplicated by this tool. It only *reads*
  the device token `darkbloom login` already wrote to
  `~/.darkbloom/auth_token` (server-side, never sent to the browser or logged)
  to authenticate against Darkbloom's earnings API - the same token the
  `darkbloom` CLI itself already uses.
- Revenue/cost numbers are estimates in several places (clearly labeled in the
  UI) - not audited, not financial advice.

## How the pieces fit together

| Component | Runs as | Needs sudo? |
|---|---|---|
| `dashboard/server.py` | `io.darkbloom.dashboard` LaunchAgent | No |
| `scripts/pm-start.sh` (powermetrics) | `io.darkbloom.powermetrics` LaunchAgent | Yes (scoped rule) |
| `scripts/energy-monitor.sh` | `io.darkbloom.energy-monitor` LaunchAgent | No |

All three are `KeepAlive` LaunchAgents - if one crashes, `launchd` restarts it.
None depend on a Terminal window or `tmux` session staying open.

## Uninstall

```bash
for svc in dashboard powermetrics energy-monitor; do
  launchctl bootout gui/$(id -u)/io.darkbloom.$svc 2>/dev/null
  rm -f ~/Library/LaunchAgents/io.darkbloom.$svc.plist
done
sudo rm -f /etc/sudoers.d/darkbloom-powermetrics
rm -rf ~/.darkbloom/dashboard ~/.darkbloom/*.sh ~/.darkbloom/energy-log.csv \
       ~/.darkbloom/warmup.json ~/.darkbloom/warmup.log ~/.darkbloom/trust-changes.log \
       ~/.darkbloom/inference-durations.csv ~/.darkbloom/elpris-zone.json \
       ~/.darkbloom/elpris-surcharge.json
```

(This leaves your actual Darkbloom install - `~/.darkbloom/bin`,
`~/.cache/huggingface` etc. - untouched.)

## License

MIT - see [LICENSE](LICENSE).
