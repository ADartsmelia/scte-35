#!/usr/bin/env bash
# =============================================================================
# setup.sh — First-time server setup for SCTE-35 Overlay Engine
#
# Run ONCE as root (or with sudo) after cloning the repo to /opt/scte35:
#
#   sudo bash /opt/scte35/scripts/setup.sh
#
# What it does:
#   1. Installs system packages: ffmpeg, python3, nginx, nodejs, npm
#   2. Creates backend Python virtual environment and installs pip deps
#   3. Installs frontend npm dependencies and builds the React dashboard
#   4. Installs PM2 globally and configures log rotation
#   5. Starts the backend via PM2 and saves the process list
#   6. Symlinks the nginx config and reloads nginx
#   7. Installs the PM2 systemd startup hook so the service survives reboots
# =============================================================================
set -euo pipefail

APP_DIR="/opt/scte35"
LOG_DIR="/var/log/scte35"
NGINX_SITES="/etc/nginx/sites-enabled"

# ── Colour helpers ────────────────────────────────────────────────────────────
green()  { echo -e "\033[1;32m[OK]  $*\033[0m"; }
yellow() { echo -e "\033[1;33m[..] $*\033[0m"; }
red()    { echo -e "\033[1;31m[ERR] $*\033[0m"; exit 1; }

# ── 0. Sanity checks ──────────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || red "Please run as root: sudo bash $0"
[[ -d "$APP_DIR" ]] || red "Repo not found at $APP_DIR. Clone it first:
  git clone https://github.com/YOUR_ORG/YOUR_REPO.git $APP_DIR"

# ── 1. System packages ────────────────────────────────────────────────────────
yellow "Updating package lists…"
apt-get update -qq

yellow "Installing system dependencies…"
apt-get install -y -qq \
    ffmpeg \
    python3 python3-venv python3-pip \
    nodejs npm \
    nginx \
    git \
    curl

green "System packages installed"

# ── 2. Log directory ──────────────────────────────────────────────────────────
mkdir -p "$LOG_DIR"
green "Log directory: $LOG_DIR"

# ── 3. Python virtual environment ─────────────────────────────────────────────
yellow "Creating Python venv…"
python3 -m venv "$APP_DIR/backend/.venv"

yellow "Installing Python dependencies…"
"$APP_DIR/backend/.venv/bin/pip" install --upgrade pip -q
"$APP_DIR/backend/.venv/bin/pip" install -r "$APP_DIR/backend/requirements.txt" -q

green "Python environment ready"

# ── 4. Frontend build ─────────────────────────────────────────────────────────
yellow "Installing Node dependencies…"
npm --prefix "$APP_DIR/frontend" ci --silent

yellow "Building React dashboard…"
npm --prefix "$APP_DIR/frontend" run build

green "Frontend built → $APP_DIR/frontend/dist"

# ── 5. PM2 ───────────────────────────────────────────────────────────────────
yellow "Installing PM2 globally…"
npm install -g pm2 --silent

yellow "Installing pm2-logrotate…"
pm2 install pm2-logrotate --silent 2>/dev/null || true

yellow "Starting backend via PM2…"
pm2 start "$APP_DIR/ecosystem.config.cjs"
pm2 save

green "PM2 process started and saved"

# ── 6. Nginx ─────────────────────────────────────────────────────────────────
yellow "Symlinking nginx config…"
ln -sf "$APP_DIR/nginx/scte35.conf" "$NGINX_SITES/scte35"

# Remove default site if present
[[ -f "$NGINX_SITES/default" ]] && rm -f "$NGINX_SITES/default"

yellow "Testing nginx config…"
nginx -t

yellow "Reloading nginx…"
systemctl reload nginx || systemctl start nginx

green "Nginx configured and running"

# ── 7. PM2 startup hook ───────────────────────────────────────────────────────
yellow "Installing PM2 systemd startup hook…"
pm2 startup systemd -u root --hp /root | tail -1 | bash || true

green "PM2 startup hook installed"

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo "============================================================"
echo " Setup complete!"
echo "============================================================"
echo " Dashboard:  http://$(hostname -I | awk '{print $1}')"
echo " PM2 status: pm2 status"
echo " Backend log: pm2 logs scte35-backend"
echo " Nginx log:  tail -f /var/log/nginx/error.log"
echo "============================================================"
echo " To deploy updates, run:  bash $APP_DIR/scripts/deploy.sh"
echo "============================================================"
