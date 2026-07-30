"""
Clone a program from GitHub, install its dependencies, wrap it in a systemd unit
and keep it running.

Each program is a git clone under PROGRAMS_DIR/<name> supervised by its own unit
(harness-prog-<name>) with Restart=always, so it survives crashes and reboots.
One JSON registry file holds the lot, no database.

No HTTP in here. Routes are in api.py, the monitor kiosk in kiosk.py.
"""
import base64
import datetime
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Optional

from harness import config

# Program names become unit names and directory names, so keep them boring.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,40}$")
# GitHub HTTPS repos only — no arbitrary hosts or flags smuggled into `git clone`.
REPO_RE = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?(\.git)?/?$")
SHORT_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")   # owner/repo shorthand
# GitHub token: classic ghp_… or fine-grained github_pat_….
TOKEN_RE = re.compile(r"^[A-Za-z0-9_.-]{8,255}$")

# github: check GitHub, you click Update. auto: check and apply unattended.
# self: the program runs its own updater and the harness stays out.
OTA_MODES = ("github", "auto", "self")

_lock = threading.Lock()
# name -> {"phase": "cloning"|"installing"|"starting"}
_imports: dict[str, dict] = {}

_UNIT_TEMPLATE = """\
[Unit]
Description=PiHarness program: {name}
After=network-online.target

[Service]
Type=simple
WorkingDirectory={workdir}
{env_line}EnvironmentFile=-{env_file}
# Last, so a key in the user's secrets file can't shadow HARNESS_TOKEN.
EnvironmentFile=-{harness_env_file}
ExecStart=/bin/bash -lc {cmd}
Restart=always
RestartSec=5
# Background programs yield to the harness and the OS — a hungry always-on app
# can't hog the Pi or heat it up while the system stays responsive. It still
# gets the whole CPU when nothing else wants it. Soft priority, not a hard
# CPUQuota: capping arbitrary user programs would silently break CPU-bound ones.
Nice=15
CPUWeight=20
IOWeight=20
StandardOutput=journal
StandardError=journal
SyslogIdentifier=harness-prog-{name}

[Install]
WantedBy=multi-user.target
"""


def _run(cmd: list[str], cwd=None, timeout=60, env=None) -> tuple[int, str]:
    """Run a command, never raise. Returns (returncode, combined output).
    A returncode of -1 means it couldn't be run at all, which is what a dev box
    without git or systemctl looks like."""
    try:
        full_env = {**os.environ, **env} if env else None
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd,
                           timeout=timeout, env=full_env)
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as exc:  # noqa: BLE001
        return -1, str(exc)


# ── Registry ──────────────────────────────────────────────────────────────────

def load() -> dict:
    try:
        return json.loads(config.REGISTRY_FILE.read_text())
    except Exception:   # noqa: BLE001 - missing or corrupt reads as empty
        return {}


def save(d: dict) -> None:
    try:
        config.REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
        config.REGISTRY_FILE.write_text(json.dumps(d, indent=2))
        os.chmod(config.REGISTRY_FILE, 0o600)   # holds GitHub tokens — root-only
    except OSError:
        pass


def get(name: str) -> Optional[dict]:
    return load().get(name) if NAME_RE.fullmatch(name) else None


def env_file(name: str) -> Path:
    return config.ENV_DIR / f"{name}.env"


def harness_env_file(name: str) -> Path:
    """The harness's own variables for a program, kept apart from the user's
    secrets file: the Secrets editor replaces that file wholesale, and would
    otherwise wipe the program's credential every time someone saved."""
    return config.ENV_DIR / f"{name}.harness.env"


def ensure_program_token(name: str) -> None:
    """Give a program the address of the harness and a token bound to itself,
    so it can write back a secret it rotated at runtime.

    This 0600 file is the only cleartext copy — the token store keeps a hash,
    like every other token. Delete the file and the next unit write mints a new
    one. Not put in the unit itself, which is world-readable in
    /etc/systemd/system."""
    from harness import auth
    path = harness_env_file(name)
    if path.exists():
        return
    auth.revoke_program_tokens(name)   # a stale hash with no cleartext left
    token = auth.create_api_token("harness", f"program: {name}", "program", program=name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"HARNESS_URL=http://127.0.0.1:{config.PORT}\n"
                        f"HARNESS_PROGRAM={name}\n"
                        f"HARNESS_TOKEN={token}\n")
        os.chmod(path, 0o600)
    except OSError:
        auth.revoke_program_tokens(name)   # unwritable: don't leave a live token nobody holds


# ── Git ───────────────────────────────────────────────────────────────────────

def git_env(prog: dict) -> Optional[dict]:
    """Auth for git against a private GitHub repo, passed as environment
    config — the token never appears in argv (visible in `ps`), in the stored
    repo URL, or in the clone's .git/config."""
    token = prog.get("token")
    if not token:
        return None
    b64 = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {"GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {b64}"}


def local_sha(prog: dict) -> Optional[str]:
    code, out = _run(["git", "-C", prog["dir"], "rev-parse", "HEAD"], timeout=10)
    return out if code == 0 else None


def remote_sha(prog: dict) -> Optional[str]:
    code, out = _run(["git", "ls-remote", prog["repo_url"], "HEAD"], timeout=20,
                     env=git_env(prog))
    return out.split()[0] if code == 0 and out else None


# ── systemd units ─────────────────────────────────────────────────────────────

def unit(name: str) -> str:
    return f"harness-prog-{name}"


def unit_state(name: str) -> str:
    code, out = _run(["systemctl", "is-active", unit(name)], timeout=5)
    return out if code >= 0 and out in ("active", "inactive", "failed", "activating") else "unknown"


def unit_text(name: str, prog: dict) -> str:
    cmd = shlex.quote(prog["start_command"])
    env_line = f"Environment=PORT={prog['web_port']}\n" if prog.get("web_port") else ""
    return _UNIT_TEMPLATE.format(
        name=name, workdir=prog["dir"], env_line=env_line,
        env_file=env_file(name), harness_env_file=harness_env_file(name), cmd=cmd)


def write_unit(name: str, prog: dict) -> None:
    ensure_program_token(name)
    config.UNIT_DIR.mkdir(parents=True, exist_ok=True)
    (config.UNIT_DIR / f"{unit(name)}.service").write_text(unit_text(name, prog))
    _run(["systemctl", "daemon-reload"], timeout=15)


def refresh_units() -> None:
    """Rewrite each program's unit from the current template at startup, so unit
    fixes ship with harness updates. Only units whose text actually changed get
    rewritten and restarted, making this a no-op on every boot after the one
    that introduced a change."""
    changed = []
    for name, prog in load().items():
        if not prog.get("start_command"):
            continue
        ensure_program_token(name)   # programs imported before tokens existed
        path = config.UNIT_DIR / f"{unit(name)}.service"
        try:
            new_text = unit_text(name, prog)
            if path.exists() and path.read_text() == new_text:
                continue
            path.write_text(new_text)
            changed.append(name)
        except OSError:   # dev box without /etc/systemd, or an unreadable unit
            continue
    if not changed:
        return
    _run(["systemctl", "daemon-reload"], timeout=15)
    for name in changed:   # restart once so the new limits take effect
        if unit_state(name) == "active":
            _run(["systemctl", "restart", "--no-block", unit(name)], timeout=30)


# ── Import ────────────────────────────────────────────────────────────────────

def detect_start_command(repo_dir: Path) -> Optional[str]:
    """Best-effort run command from the repo's own conventions."""
    pkg = repo_dir / "package.json"
    if pkg.exists():
        try:
            scripts = json.loads(pkg.read_text()).get("scripts", {})
            if "start" in scripts:
                return "npm start"
            if (repo_dir / "index.js").exists():
                return "node index.js"
        except ValueError:
            pass
    py = repo_dir / ".venv/bin/python"
    python = str(py) if py.exists() else "python3"
    for entry in ("main.py", "app.py", "server.py"):
        if (repo_dir / entry).exists():
            return f"{python} {entry}"
    if (repo_dir / "index.js").exists():
        return "node index.js"
    return None


def install_deps(name: str, repo_dir: Path) -> Optional[str]:
    """Install declared dependencies; returns an error string or None."""
    if (repo_dir / "requirements.txt").exists():
        _imports.setdefault(name, {})["phase"] = "installing"
        code, out = _run(["python3", "-m", "venv", str(repo_dir / ".venv")], timeout=120)
        if code != 0:
            return f"venv failed: {out[-300:]}"
        code, out = _run([str(repo_dir / ".venv/bin/pip"), "install", "-q",
                          "-r", str(repo_dir / "requirements.txt")], timeout=600)
        if code != 0:
            return f"pip install failed: {out[-300:]}"
    if (repo_dir / "package.json").exists():
        _imports.setdefault(name, {})["phase"] = "installing"
        code, out = _run(["npm", "install", "--omit=dev", "--no-audit", "--no-fund"],
                         cwd=str(repo_dir), timeout=600)
        if code != 0:
            return f"npm install failed: {out[-300:]}"
    return None


def import_worker(name: str, repo_url: str, start_command: Optional[str]) -> None:
    """Clone, install, start. Runs off the request thread. A failure removes the
    half-cloned directory and leaves the program marked "error" with the
    reason."""
    repo_dir = config.PROGRAMS_DIR / name
    try:
        config.PROGRAMS_DIR.mkdir(parents=True, exist_ok=True)
        auth = git_env(load().get(name) or {})
        code, out = _run(["git", "clone", "--depth", "1", repo_url, str(repo_dir)],
                         timeout=300, env=auth)
        if code != 0:
            # A private repo without a token 404s / prompts for credentials.
            hint = (". Is the repo private? Add an access token and re-import."
                    if not auth and ("could not read Username" in out or "Repository not found" in out)
                    else "")
            raise RuntimeError(f"git clone failed: {out[-300:]}{hint}")

        err = install_deps(name, repo_dir)
        if err:
            raise RuntimeError(err)

        cmd = start_command or detect_start_command(repo_dir)
        with _lock:
            d = load()
            prog = d[name]
            prog["start_command"] = cmd
            prog["status"] = "ready" if cmd else "needs_command"
            save(d)

        if cmd:
            _imports.setdefault(name, {})["phase"] = "starting"
            write_unit(name, prog)
            _run(["systemctl", "enable", "--now", unit(name)], timeout=30)
        _imports.pop(name, None)
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(repo_dir, ignore_errors=True)
        with _lock:
            d = load()
            if name in d:
                d[name]["status"] = "error"
                d[name]["error"] = str(exc)
                save(d)
        _imports.pop(name, None)


def start_import(name: str, url: str, req) -> None:
    """Register the program and kick off its background import."""
    with _lock:
        d = load()
        d[name] = {
            "repo_url": url,
            "dir": str(config.PROGRAMS_DIR / name),
            "start_command": req.start_command,
            "web_port": req.web_port,
            "monitor_command": (req.monitor_command or "").strip() or None,
            "token": (req.token or "").strip() or None,
            # Private until published: with a tunnel up, a public default would
            # expose a program to the internet the moment it finishes importing.
            "public": False,
            "ota": req.ota,
            "status": "importing",
            "created": datetime.datetime.now().isoformat(timespec="seconds"),
        }
        save(d)
    _imports[name] = {"phase": "cloning"}
    threading.Thread(target=import_worker, args=(name, url, req.start_command),
                     daemon=True, name=f"prog-import-{name}").start()


# ── Updates (OTA) ─────────────────────────────────────────────────────────────

def apply_update(name: str, prog: dict) -> tuple[bool, str]:
    """git pull --ff-only, reinstall declared deps, restart the unit. Shared by
    the manual Update button and unattended auto-update.

    --ff-only so an update can't force-reset the clone: local commits GitHub
    can't fast-forward over leave the program on the old code instead of
    losing them."""
    from harness import kiosk   # lazy — kiosk imports this module
    repo_dir = Path(prog["dir"])
    code, out = _run(["git", "-C", str(repo_dir), "pull", "--ff-only"], timeout=120,
                     env=git_env(prog))
    if code != 0:
        return False, f"git pull failed: {out[-300:]}"
    _imports.setdefault(name, {"phase": "installing"})
    err = install_deps(name, repo_dir)
    _imports.pop(name, None)
    if err:
        return False, err
    if prog.get("start_command"):
        _run(["systemctl", "restart", unit(name)], timeout=30)
        kiosk.kick(name)
    return True, out[-200:]


def run_auto_updates() -> list[str]:
    """Pull and restart every program on ota='auto' whose GitHub HEAD has moved.
    Returns the names updated. Runs on a timer so nobody has to click Update."""
    updated = []
    for name, prog in load().items():
        if prog.get("ota") != "auto" or prog.get("status") == "importing":
            continue
        try:
            local, remote = local_sha(prog), remote_sha(prog)
            if local and remote and local != remote and apply_update(name, prog)[0]:
                updated.append(name)
        except Exception:   # noqa: BLE001 - one bad program can't stall the rest
            continue
    return updated


# ── Removal ───────────────────────────────────────────────────────────────────

def remove(name: str, prog: dict) -> None:
    from harness import kiosk   # lazy — kiosk imports this module
    if kiosk.current() == name:
        kiosk.off()
    _run(["systemctl", "disable", "--now", unit(name)], timeout=30)
    try:
        (config.UNIT_DIR / f"{unit(name)}.service").unlink()
    except OSError:
        pass
    _run(["systemctl", "daemon-reload"], timeout=15)
    shutil.rmtree(prog["dir"], ignore_errors=True)
    for path in (env_file(name), harness_env_file(name)):
        try:
            path.unlink()   # secrets die with the program
        except OSError:
            pass
    from harness import auth
    auth.revoke_program_tokens(name)
    with _lock:
        d = load()
        d.pop(name, None)
        save(d)


# ── Public links ──────────────────────────────────────────────────────────────

def public_base() -> tuple[Optional[str], Optional[str]]:
    """(public https origin, via) for the /apps/<name>/ links.

    Order: an explicitly configured HARNESS_PUBLIC_URL, then a running Cloudflare
    tunnel, then Tailscale. Configuration wins because someone who set it meant
    it; the tunnel outranks Tailscale because it reaches the whole internet
    rather than one tailnet."""
    if config.PUBLIC_URL:
        url = config.PUBLIC_URL
        return (url if url.startswith("http") else f"https://{url}"), "configured"
    from harness import tunnel   # lazy — tunnel imports this module
    tunnel_url = tunnel.public_url()
    if tunnel_url:
        return tunnel_url, "cloudflare"
    code, out = _run(["tailscale", "status", "--json"], timeout=5)
    if code == 0:
        try:
            st = json.loads(out)
            if st.get("BackendState") == "Running":
                host = (st.get("Self", {}).get("DNSName") or "").rstrip(".") \
                    or next(iter(st.get("Self", {}).get("TailscaleIPs") or []), None)
                if host:
                    return f"https://{host}", "tailscale"
        except ValueError:
            pass
    return None, None


def listing() -> list[dict]:
    """Every program with its live status, links and settings, in the shape the
    UI renders. Tokens are never included, only whether one exists."""
    from harness import kiosk   # lazy — kiosk imports this module
    base, via = public_base()
    mon = kiosk.current()
    out = []
    with _lock:
        d = load()
    for name, prog in sorted(d.items()):
        importing = _imports.get(name)
        status = prog.get("status", "ready")
        if importing:
            status = "importing"
        elif status == "importing":
            status = "error"   # restarted mid-import, so the worker is gone
        elif status == "ready":
            status = unit_state(name)   # active | inactive | failed | unknown
        out.append({
            "name": name,
            "repo_url": prog["repo_url"],
            "start_command": prog.get("start_command"),
            "web_port": prog.get("web_port"),
            "public": prog.get("public", False),
            "ota": prog.get("ota", "github"),
            "status": status,
            "phase": importing.get("phase") if importing else None,
            "error": prog.get("error"),
            "created": prog.get("created"),
            "global_url": f"{base}/apps/{name}/" if base and prog.get("web_port") else None,
            "global_via": via if base and prog.get("web_port") else None,
            "on_monitor": name == mon,
            "monitor_command": prog.get("monitor_command"),
            "has_token": bool(prog.get("token")),
        })
    return out
