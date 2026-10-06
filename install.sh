#!/bin/bash
# Installs the Darkbloom Live & Stats dashboard + background services for the
# CURRENT user. Safe to re-run - it just overwrites its own files.
#
# What this does automatically (no sudo needed):
#   - Copies dashboard/ and scripts/ into ~/.darkbloom/
#   - Installs and starts 2 LaunchAgents: dashboard, energy-monitor
#
# What it prepares but does NOT run for you (needs your password, on purpose -
# see README "Security notes"):
#   - A scoped sudoers rule + LaunchAgent for `powermetrics` (CPU/GPU power
#     sampling needs root). You run one short script yourself, once.
#   - A second, equally scoped sudoers rule for the optional fan-recovery
#     helper (see README "Optional: automatic fan recovery") - only written
#     if you've already built and installed that binary yourself; otherwise
#     skipped entirely, and the dashboard's fan-recovery loop just no-ops.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="$HOME/.darkbloom"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
CURRENT_USER="$(id -un)"

echo "== Darkbloom Live & Stats installer =="
echo "Target directory: $TARGET"
echo ""

if [ ! -x "$TARGET/bin/darkbloom" ]; then
  echo "WARNING: $TARGET/bin/darkbloom not found."
  echo "This tool monitors an existing Darkbloom provider install - it does not"
  echo "install Darkbloom itself. See https://darkbloom.dev to set that up first."
  echo ""
fi

for cmd in jq bc python3; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "WARNING: '$cmd' not found on PATH - some features will not work until it's installed."
  fi
done

mkdir -p "$TARGET/dashboard"
cp "$REPO_DIR/dashboard/server.py" "$TARGET/dashboard/server.py"
cp "$REPO_DIR/dashboard/index.html" "$TARGET/dashboard/index.html"
cp "$REPO_DIR/dashboard/app.js" "$TARGET/dashboard/app.js"
cp "$REPO_DIR/dashboard/app.css" "$TARGET/dashboard/app.css"
# The version shown on the page comes from the top CHANGELOG heading - the
# single place it is written - so it cannot lag behind the code.
VERSION="$(grep -m1 -oE '^## v[0-9]+' "$REPO_DIR/CHANGELOG.md" | sed 's/^## //')"
echo "${VERSION:-unknown}" > "$TARGET/dashboard/VERSION"

for f in pm-start.sh pm-run.sh energy-monitor.sh max-power.py experiment.py provider_log_stream.py; do
  cp "$REPO_DIR/scripts/$f" "$TARGET/$f"
  chmod +x "$TARGET/$f"
done

echo "Files copied to $TARGET"

mkdir -p "$LAUNCH_AGENTS"
for tmpl in "$REPO_DIR"/launchd/*.plist.template; do
  name="$(basename "$tmpl" .template)"
  sed "s|__HOME__|$HOME|g" "$tmpl" > "$LAUNCH_AGENTS/$name"
done
echo "LaunchAgent plists written to $LAUNCH_AGENTS"

# Scoped sudoers helper - written to disk, but NOT executed here. See README.
sed "s|__USER__|$CURRENT_USER|g" "$REPO_DIR/launchd/darkbloom-powermetrics.sudoers.template" \
  > /tmp/darkbloom-sudoers-rule

cat > "$TARGET/setup-powermetrics-sudoers.sh" <<SETUP
#!/bin/bash
set -euo pipefail
RULE_FILE=/tmp/darkbloom-pm-sudoers-tmp
DEST=/etc/sudoers.d/darkbloom-powermetrics

cat > "\$RULE_FILE" <<'EOF'
$CURRENT_USER ALL=(root) NOPASSWD: /usr/bin/powermetrics *
EOF

sudo visudo -c -f "\$RULE_FILE"
sudo cp "\$RULE_FILE" "\$DEST"
sudo chmod 440 "\$DEST"
sudo chown root:wheel "\$DEST"
rm -f "\$RULE_FILE"
echo "SUDOERS_INSTALLED_OK"
SETUP
chmod +x "$TARGET/setup-powermetrics-sudoers.sh"

# Same pattern for freeing macOS's file cache (/usr/sbin/purge) before a
# model load - optional; without it the dashboard just shows the command.
cat > "$TARGET/setup-purge-sudoers.sh" <<SETUP
#!/bin/bash
set -euo pipefail
RULE_FILE=/tmp/darkbloom-purge-sudoers-tmp
DEST=/etc/sudoers.d/darkbloom-purge

cat > "\$RULE_FILE" <<'EOF'
$CURRENT_USER ALL=(root) NOPASSWD: /usr/sbin/purge
EOF

sudo visudo -c -f "\$RULE_FILE"
sudo cp "\$RULE_FILE" "\$DEST"
sudo chmod 440 "\$DEST"
sudo chown root:wheel "\$DEST"
rm -f "\$RULE_FILE"
echo "SUDOERS_INSTALLED_OK"
SETUP
chmod +x "$TARGET/setup-purge-sudoers.sh"

# Same pattern, for the optional fan-recovery helper - only relevant if you've
# already built it (see README), so only offered when the binary exists.
if [ -x "$TARGET/bin/darkbloom-fan-helper" ]; then
  cat > "$TARGET/setup-fan-helper-sudoers.sh" <<SETUP
#!/bin/bash
set -euo pipefail
RULE_FILE=/tmp/darkbloom-fan-sudoers-tmp
DEST=/etc/sudoers.d/darkbloom-fan-helper

cat > "\$RULE_FILE" <<'EOF'
$CURRENT_USER ALL=(root) NOPASSWD: $TARGET/bin/darkbloom-fan-helper *
EOF

sudo visudo -c -f "\$RULE_FILE"
sudo cp "\$RULE_FILE" "\$DEST"
sudo chmod 440 "\$DEST"
sudo chown root:wheel "\$DEST"
rm -f "\$RULE_FILE"
echo "SUDOERS_INSTALLED_OK"
SETUP
  chmod +x "$TARGET/setup-fan-helper-sudoers.sh"
  echo "Found $TARGET/bin/darkbloom-fan-helper - run setup-fan-helper-sudoers.sh to enable automatic fan recovery."
fi

# Start the 4 services that don't need root right away.
for svc in dashboard energy-monitor experiment log-stream; do
  launchctl bootout "gui/$(id -u)/io.darkbloom.$svc" >/dev/null 2>&1 || true
  # bootout returns before the job is gone; bootstrapping too early fails with
  # "Bootstrap failed: 5: Input/output error" and set -e ends the install.
  for _ in $(seq 1 20); do
    launchctl print "gui/$(id -u)/io.darkbloom.$svc" >/dev/null 2>&1 || break
    sleep 0.5
  done
  launchctl bootstrap "gui/$(id -u)" "$LAUNCH_AGENTS/io.darkbloom.$svc.plist"
done

echo ""
echo "== Done =="
echo "Dashboard running at: http://127.0.0.1:8787"
echo ""
echo "One more step for electricity-cost tracking (needs your password once, ever):"
echo "  1. Run:  bash $TARGET/setup-powermetrics-sudoers.sh"
echo "  2. Then: launchctl bootstrap gui/\$(id -u) $LAUNCH_AGENTS/io.darkbloom.powermetrics.plist"
echo ""
echo "Optional, lets model loads free macOS's file cache (needs your password once):"
echo "  bash $TARGET/setup-purge-sudoers.sh"
echo ""
echo "See README.md for how each piece works. Account data syncs live automatically if darkbloom login has already run on this Mac."
