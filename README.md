# Darkbloom Monitor

A local dashboard + background services for a [Darkbloom](https://darkbloom.dev)
provider Mac: live CPU/GPU/RAM gauges, electricity-cost vs. revenue tracking,
automatic model warmup, timed model rotation (so you can compare demand
between models), and a real-vs-estimated payout comparison pulled from your
actual Darkbloom account.

**Not affiliated with or endorsed by Darkbloom, Eigen Labs, or EigenLayer.**
Community tool, use at your own risk. It only reads local system state and
your own Darkbloom account data - it does not modify your provider's behavior
beyond the model-rotation feature you opt into.

![status](https://img.shields.io/badge/status-community%20project-blue)

## What it looks like

A single-page dashboard at `http://127.0.0.1:8787`, only reachable from the
machine it runs on:

- **Status cards** - trust level, daemon state, requests served, warm models,
  accumulated electricity cost, net (real balance or estimate)
- **Model rotation** - alternates between two models you configure, but only
  counts time toward the next switch once hardware-trust is actually achieved
  (so idle self-signed time doesn't inflate a period's "uptime")
- **Darkbloom account** - real jobs/tokens/payout per model from your account
  ledger, compared against what a naive flat per-token estimate would guess
  (labeled "DB cut" - **not** an official platform fee, just the gap between
  guess and reality)
- **Warmup** - periodically pings the provider's local endpoint so the active
  model stays loaded instead of unloading between requests
- **Live power gauges** - CPU / GPU / Total watts (with an estimated non-SoC
  baseline added in) / RAM used, all updating every 2 seconds
- **Ollama indicator** - flags when Ollama has a model loaded, since that's a
  common cause of memory contention with the Darkbloom provider on the same
  machine
- **History charts** - power over time, cumulative electricity cost vs.
  estimated revenue

## Prerequisites

- macOS, Apple Silicon, with [Darkbloom](https://darkbloom.dev) already
  installed and enrolled (`~/.darkbloom/bin/darkbloom` should exist and
  `darkbloom status` should work)
- `python3`, `bc`, `jq`, `curl` - all present on a stock macOS install except
  `jq`, which you may need to install (`brew install jq`)
- Two or more models downloaded (`darkbloom models download <id>`) if you want
  to use the model-rotation feature

## Install

```bash
git clone https://github.com/jordglob/darkbloom-monitor.git
cd darkbloom-monitor
./install.sh
```

This copies the dashboard and scripts into `~/.darkbloom/`, writes LaunchAgent
plists to `~/Library/LaunchAgents/`, and starts three of the four background
services (dashboard, energy-monitor, model-rotate). It will **not** touch
`/etc/sudoers` or ask for your password - see below.

Open **http://127.0.0.1:8787** - the dashboard should already be showing
status, warmup, and model-rotation info at this point. Power gauges and
electricity-cost tracking will show "no data yet" until you do the next step.

### Enabling electricity-cost tracking (one-time, needs your password)

CPU/GPU power sampling (`powermetrics`) requires root. Rather than storing
your password or asking for it repeatedly, the installer prepares a **scoped**
sudoers rule that only ever allows running `powermetrics` itself - nothing
else - without a password:

```bash
bash ~/.darkbloom/setup-powermetrics-sudoers.sh
```

It validates the rule's syntax with `visudo -c` before touching the real
sudoers config, so a typo can't lock you out of sudo. Then start the fourth
service:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/io.darkbloom.powermetrics.plist
```

From here on, everything (all four services) survives reboots and
auto-restarts if killed - `launchd`'s `KeepAlive` is the watchdog, nothing runs
in a terminal or `tmux` session that needs to stay open.

## Configuring which models it rotates between

Edit `~/.darkbloom/model-rotate.sh` and change this line to models you've
actually downloaded:

```bash
MODELS=(gpt-oss-20b qwen3-vl-30b-a3b-instruct)
```

Then restart the service:

```bash
launchctl kickstart -k gui/$(id -u)/io.darkbloom.model-rotate
```

Only one model is ever loaded at a time - this is meant for comparing demand
between two models over multi-hour periods, not for serving multiple models
concurrently (if your machine has enough RAM for that, you don't need this
tool's rotation feature - just pass multiple `--model` flags to
`darkbloom start` yourself).

## Electricity price

Defaults to Sweden's free spot-price API (`elprisetjustnu.se`, zone SE3). If
you're elsewhere, open `~/.darkbloom/energy-monitor.sh` and set
`FIXED_PRICE_PER_KWH` to a flat rate in your own currency - the rest of the
math doesn't care what currency it is, it just needs a number per kWh.

## Syncing real account data (the bookmarklet)

The "Darkbloom Account" panel shows real payout data, but it isn't a live
connection - an HTTPS page (`console.darkbloom.dev`) fetching data and posting
it to a plain HTTP local server is blocked by the browser as mixed content, no
matter how you slice it (tried `fetch()`, tried `<img>` - Chrome blocks both).
The workaround that actually works: a **bookmarklet** that opens a very brief
tab which posts the data and closes itself immediately (~0.5s) - top-level
navigation isn't subject to the same restriction.

On the dashboard page, drag the **"↻ Sync Darkbloom Account"** button in the
Darkbloom Account panel to your bookmarks bar. Whenever you want fresh
numbers, open `console.darkbloom.dev` (logged in) and click it.

## Security notes

- The dashboard binds to `127.0.0.1` only - not reachable from other devices
  on your network.
- The sudoers rule installed by `setup-powermetrics-sudoers.sh` is scoped to
  `/usr/bin/powermetrics` **only**. It cannot be used to run any other command
  as root.
- No credentials are ever stored by this tool. The account-sync bookmarklet
  runs in your own already-authenticated browser tab and never touches your
  session cookie - it reads data via Darkbloom's own API (same-origin fetch)
  and hands it to the local server as a one-shot query string.
- Revenue/cost numbers are estimates in several places (clearly labeled in the
  UI) - not audited, not financial advice.

## How the pieces fit together

| Component | Runs as | Needs sudo? |
|---|---|---|
| `dashboard/server.py` | `io.darkbloom.dashboard` LaunchAgent | No |
| `scripts/pm-start.sh` (powermetrics) | `io.darkbloom.powermetrics` LaunchAgent | Yes (scoped rule) |
| `scripts/energy-monitor.sh` | `io.darkbloom.energy-monitor` LaunchAgent | No |
| `scripts/model-rotate.sh` | `io.darkbloom.model-rotate` LaunchAgent | No |

All four are `KeepAlive` LaunchAgents - if one crashes, `launchd` restarts it.
None depend on a Terminal window or `tmux` session staying open.

## Uninstall

```bash
for svc in dashboard powermetrics energy-monitor model-rotate; do
  launchctl bootout gui/$(id -u)/io.darkbloom.$svc 2>/dev/null
  rm -f ~/Library/LaunchAgents/io.darkbloom.$svc.plist
done
sudo rm -f /etc/sudoers.d/darkbloom-powermetrics
rm -rf ~/.darkbloom/dashboard ~/.darkbloom/*.sh ~/.darkbloom/energy-log.csv \
       ~/.darkbloom/model-rotate.log ~/.darkbloom/model-rotate.state \
       ~/.darkbloom/warmup.json ~/.darkbloom/warmup.log ~/.darkbloom/account-data.json
```

(This leaves your actual Darkbloom install - `~/.darkbloom/bin`,
`~/.cache/huggingface` etc. - untouched.)

## License

MIT - see [LICENSE](LICENSE).
