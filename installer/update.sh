#!/usr/bin/env bash
# PiHarness updater — pulls the latest harness itself (not the programs it runs;
# those update on their own OTA settings).
#
#   update.sh            → interactive, asks before applying
#   update.sh --auto     → non-interactive, applies whatever is on the branch
#   update.sh --check    → print status; exit 0 up-to-date, 1 update available
set -euo pipefail

INSTALL_DIR="/opt/piharness"
LOG_DIR="/var/log/piharness"
LOG="$LOG_DIR/update.log"
BRANCH="${HARNESS_BRANCH:-main}"

AUTO=0; CHECK_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --auto)  AUTO=1 ;;
    --check) CHECK_ONLY=1 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "Run as root."; exit 1; }
mkdir -p "$LOG_DIR"
exec > >(tee -a "$LOG") 2>&1
echo "--- $(date -Iseconds) update.sh $* ---"

cd "$INSTALL_DIR"
CURRENT_SHA=$(git rev-parse HEAD 2>/dev/null || echo none)
CURRENT_VER=$(cat VERSION 2>/dev/null || echo unknown)

git fetch origin "$BRANCH" --quiet || { echo "Cannot reach GitHub — skipping."; exit 0; }
LATEST_SHA=$(git rev-parse "origin/$BRANCH")
LATEST_VER=$(git show "origin/$BRANCH:VERSION" 2>/dev/null || echo unknown)

if [[ "$CURRENT_SHA" == "$LATEST_SHA" ]]; then
  echo "Already up to date ($CURRENT_VER)."
  exit 0
fi

echo "Update available: $CURRENT_VER ($CURRENT_SHA) → $LATEST_VER ($LATEST_SHA)"
[[ $CHECK_ONLY -eq 1 ]] && exit 1

if [[ $AUTO -eq 0 ]]; then
  read -rp "Apply it now? [y/N] " CONFIRM
  [[ "${CONFIRM,,}" == "y" ]] || { echo "Skipped."; exit 0; }
fi

git pull origin "$BRANCH" --ff-only
"$INSTALL_DIR/venv/bin/pip" install -q -r requirements.txt
# Ship changes to the unit file itself, keeping any custom port.
PORT=$(grep -oP -- '--port \K[0-9]+' /etc/systemd/system/piharness.service 2>/dev/null || echo 8080)
sed "s|--port 8080|--port $PORT|" "$INSTALL_DIR/installer/piharness.service" \
  > /etc/systemd/system/piharness.service
systemctl daemon-reload

if systemctl is-active --quiet piharness; then
  systemctl restart piharness
  echo "Service restarted."
fi

echo "Update complete → $(cat VERSION)"
echo "Rollback:  cd $INSTALL_DIR && git checkout $CURRENT_SHA && systemctl restart piharness"
