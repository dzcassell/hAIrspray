#!/bin/bash
#
# Install the hAIrspray launchd agent so the container starts at login,
# independent of Docker Desktop's "start at login" setting.
#
# Safe to re-run: unloads any existing agent first.
#
set -euo pipefail

say() { printf '%s\n' "$*"; }

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MACOS_DIR="$REPO_DIR/macos"
TEMPLATE="$MACOS_DIR/hairspray.plist.template"
WRAPPER="$MACOS_DIR/hairspray-start.sh"

LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
PLIST_DEST="$LAUNCH_AGENTS_DIR/ai.hairspray.autostart.plist"
LABEL="ai.hairspray.autostart"

# ---------------------------------------------------------------------------
# Sanity checks
# ---------------------------------------------------------------------------

if [ "$(uname -s)" != "Darwin" ]; then
  say "error: this installer is for macOS (Darwin) only."
  say "       on Linux, use systemd/hairspray.service instead."
  exit 1
fi

if [ ! -f "$TEMPLATE" ]; then
  say "error: $TEMPLATE not found."
  say "       run this from inside a hAIrspray checkout."
  exit 1
fi

if [ ! -f "$WRAPPER" ]; then
  say "error: $WRAPPER not found."
  exit 1
fi

# ---------------------------------------------------------------------------
# Make the wrapper executable (idempotent)
# ---------------------------------------------------------------------------

chmod +x "$WRAPPER"

# ---------------------------------------------------------------------------
# Render the plist with this user's paths
# ---------------------------------------------------------------------------

mkdir -p "$LAUNCH_AGENTS_DIR"

# Using | as the sed delimiter because $HOME and $REPO_DIR contain /.
sed \
  -e "s|__REPO_DIR__|$REPO_DIR|g" \
  -e "s|__HOME__|$HOME|g" \
  "$TEMPLATE" > "$PLIST_DEST"

say "rendered $PLIST_DEST"

# ---------------------------------------------------------------------------
# Load it (unload first if already present, so re-running this script
# picks up any template changes cleanly).
# ---------------------------------------------------------------------------

if launchctl list 2>/dev/null | awk '{print $3}' | grep -qx "$LABEL"; then
  say "agent already loaded; unloading first..."
  launchctl unload "$PLIST_DEST" 2>/dev/null || true
fi

launchctl load "$PLIST_DEST"

say ""
say "✓ hAIrspray launchd agent installed and loaded."
say ""
say "It will start automatically on every login. The first run will"
say "happen now — Docker Desktop needs to be running (or will start"
say "automatically if you have that setting enabled)."
say ""
say "Logs: ~/Library/Logs/hAIrspray-launchd.log"
say ""
say "To check status:"
say "  launchctl list | grep $LABEL"
say ""
say "To stop autostarting:"
say "  launchctl unload $PLIST_DEST"
say "  rm $PLIST_DEST"
