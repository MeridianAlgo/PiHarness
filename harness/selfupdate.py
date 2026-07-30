"""OTA for the harness itself, from GitHub — the same deal its programs get.

Kept out of programs.py because the two are not the same operation. A program
is a clone the harness supervises and can restart at will; this is the clone the
harness is *running out of*, and applying an update means restarting the process
doing the applying. Hence systemd-run below.

No HTTP in here. Routes are in api.py.
"""
import os
import shutil
from pathlib import Path
from typing import Optional

from harness import __version__, programs

# The install itself: /opt/piharness in a real install, the repo on a dev box.
ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "installer" / "update.sh"
BRANCH = os.environ.get("HARNESS_BRANCH", "main")
# Transient unit the updater runs as. Named, so its output is one journalctl away.
UNIT = "piharness-update"


class SelfUpdateError(RuntimeError):
    """Something the caller can act on: no git checkout, no updater, no systemd."""


def _git(*args: str, timeout: int = 30) -> tuple[int, str]:
    return programs._run(["git", "-C", str(ROOT), *args], timeout=timeout)


def _version_at(ref: str) -> Optional[str]:
    code, out = _git("show", f"{ref}:VERSION")
    return out.strip() or None if code == 0 else None


def check() -> dict:
    """Where this install sits against GitHub.

    Fetches, so it costs a network round trip — which is why nothing calls it on
    a timer the way program checks are. It is asked for, not polled."""
    code, local = _git("rev-parse", "HEAD")
    if code != 0:
        return {"version": __version__, "local": None, "remote": None,
                "update_available": False, "branch": BRANCH,
                "error": "Not a git install, so there is nothing to pull. "
                         "Reinstall with installer/install.sh to get updates."}

    code, out = _git("fetch", "origin", BRANCH, "--quiet", timeout=60)
    if code != 0:
        return {"version": __version__, "local": local[:8], "remote": None,
                "update_available": False, "branch": BRANCH,
                "error": f"Cannot reach GitHub: {out[-200:]}"}

    code, remote = _git("rev-parse", f"origin/{BRANCH}")
    if code != 0:
        return {"version": __version__, "local": local[:8], "remote": None,
                "update_available": False, "branch": BRANCH,
                "error": f"No branch '{BRANCH}' on origin."}

    return {"version": __version__,
            "local": local[:8],
            "remote": remote[:8],
            "remote_version": _version_at(f"origin/{BRANCH}"),
            "branch": BRANCH,
            "update_available": local != remote,
            "error": None}


def apply() -> dict:
    """Hand installer/update.sh to systemd as a transient unit, and let go.

    It cannot run as a child of this process. The script ends in
    `systemctl restart piharness`, and a child lives in the harness's own cgroup
    — systemd would kill the updater along with the service it was restarting,
    leaving the install half-applied. systemd-run puts it in a cgroup of its
    own, so it outlives the restart it causes.

    Returns as soon as the updater has started. It is not finished: the harness
    is about to go down and come back on the new code, which is also why nothing
    useful can be returned about the outcome. Read logs() afterwards."""
    if not SCRIPT.exists():
        raise SelfUpdateError(
            f"{SCRIPT} is missing, so this isn't a git install. Nothing to update.")
    if not shutil.which("systemd-run"):
        raise SelfUpdateError(
            f"systemd-run isn't available here. Run `sudo {SCRIPT}` on the Pi instead.")
    code, out = programs._run(
        ["systemd-run", "--unit", UNIT, "--collect",
         "--description=PiHarness self-update",
         "/bin/bash", str(SCRIPT), "--auto"],
        timeout=30)
    if code != 0:
        raise SelfUpdateError(f"Could not start the updater: {out[-200:]}")
    return {"status": "started", "unit": UNIT,
            "detail": "Updating. The harness restarts when it finishes — "
                      "reload this page in about a minute."}


def logs(lines: int = 80) -> str:
    code, out = programs._run(
        ["journalctl", "-u", UNIT, "--no-pager", "-n", str(min(lines, 400))],
        timeout=10)
    return out if code == 0 and out else "No update has run yet."
