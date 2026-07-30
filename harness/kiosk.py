"""
Monitor kiosk. Puts a program's web UI fullscreen on a screen plugged into the
Pi, using cage (a Wayland kiosk compositor) and Chromium --kiosk in one systemd
unit. One-time setup on the Pi:

    sudo apt install cage seatd chromium-browser

One program on the screen at a time, since it's one display. HDMI, DisplayPort
and USB DisplayLink all work. The launcher re-picks the display on every start,
because card numbers move between boots.
"""
import os
import shutil
import shlex
from pathlib import Path
from typing import Optional

from harness import config
from harness.programs import _run, load

KIOSK_UNIT = "harness-kiosk"
DRM_DIR = Path("/sys/class/drm")


class KioskError(Exception):
    """Something to fix on the Pi, usually a missing package. The API turns this
    into a 409 with the message as-is."""


# NO modprobe here: loading udl with a modern (DL-3xxx+) DisplayLink monitor
# attached wedges the kernel — udl claims a device it can't drive and hangs in
# USB forever. DisplayLinkManager loads evdi itself, and old DL-1x5 panels
# autoload udl via the kernel's own modalias matching.
_UNIT_TEMPLATE = """\
[Unit]
Description=PiHarness monitor kiosk: {name}
After=multi-user.target harness-prog-{name}.service displaylink-driver.service seatd.service
Wants=harness-prog-{name}.service

[Service]
# A kiosk Pi often has no keyboard or mouse — let cage start anyway.
Environment=WLR_LIBINPUT_NO_DEVICES=1
WorkingDirectory={workdir}
# The port wait below can take 180s — outlive systemd's default 90s start timeout.
TimeoutStartSec=240
# Give up on stuck processes fast at stop; a kernel-wedged one gets left behind
# (systemd logs and ignores it) instead of blocking every restart for minutes.
TimeoutStopSec=10
{pre_line}ExecStartPre=/bin/bash -c 'for i in $(seq 90); do (exec 3<>/dev/tcp/127.0.0.1/{port}) 2>/dev/null && exit 0; sleep 2; done; exit 0'
ExecStart=/bin/bash {script}
Restart=always
RestartSec=3
StandardOutput=journal
StandardError=journal
SyslogIdentifier=harness-kiosk

[Install]
WantedBy=multi-user.target
"""

# The launcher re-picks the display every start and works on any board:
# HDMI/DP → cage on the connected card; USB DisplayLink (evdi/udl) → cage
# renders on whatever real GPU has connectors (vc4, i915, amdgpu, …) and scans
# out on the USB card, with seatd as the session backend.
_LAUNCH_SCRIPT = """\
#!/bin/bash
# PiHarness kiosk launcher — regenerated on every Show; do not edit.
URL=http://127.0.0.1:{port}/
# Own profile dir on tmpfs: a previous chromium killed hard (or a stray one
# from a manual session) can never hold the profile lock and leave cage
# showing just a cursor. --no-sandbox: chromium refuses to run as root without
# it, and it only ever renders localhost.
FLAGS="--kiosk --no-sandbox --noerrdialogs --disable-infobars --incognito --user-data-dir=/run/piharness-kiosk"
# seatd session for every display kind — a root service has no logind seat,
# and seatd's socket goes stale after an unclean compositor exit ("Broken
# pipe"), so restart it fresh right before cage.
mkdir -p /run/user/0 && chmod 700 /run/user/0
export XDG_RUNTIME_DIR=/run/user/0 LIBSEAT_BACKEND=seatd
systemctl restart seatd 2>/dev/null && sleep 1
d=""; c=""
for s in /sys/class/drm/card*-*/status; do
  grep -q "^connected" "$s" || continue
  c=$(basename "$(dirname "$s")" | cut -d- -f1)
  d=$(basename "$(readlink -f /sys/class/drm/$c/device/driver)" 2>/dev/null)
  break
done
# evdi connectors often say "unknown", never "connected" — an evdi card
# existing at all means a DisplayLink USB display is attached.
if [ -z "$c" ]; then
  for dr in /sys/class/drm/card*/device/driver; do
    [ "$(basename "$(readlink -f "$dr")")" = evdi ] && d=evdi && break
  done
fi
if [ "$d" = evdi ] || [ "$d" = udl ]; then
  # Scanout device: the USB display's card — by-path when available (evdi on
  # any board, usb for old udl monitors), else the connector we detected.
  dl=""
  for l in /dev/dri/by-path/*evdi*-card /dev/dri/by-path/*usb*-card; do
    [ -e "$l" ] && dl=$(readlink -f "$l") && break
  done
  if [ -z "$dl" ] && [ -n "$c" ]; then dl=/dev/dri/$c; fi
  # Render device: first card that has connectors and isn't a USB display —
  # vc4 on a Pi, i915/amdgpu/nouveau on PCs. Render-only nodes (v3d) have no
  # connectors and are skipped.
  gpu=""
  for cardpath in /dev/dri/card*; do
    n=$(basename "$cardpath")
    if [ "$cardpath" = "$dl" ]; then continue; fi
    drv=$(basename "$(readlink -f /sys/class/drm/$n/device/driver)" 2>/dev/null)
    case "$drv" in evdi|udl) continue;; esac
    ls /sys/class/drm/"$n"-* >/dev/null 2>&1 || continue
    gpu=$cardpath; break
  done
  if [ -n "$gpu" ] && [ -n "$dl" ]; then export WLR_DRM_DEVICES="$gpu:$dl"
  elif [ -n "$dl" ]; then
    # No render GPU found — drive the USB display alone with software rendering.
    export WLR_DRM_DEVICES="$dl" WLR_RENDERER=pixman
  fi
  exec {cage} -- {browser} $FLAGS "$URL"
fi
[ -n "$c" ] && export WLR_DRM_DEVICES=/dev/dri/$c
exec {cage} -- {browser} $FLAGS "$URL"
"""


def monitor_connected() -> bool:
    for p in DRM_DIR.glob("card*-*/status"):
        try:
            if p.read_text().strip() == "connected":
                return True
        except OSError:
            pass
    return False


def current() -> Optional[str]:
    """The program currently armed for the monitor, if it still exists."""
    try:
        name = config.MONITOR_FILE.read_text().strip()
        return name if name in load() else None
    except OSError:
        return None


def show(name: str, prog: dict) -> None:
    """Arm the kiosk on this program. With no monitor plugged in it stays armed
    and displays as soon as one is attached."""
    browser = shutil.which("chromium-browser") or shutil.which("chromium")
    cage = shutil.which("cage")
    if not browser or not cage or not shutil.which("seatd"):
        raise KioskError("Kiosk tools missing. Run: "
                         "sudo apt install cage seatd chromium-browser")
    _run(["systemctl", "enable", "--now", "seatd"], timeout=15)
    config.KIOSK_SCRIPT.parent.mkdir(parents=True, exist_ok=True)
    config.KIOSK_SCRIPT.write_text(_LAUNCH_SCRIPT.format(
        port=prog["web_port"], browser=browser, cage=cage))
    os.chmod(config.KIOSK_SCRIPT, 0o755)

    # Optional per-program monitor command — fired on every kiosk start (each
    # show, each boot) as its OWN transient unit via systemd-run: it lives
    # outside the kiosk's cgroup, so no matter what it does (hang, crash, wedge
    # in the kernel — ddcutil on a dead i2c bus can) the kiosk never waits on
    # it, at start or at stop. Capped at 2 minutes.
    pre = prog.get("monitor_command")
    pre_line = (f"ExecStartPre=-/usr/bin/systemd-run --collect --no-block "
                f"--unit=harness-kiosk-cmd -p RuntimeMaxSec=120 "
                f"-p WorkingDirectory={prog['dir']} "
                f"/bin/bash -lc {shlex.quote(pre)}\n") if pre else ""

    config.UNIT_DIR.mkdir(parents=True, exist_ok=True)
    unit_path = config.UNIT_DIR / f"{KIOSK_UNIT}.service"
    new_text = _UNIT_TEMPLATE.format(
        name=name, port=prog["web_port"], workdir=prog["dir"],
        pre_line=pre_line, script=config.KIOSK_SCRIPT)
    try:
        changed = unit_path.read_text() != new_text
    except OSError:
        changed = True
    unit_path.write_text(new_text)
    if changed:
        _run(["systemctl", "daemon-reload"], timeout=15)
    # Always on: enable so the screen comes back by itself after a reboot.
    # --no-block: the kiosk's own start waits for the program's port (up to
    # 3 min) — enqueue it and return; the screen comes up when it's ready.
    code, out = _run(["systemctl", "enable", "--now", "--no-block", KIOSK_UNIT], timeout=30)
    if code > 0:
        raise KioskError(f"Could not start the kiosk: {out[-300:]}")
    if changed:
        # `enable --now` doesn't restart an already-active kiosk — restart so
        # the rewritten unit takes effect. An unchanged unit is left alone; a
        # restart storm on every edit helps nobody.
        _run(["systemctl", "restart", "--no-block", KIOSK_UNIT], timeout=30)
    try:
        config.MONITOR_FILE.write_text(name)
    except OSError:
        pass


def off() -> None:
    _run(["systemctl", "disable", "--now", KIOSK_UNIT], timeout=30)
    try:
        (config.UNIT_DIR / f"{KIOSK_UNIT}.service").unlink()
    except OSError:
        pass
    _run(["systemctl", "daemon-reload"], timeout=15)
    try:
        config.MONITOR_FILE.unlink()
    except OSError:
        pass


def kick(name: str) -> None:
    """Restart the kiosk when the program on screen restarts. Chromium never
    recovers on its own from a page that died under it."""
    if current() == name:
        _run(["systemctl", "restart", "--no-block", KIOSK_UNIT], timeout=30)


def refresh() -> None:
    """Rewrite an armed kiosk's unit and launcher from the current templates at
    startup, so kiosk fixes ship with harness updates. An unchanged unit is left
    alone, to avoid restart churn."""
    try:
        name = current()
        if not name:
            return
        prog = load().get(name)
        if prog and prog.get("web_port"):
            show(name, prog)
    except Exception:   # noqa: BLE001 - never block startup on the kiosk
        pass
