"""
Cloudflare Tunnel: a public HTTPS address for the Pi with no port forwarding,
no static IP and no open inbound port on the router. cloudflared dials out and
Cloudflare proxies traffic back down that connection.

Two modes, because they suit different people:

  quick  — `cloudflared tunnel --url`. No Cloudflare account, no config. You get
           a random https://<words>.trycloudflare.com hostname that changes every
           restart. Good for trying it; useless for a bookmark.
  named  — `cloudflared tunnel run` with a connector token from the Cloudflare
           dashboard. Stable hostname on a domain you control, survives restarts.

The token is a credential: it is written to an EnvironmentFile at 0600 and read
by cloudflared as TUNNEL_TOKEN, so it never appears in the unit file, in argv,
or in `ps` output — the same treatment the GitHub tokens get in programs.py.
"""
import json
import os
import re
import shutil
import time
from typing import Optional

from harness import config
from harness import programs

TUNNEL_UNIT = "harness-tunnel"
MODES = ("quick", "named")

# The hostname cloudflared prints once a quick tunnel is up. It only ever
# appears in the journal, so that is where it has to be read from.
_QUICK_HOST_RE = re.compile(r"https://([a-z0-9-]+\.trycloudflare\.com)")
# A connector token is a long base64url blob. Loose on purpose: Cloudflare has
# changed the length before, and a wrong guess here locks people out of a
# feature for no security gain.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-=.]{40,4096}$")

_UNIT_TEMPLATE = """\
[Unit]
Description=PiHarness Cloudflare tunnel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=-{env_file}
ExecStart={binary} tunnel --no-autoupdate {args}
Restart=always
RestartSec=10
# The tunnel is plumbing, not the workload: it yields to the harness and to the
# programs, the same as any imported program does.
Nice=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=harness-tunnel

[Install]
WantedBy=multi-user.target
"""


class TunnelError(Exception):
    """Something the user can fix, surfaced as a 409 rather than a 500."""


def binary() -> Optional[str]:
    return shutil.which("cloudflared")


def installed() -> bool:
    return binary() is not None


# ── Stored state ──────────────────────────────────────────────────────────────

def _state_file():
    return config.CONFIG_DIR / "tunnel.json"


def _env_file():
    return config.CONFIG_DIR / "tunnel.env"


def load() -> dict:
    try:
        return json.loads(_state_file().read_text())
    except Exception:   # noqa: BLE001 - missing or corrupt reads as "off"
        return {}


def save(state: dict) -> None:
    try:
        config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        _state_file().write_text(json.dumps(state, indent=2))
        os.chmod(_state_file(), 0o600)
    except OSError:
        pass


def _write_token(token: Optional[str]) -> None:
    path = _env_file()
    if not token:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(f"TUNNEL_TOKEN={token}\n")
    os.chmod(path, 0o600)


# ── Status ────────────────────────────────────────────────────────────────────

def unit_state() -> str:
    code, out = programs._run(["systemctl", "is-active", TUNNEL_UNIT], timeout=5)
    return out if code >= 0 and out in ("active", "inactive", "failed", "activating") else "unknown"


def quick_hostname() -> Optional[str]:
    """Scrape the assigned hostname out of the journal. Quick tunnels announce
    theirs once at startup and never again, so there is nowhere else to get it."""
    code, out = programs._run(["journalctl", "-u", TUNNEL_UNIT, "--no-pager", "-n", "200"], timeout=10)
    if code != 0:
        return None
    found = _QUICK_HOST_RE.findall(out)
    return found[-1] if found else None


def public_url() -> Optional[str]:
    """The https origin this tunnel serves, or None when it isn't up yet."""
    state = load()
    if not state.get("enabled"):
        return None
    if unit_state() not in ("active", "activating"):
        return None
    if state.get("mode") == "named":
        host = state.get("hostname")
        return f"https://{host}" if host else None
    host = state.get("hostname") or quick_hostname()
    if host and host != state.get("hostname"):
        # Cache it so the dashboard doesn't shell out to journalctl every poll.
        save({**state, "hostname": host})
    return f"https://{host}" if host else None


def status() -> dict:
    state = load()
    active = unit_state()
    url = public_url()
    return {
        "installed": installed(),
        "enabled": bool(state.get("enabled")),
        "mode": state.get("mode"),
        "state": active,
        "hostname": state.get("hostname"),
        "url": url,
        "has_token": bool(state.get("mode") == "named" and _env_file().exists()),
        # A quick tunnel's address is regenerated on every restart, so anything
        # the user bookmarks or shares will break. Say so rather than imply
        # permanence.
        "ephemeral": state.get("mode") == "quick",
    }


# ── Control ───────────────────────────────────────────────────────────────────

def enable(mode: str, token: Optional[str] = None, hostname: Optional[str] = None) -> dict:
    if mode not in MODES:
        raise TunnelError(f"mode must be one of {', '.join(MODES)}")
    if not installed():
        raise TunnelError(
            "cloudflared isn't installed. Install it with: "
            "curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/"
            "cloudflared-linux-arm64.deb -o /tmp/cf.deb && sudo dpkg -i /tmp/cf.deb")

    if mode == "named":
        token = (token or "").strip()
        if not token:
            # Already configured and just being restarted: keep the stored token.
            if not _env_file().exists():
                raise TunnelError(
                    "A named tunnel needs a connector token from the Cloudflare "
                    "dashboard (Zero Trust > Networks > Tunnels > your tunnel > Install).")
        elif not _TOKEN_RE.fullmatch(token):
            raise TunnelError("That doesn't look like a Cloudflare connector token.")
        else:
            _write_token(token)
        args = "run"
        hostname = (hostname or "").strip().removeprefix("https://").removeprefix("http://").strip("/")
        if hostname and not re.fullmatch(r"[A-Za-z0-9.-]{3,253}", hostname):
            raise TunnelError("That doesn't look like a hostname.")
    else:
        _write_token(None)
        args = f"--url http://127.0.0.1:{config.PORT}"
        hostname = None

    unit_path = config.UNIT_DIR / f"{TUNNEL_UNIT}.service"
    text = _UNIT_TEMPLATE.format(env_file=_env_file(), binary=binary(), args=args)
    config.UNIT_DIR.mkdir(parents=True, exist_ok=True)
    if not unit_path.exists() or unit_path.read_text() != text:
        unit_path.write_text(text)
        programs._run(["systemctl", "daemon-reload"], timeout=15)

    save({"enabled": True, "mode": mode, "hostname": hostname})
    code, out = programs._run(["systemctl", "enable", "--now", TUNNEL_UNIT], timeout=45)
    if code > 0:
        raise TunnelError(f"Could not start the tunnel: {out[-300:]}")

    # A quick tunnel takes a few seconds to be assigned a hostname. Give it a
    # short grace period so the UI can show the address immediately instead of
    # an empty box the user has to refresh.
    if mode == "quick":
        for _ in range(10):
            time.sleep(1)
            if quick_hostname():
                break
    return status()


def disable() -> dict:
    programs._run(["systemctl", "disable", "--now", TUNNEL_UNIT], timeout=30)
    try:
        (config.UNIT_DIR / f"{TUNNEL_UNIT}.service").unlink()
    except FileNotFoundError:
        pass
    programs._run(["systemctl", "daemon-reload"], timeout=15)
    _write_token(None)
    save({"enabled": False, "mode": None, "hostname": None})
    return status()


def logs(lines: int = 80) -> str:
    code, out = programs._run(["journalctl", "-u", TUNNEL_UNIT, "--no-pager", "-n",
                      str(min(lines, 400))], timeout=10)
    return out if code == 0 else "No tunnel logs available."


def refresh() -> None:
    """At startup, re-assert the stored intent. A tunnel the user turned on is
    expected to still be on after a reboot."""
    state = load()
    if not state.get("enabled"):
        return
    if unit_state() in ("active", "activating"):
        return
    try:
        enable(state.get("mode") or "quick", hostname=state.get("hostname"))
    except TunnelError:
        pass


def is_active() -> bool:
    """True when the harness is reachable from the public internet through the
    tunnel. Used to harden cookies and add HSTS without needing configuration."""
    return bool(load().get("enabled")) and unit_state() in ("active", "activating")
