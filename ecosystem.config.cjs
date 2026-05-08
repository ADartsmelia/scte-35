/**
 * PM2 Ecosystem — SCTE-35 Overlay Engine
 *
 * Usage:
 *   pm2 start ecosystem.config.cjs          # start
 *   pm2 reload ecosystem.config.cjs         # zero-downtime reload
 *   pm2 stop scte35-backend                 # stop
 *   pm2 save                                # persist across reboots
 *   pm2 startup                             # install systemd unit (run once)
 */

"use strict";

// Change this to wherever you cloned the repo on the server.
const APP_DIR = "/opt/scte35";

module.exports = {
  apps: [
    {
      name: "scte35-backend",

      // Run uvicorn from inside the venv directly — no shell activation needed.
      script: `${APP_DIR}/backend/.venv/bin/uvicorn`,
      args: "app.main:app --host 127.0.0.1 --port 8000 --workers 1",

      cwd: `${APP_DIR}/backend`,

      // Don't let PM2 treat this as a Node script.
      interpreter: "none",

      // Restart automatically on crash, but give up after 10 fast failures.
      autorestart: true,
      max_restarts: 10,
      min_uptime: "10s",
      restart_delay: 3000,

      // Never watch files — rely on manual `pm2 reload` after deploy.
      watch: false,

      // Log files (create /var/log/scte35/ first — setup.sh does this).
      error_file: "/var/log/scte35/backend-error.log",
      out_file:   "/var/log/scte35/backend-out.log",
      log_date_format: "YYYY-MM-DD HH:mm:ss Z",

      // Keep last 7 days of rotated logs (requires pm2-logrotate module).
      // Install once: pm2 install pm2-logrotate
      env: {
        NODE_ENV: "production",
        PYTHONUNBUFFERED: "1",
      },
    },
  ],
};
