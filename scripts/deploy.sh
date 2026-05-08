#!/usr/bin/env bash
# =============================================================================
# deploy.sh — Pull latest code and restart services
#
# Run on the server after every push to main:
#
#   bash /opt/scte35/scripts/deploy.sh
#
# Or set up a GitHub Actions workflow / webhook to call this automatically.
# =============================================================================
set -euo pipefail

APP_DIR="/opt/scte35"

green()  { echo -e "\033[1;32m[OK]  $*\033[0m"; }
yellow() { echo -e "\033[1;33m[..] $*\033[0m"; }
red()    { echo -e "\033[1;31m[ERR] $*\033[0m"; exit 1; }

cd "$APP_DIR"

# ── 1. Pull latest code ───────────────────────────────────────────────────────
yellow "Pulling latest code from git…"
git pull --ff-only
green "Code updated"

# ── 2. Python dependencies ────────────────────────────────────────────────────
yellow "Syncing Python dependencies…"
"$APP_DIR/backend/.venv/bin/pip" install -r "$APP_DIR/backend/requirements.txt" -q
green "Python deps up to date"

# ── 3. Frontend build ─────────────────────────────────────────────────────────
yellow "Installing Node dependencies…"
npm --prefix "$APP_DIR/frontend" ci --silent

yellow "Building React dashboard…"
npm --prefix "$APP_DIR/frontend" run build
green "Frontend rebuilt"

# ── 4. Reload backend (zero-downtime) ────────────────────────────────────────
yellow "Reloading backend via PM2…"
# `pm2 reload` does a graceful in-place restart — no dropped connections.
pm2 reload ecosystem.config.cjs --update-env
pm2 save
green "Backend reloaded"

# ── 5. Reload nginx (picks up any config changes) ────────────────────────────
yellow "Reloading nginx…"
nginx -t && systemctl reload nginx
green "Nginx reloaded"

echo ""
echo "============================================================"
echo " Deploy complete! $(date '+%Y-%m-%d %H:%M:%S')"
echo "============================================================"
echo " pm2 status           — check process health"
echo " pm2 logs scte35-backend --lines 50  — recent backend logs"
echo "============================================================"
