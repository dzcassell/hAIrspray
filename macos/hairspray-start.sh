#!/bin/bash
#
# hAIrspray launchd wrapper.
#
# Invoked by the launchd agent in ~/Library/LaunchAgents/ at login.
# Waits for Docker Desktop to be fully up (which can take 30-90s on
# a cold boot while the Linux VM spins), then runs `docker compose
# up -d` in the project directory.
#
# Installed by ./install.sh; see the README's macOS section.
#
set -euo pipefail

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') hairspray-start: $*"; }

# ---------------------------------------------------------------------------
# Locate the docker binary.
# Docker Desktop's installer creates /usr/local/bin/docker on both
# Intel and Apple Silicon; some Homebrew setups symlink to
# /opt/homebrew/bin/docker. As a last resort we look inside the app
# bundle itself.
# ---------------------------------------------------------------------------

DOCKER=""
for candidate in \
    /usr/local/bin/docker \
    /opt/homebrew/bin/docker \
    /Applications/Docker.app/Contents/Resources/bin/docker ; do
  if [ -x "$candidate" ]; then
    DOCKER="$candidate"
    break
  fi
done

if [ -z "$DOCKER" ]; then
  log "error: could not find the docker binary in any expected location"
  log "       is Docker Desktop installed? https://www.docker.com/products/docker-desktop/"
  exit 1
fi

# ---------------------------------------------------------------------------
# Find the project directory.
# This script lives in <repo>/macos/; the repo root is one level up.
# ---------------------------------------------------------------------------

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"
log "project dir: $PROJECT_DIR"
log "docker:      $DOCKER"

# ---------------------------------------------------------------------------
# Wait for Docker Desktop to be ready.
# On login, Docker Desktop's "start at login" setting races us;
# `docker info` will fail until the Linux VM and socket are up.
# Poll for up to 3 minutes before giving up.
# ---------------------------------------------------------------------------

log "waiting for Docker Desktop..."
for i in $(seq 1 36); do
  if "$DOCKER" info >/dev/null 2>&1; then
    log "Docker Desktop ready after $((i * 5))s"
    break
  fi
  sleep 5
done

if ! "$DOCKER" info >/dev/null 2>&1; then
  log "error: Docker Desktop never became available after 3 minutes"
  log "       is Docker Desktop set to start at login?"
  log "       (Settings -> General -> 'Start Docker Desktop when you log in')"
  exit 1
fi

# ---------------------------------------------------------------------------
# Bring the stack up. --build is a no-op on unchanged images, so it's
# safe to leave in; on a fresh pull it ensures the container runs
# whatever code is in the working tree.
# ---------------------------------------------------------------------------

log "running: docker compose up -d --build"
exec "$DOCKER" compose up -d --build
