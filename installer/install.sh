#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  PiHarness — one-shot installer
#  Raspberry Pi 3 → Pi 5 (ARM64/ARMv7), Raspberry Pi OS Bullseye or newer, and
#  any other Debian/Ubuntu box with systemd.
#
#  Install (stdin is preserved, so the prompts work):
#    bash <(curl -fsSL https://raw.githubusercontent.com/MeridianAlgo/PiHarness/main/installer/install.sh)
#
#  Fully unattended:
#    HARNESS_PASSWORD=secret123 HARNESS_KIOSK=yes bash <(curl -fsSL …/install.sh)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

REPO_URL="https://github.com/MeridianAlgo/PiHarness.git"
BRANCH="${HARNESS_BRANCH:-main}"
INSTALL_DIR="/opt/piharness"
CONFIG_DIR="/etc/piharness"
PROGRAMS_DIR="$INSTALL_DIR/programs"
SERVICE_FILE="/etc/systemd/system/piharness.service"
PORT="${HARNESS_PORT:-8080}"

R='\033[0;31m'; G='\033[0;32m'; Y='\033[1;33m'; B='\033[0;34m'; C='\033[0;36m'; NC='\033[0m'
info() { echo -e "${G}[✓]${NC} $*"; }
step() { echo -e "${B}[→]${NC} $*"; }
warn() { echo -e "${Y}[!]${NC} $*"; }
die()  { echo -e "${R}[✗]${NC} $*" >&2; exit 1; }
section() { echo -e "\n${C}── $* ──${NC}"; }

# ── Root ─────────────────────────────────────────────────────────────────────
# Run via bash <(curl …) there's no local file to re-run with sudo, so
# re-download and exec under sudo automatically.
if [[ $EUID -ne 0 ]]; then
  step "Installer needs root — re-running with sudo (a password may be required)…"
  _TMP=$(mktemp /tmp/piharness-install.XXXXXXXX.sh)
  curl -fsSL "https://raw.githubusercontent.com/MeridianAlgo/PiHarness/$BRANCH/installer/install.sh" > "$_TMP"
  exec sudo -E bash "$_TMP" "$@"
fi

section "PiHarness installer"
echo "  Architecture : $(uname -m)"
echo "  OS           : $(. /etc/os-release && echo "$PRETTY_NAME")"
echo "  Kernel       : $(uname -r)"
command -v systemctl &>/dev/null || die "systemd is required — this installer supervises programs with it."

# ── Python ───────────────────────────────────────────────────────────────────
PYTHON_BIN=""
for bin in python3.12 python3.11 python3.10 python3.9 python3; do
  command -v "$bin" &>/dev/null || continue
  if "$bin" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
    PYTHON_BIN="$bin"; break
  fi
done
if [[ -z "$PYTHON_BIN" ]]; then
  warn "Python 3.9+ not found — installing python3…"
  apt-get update -qq
  apt-get install -y --no-install-recommends python3 python3-venv python3-dev \
    || die "Could not install Python 3.9+. Upgrade the OS and try again."
  PYTHON_BIN="python3"
fi
info "Using $PYTHON_BIN ($("$PYTHON_BIN" --version))"

# ── System packages ──────────────────────────────────────────────────────────
section "System packages"
step "Updating package lists…"
apt-get update -qq
# git and node cover cloning and Node programs; the venv module builds each
# Python program's private virtualenv.
apt-get install -y --no-install-recommends \
  git curl ca-certificates python3-venv nodejs npm avahi-daemon \
  || warn "Some packages failed to install — Node programs may not run"
systemctl enable --now avahi-daemon 2>/dev/null || true
info "System packages installed"

# ── Kiosk tools (optional) ───────────────────────────────────────────────────
section "Monitor kiosk"
KIOSK="${HARNESS_KIOSK:-}"
if [[ -z "$KIOSK" ]]; then
  read -rp "Install the kiosk tools, so a program's web UI can fill a screen plugged into the Pi? [y/N] " KIOSK || true
fi
if [[ "${KIOSK,,}" =~ ^(y|yes)$ ]]; then
  step "Installing cage, seatd and Chromium (this takes a few minutes)…"
  apt-get install -y --no-install-recommends cage seatd chromium-browser \
    || apt-get install -y --no-install-recommends cage seatd chromium \
    || warn "Kiosk tools failed to install — 'Show on monitor' will tell you what's missing"
  systemctl enable seatd 2>/dev/null || true
  info "Kiosk tools installed"
else
  info "Skipped — install later with: sudo apt install cage seatd chromium-browser"
fi

# ── Code ─────────────────────────────────────────────────────────────────────
section "PiHarness"
mkdir -p "$CONFIG_DIR" "$PROGRAMS_DIR"
chmod 700 "$CONFIG_DIR"
if [[ -d "$INSTALL_DIR/.git" ]]; then
  step "Updating the existing install…"
  git -C "$INSTALL_DIR" fetch origin "$BRANCH" --quiet
  git -C "$INSTALL_DIR" checkout "$BRANCH" --quiet
  git -C "$INSTALL_DIR" pull origin "$BRANCH" --ff-only --quiet
else
  step "Cloning $REPO_URL…"
  # The clone target holds programs/ already — clone beside it and move in.
  _TMP_CLONE=$(mktemp -d)
  git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$_TMP_CLONE/repo" --quiet
  mkdir -p "$INSTALL_DIR"
  cp -a "$_TMP_CLONE/repo/." "$INSTALL_DIR/"
  rm -rf "$_TMP_CLONE"
fi

step "Building the virtualenv…"
"$PYTHON_BIN" -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install -q --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"
info "Dependencies installed"

# ── Service ──────────────────────────────────────────────────────────────────
section "Service"
sed "s|--port 8080|--port $PORT|" "$INSTALL_DIR/installer/piharness.service" > "$SERVICE_FILE"
if [[ "$PORT" != "8080" ]]; then
  printf 'HARNESS_PORT=%s\n' "$PORT" >> "$CONFIG_DIR/env"
  chmod 600 "$CONFIG_DIR/env"
fi
systemctl daemon-reload
systemctl enable piharness --quiet
systemctl restart piharness
sleep 2
if systemctl is-active --quiet piharness; then
  info "piharness.service is running"
else
  die "The service failed to start — check: journalctl -u piharness -n 40"
fi

# ── Account ──────────────────────────────────────────────────────────────────
# The first account is created through the web UI, or here if a password was
# supplied. Nothing is reachable until it exists.
section "Account"
if [[ -s "$CONFIG_DIR/credentials.json" ]]; then
  info "An account already exists — sign in with it"
elif [[ -n "${HARNESS_PASSWORD:-}" ]]; then
  USERNAME="${HARNESS_USERNAME:-admin}"
  # Built with json.dumps so a password containing quotes or backslashes
  # can't produce a malformed body.
  BODY=$(HARNESS_USERNAME="$USERNAME" "$PYTHON_BIN" -c \
    'import json, os; print(json.dumps({"username": os.environ["HARNESS_USERNAME"], "password": os.environ["HARNESS_PASSWORD"]}))')
  if curl -fsS -X POST "http://127.0.0.1:$PORT/api/setup" \
       -H 'Content-Type: application/json' -d "$BODY" >/dev/null; then
    info "Account '$USERNAME' created"
  else
    warn "Could not create the account — do it in the browser on first visit"
  fi
else
  info "Open the web UI and create your account on the first visit"
fi

HOST=$(hostname)
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
section "Done"
echo ""
echo -e "  Web UI   ${C}http://${HOST}.local:${PORT}${NC}"
[[ -n "$IP" ]] && echo -e "           ${C}http://${IP}:${PORT}${NC}" || true
echo ""
echo "  Logs     journalctl -u piharness -f"
echo "  Update   sudo $INSTALL_DIR/installer/update.sh"
echo "  Programs $PROGRAMS_DIR"
echo ""
