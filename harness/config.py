"""Paths and tunables. Each one has an environment override so a dev box can
run out of a temp directory instead of /etc and /opt."""
import os
from pathlib import Path

# State the harness keeps for itself: credentials, program registry, secrets.
# Root-only in a real install.
CONFIG_DIR = Path(os.environ.get("HARNESS_CONFIG_DIR", "/etc/piharness"))
CREDENTIALS_FILE = CONFIG_DIR / "credentials.json"
REGISTRY_FILE = CONFIG_DIR / "programs.json"
# Per-program secrets, injected via the unit's EnvironmentFile. Kept outside
# the clone so a program's own `git pull` can't expose them.
ENV_DIR = CONFIG_DIR / "program-env"
MONITOR_FILE = CONFIG_DIR / "monitor-program"
KIOSK_SCRIPT = CONFIG_DIR / "kiosk-launch.sh"

# Cloned program code.
PROGRAMS_DIR = Path(os.environ.get("HARNESS_PROGRAMS_DIR", "/opt/piharness/programs"))
UNIT_DIR = Path(os.environ.get("HARNESS_UNIT_DIR", "/etc/systemd/system"))

# The port the harness listens on. Reserved, so a program can't claim it.
PORT = int(os.environ.get("HARNESS_PORT", "8080"))

SESSION_TTL_HOURS = int(os.environ.get("HARNESS_SESSION_TTL", "24"))
# Set to 1 when serving HTTPS directly, with no reverse proxy in front, so the
# session cookie gets the Secure flag.
COOKIE_SECURE = os.environ.get("HARNESS_COOKIE_SECURE", "0") == "1"
COOKIE_NAME = "harness_session"

# Public origin for the /apps/<name>/ links, for when the Pi is behind a domain
# or a tunnel. Left empty, Tailscale is autodetected instead.
PUBLIC_URL = os.environ.get("HARNESS_PUBLIC_URL", "").rstrip("/")

# How often the unattended updater checks GitHub for programs on ota="auto".
AUTO_UPDATE_INTERVAL = int(os.environ.get("HARNESS_AUTO_UPDATE_INTERVAL", str(6 * 3600)))

# How often host metrics are sampled into the in-memory history the dashboard
# draws. 5s over 120 points is 10 minutes of trend, which is the window that
# answers "is it climbing right now".
METRICS_INTERVAL = int(os.environ.get("HARNESS_METRICS_INTERVAL", "5"))

# How often the tunnel is re-checked: restarted if it died, re-read if a quick
# tunnel came back on a different address. Cheap (two systemctl calls), so it
# can run often enough that a rotated address is never stale for long.
TUNNEL_CHECK_INTERVAL = int(os.environ.get("HARNESS_TUNNEL_CHECK_INTERVAL", "60"))

# Requests per minute per IP against /api, before a 429. Generous enough that
# the dashboard polling plus a person clicking never hits it, low enough that a
# leaked token can't drive git and systemctl in a loop.
RATE_LIMIT = int(os.environ.get("HARNESS_RATE_LIMIT", "240"))
# Sign-in is separately and much more tightly limited; auth.py's throttle
# handles repeated failures, this caps the raw attempt rate.
RATE_LIMIT_LOGIN = int(os.environ.get("HARNESS_RATE_LIMIT_LOGIN", "20"))

# Largest request body accepted, in bytes. The biggest legitimate one is a
# secrets blob, capped at 32 KB by the endpoint itself.
MAX_BODY_BYTES = int(os.environ.get("HARNESS_MAX_BODY", str(1024 * 1024)))

UI_DIR = Path(__file__).parent.parent / "ui"
# The MCP server. Served over HTTP so an agent machine can fetch it
# straight from the Pi rather than going via GitHub.
AGENT_DIR = Path(__file__).parent.parent / "agent"

_raw_origins = os.environ.get("HARNESS_CORS_ORIGINS", "")
CORS_ORIGINS = [o.strip() for o in _raw_origins.split(",") if o.strip()]
