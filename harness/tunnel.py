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
import logging
import os
import re
import shutil
import time
from typing import Optional

from harness import config
from harness import programs

log = logging.getLogger("harness.tunnel")

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
    """Scrape the currently assigned hostname out of the journal. Quick tunnels
    announce theirs once per start and never again, so there is nowhere else to
    get it. The last match wins: after a restart the journal holds the old
    hostname as well as the live one."""
    code, out = programs._run(["journalctl", "-u", TUNNEL_UNIT, "--no-pager", "-n", "200"], timeout=10)
    if code != 0:
        return None
    found = _QUICK_HOST_RE.findall(out)
    return found[-1] if found else None


# The journal scrape is a subprocess, and the dashboard polls status(). Cache it
# briefly — but only briefly. Caching it permanently is what left the harness
# handing out a hostname that had stopped routing hours earlier.
_QUICK_TTL = 20
_quick_cache = [0.0, None]   # checked_at (monotonic), hostname


def _quick_hostname_cached() -> Optional[str]:
    now = time.monotonic()
    if now - _quick_cache[0] > _QUICK_TTL:
        _quick_cache[0] = now
        found = quick_hostname()
        if found:
            _quick_cache[1] = found
    return _quick_cache[1]


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
    # Quick mode: the journal is the truth and the stored value is only a
    # fallback for when it has rotated out. The other way round meant every
    # cloudflared restart — a reboot, a network blip, Restart=always doing its
    # job — silently changed the real address while the UI, the /apps links and
    # the MCP config all kept pointing at the dead one.
    host = _quick_hostname_cached() or state.get("hostname")
    if host and host != state.get("hostname"):
        save({**state, "hostname": host})
        log.info("quick tunnel address is now https://%s", host)
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
        # This start gets its own address; don't report the previous one.
        _quick_cache[0], _quick_cache[1] = 0.0, None

    unit_path = config.UNIT_DIR / f"{TUNNEL_UNIT}.service"
    text = _UNIT_TEMPLATE.format(env_file=_env_file(), binary=binary(), args=args)
    config.UNIT_DIR.mkdir(parents=True, exist_ok=True)
    if not unit_path.exists() or unit_path.read_text() != text:
        unit_path.write_text(text)
        programs._run(["systemctl", "daemon-reload"], timeout=15)

    save({"enabled": True, "mode": mode, "hostname": hostname})
    programs._run(["systemctl", "enable", TUNNEL_UNIT], timeout=30)
    # restart, not `enable --now`: on an already-running tunnel that was a no-op,
    # so asking for a quick tunnel again — the way you get out of a dead one —
    # left you looking at the same dead address.
    code, out = programs._run(["systemctl", "restart", TUNNEL_UNIT], timeout=45)
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
    """Re-assert the stored intent, and re-read where a quick tunnel currently
    lives. Called at startup and then on a timer.

    A tunnel the user turned on is expected to still be on after a reboot, and a
    quick tunnel that came back on a new address is expected to be reported at
    that new address rather than the one it had yesterday."""
    state = load()
    if not state.get("enabled"):
        return
    if unit_state() not in ("active", "activating"):
        try:
            enable(state.get("mode") or "quick", hostname=state.get("hostname"))
        except TunnelError as exc:
            log.warning("could not bring the tunnel back up: %s", exc)
        return
    public_url()   # persists the current quick hostname if it has rotated
