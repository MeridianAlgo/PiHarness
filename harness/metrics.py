"""
Host and per-program metrics, read straight from /proc, /sys and systemd.

No psutil: everything here is four files and one systemctl call, and a
dependency that ships compiled wheels is a poor trade on a Pi that may be
building from source. Every reader returns None rather than raising when the
file isn't there, so this module is inert on a dev box that isn't Linux.

A sampler thread keeps a short in-memory history so the dashboard can draw a
trend without a database. History dies with the process, which is the right
scope: this answers "is the Pi healthy right now", not "what happened
last Tuesday".
"""
import os
import shutil
import threading
import time
from typing import Optional

from harness import config, programs

# CPU percent needs two readings, so the previous /proc/stat total is kept here
# and the first sample after boot reports None rather than a made-up number.
_prev_cpu: Optional[tuple[int, int]] = None
_lock = threading.Lock()

# Ring buffers of (timestamp, value). Bounded, so memory can't creep.
HISTORY_POINTS = 120
_history: dict[str, list] = {"cpu": [], "temp": [], "memory": [], "disk": []}


def _read(path: str) -> Optional[str]:
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return None


# ── Host ──────────────────────────────────────────────────────────────────────

def cpu_percent() -> Optional[float]:
    """Busy percentage across all cores since the previous call."""
    global _prev_cpu
    raw = _read("/proc/stat")
    if not raw:
        return None
    for line in raw.splitlines():
        if not line.startswith("cpu "):
            continue
        parts = [int(x) for x in line.split()[1:]]
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0)   # idle + iowait
        total = sum(parts)
        prev = _prev_cpu
        _prev_cpu = (total, idle)
        if prev is None:
            return None
        d_total, d_idle = total - prev[0], idle - prev[1]
        if d_total <= 0:
            return None
        return round(100.0 * (d_total - d_idle) / d_total, 1)
    return None


def temperature() -> Optional[float]:
    """SoC temperature in °C. The Pi throttles at 80 and hard-caps at 85."""
    raw = _read("/sys/class/thermal/thermal_zone0/temp")
    if not raw or not raw.strip().lstrip("-").isdigit():
        return None
    return round(int(raw.strip()) / 1000.0, 1)


def memory() -> Optional[dict]:
    raw = _read("/proc/meminfo")
    if not raw:
        return None
    fields = {}
    for line in raw.splitlines():
        key, _, rest = line.partition(":")
        value = rest.strip().split(" ")[0]
        if value.isdigit():
            fields[key] = int(value) * 1024
    total = fields.get("MemTotal")
    # MemAvailable accounts for reclaimable cache; free alone reads alarmingly
    # low on Linux and would make every Pi look out of memory.
    available = fields.get("MemAvailable", fields.get("MemFree"))
    if not total or available is None:
        return None
    used = total - available
    return {"total": total, "used": used, "available": available,
            "percent": round(100.0 * used / total, 1)}


def disk() -> Optional[dict]:
    try:
        usage = shutil.disk_usage(str(config.PROGRAMS_DIR.anchor or "/"))
    except OSError:
        return None
    return {"total": usage.total, "used": usage.used, "free": usage.free,
            "percent": round(100.0 * usage.used / usage.total, 1) if usage.total else 0.0}


def uptime_seconds() -> Optional[float]:
    raw = _read("/proc/uptime")
    if not raw:
        return None
    try:
        return round(float(raw.split()[0]), 1)
    except (ValueError, IndexError):
        return None


def load_average() -> Optional[list]:
    try:
        return [round(x, 2) for x in os.getloadavg()]
    except (OSError, AttributeError):   # not Linux
        return None


def throttled() -> Optional[dict]:
    """Pi power/thermal flags from vcgencmd. The undervoltage bit is the usual
    cause of a Pi that behaves strangely under load, so it is worth surfacing
    even though nothing else here needs a Pi-only tool."""
    code, out = programs._run(["vcgencmd", "get_throttled"], timeout=5)
    if code != 0 or "=" not in out:
        return None
    try:
        bits = int(out.strip().split("=")[1], 16)
    except ValueError:
        return None
    return {
        "under_voltage_now": bool(bits & 0x1),
        "throttled_now": bool(bits & 0x4),
        "under_voltage_since_boot": bool(bits & 0x10000),
        "throttled_since_boot": bool(bits & 0x40000),
    }


def cpu_count() -> int:
    return os.cpu_count() or 1


def model() -> Optional[str]:
    raw = _read("/proc/device-tree/model") or _read("/sys/firmware/devicetree/base/model")
    return raw.strip("\x00").strip() if raw else None


# ── Per-program ───────────────────────────────────────────────────────────────

_SHOW_PROPS = ["MainPID", "MemoryCurrent", "CPUUsageNSec", "NRestarts",
               "ActiveEnterTimestampMonotonic", "ActiveState"]


def program_stats(unit_name: str) -> dict:
    """Memory, CPU time, restart count and uptime for one program's unit.
    Missing values come back as None; systemd reports properties it can't
    determine as the string [not set] or as 2**64-1."""
    code, out = programs._run(
        ["systemctl", "show", unit_name, "--property=" + ",".join(_SHOW_PROPS)], timeout=10)
    if code != 0:
        return {}
    raw = {}
    for line in out.splitlines():
        key, _, value = line.partition("=")
        raw[key] = value

    def number(key: str) -> Optional[int]:
        value = raw.get(key, "")
        if not value.isdigit():
            return None
        n = int(value)
        # systemd's "unknown" sentinel for unsigned properties.
        return None if n >= 2 ** 64 - 1 else n

    memory_bytes = number("MemoryCurrent")
    cpu_nsec = number("CPUUsageNSec")
    since = number("ActiveEnterTimestampMonotonic")
    up = None
    if since:
        boot = uptime_seconds()
        if boot is not None:
            up = round(max(0.0, boot - since / 1_000_000), 1)
    return {
        "pid": number("MainPID") or None,
        "memory": memory_bytes,
        "cpu_seconds": round(cpu_nsec / 1e9, 1) if cpu_nsec is not None else None,
        "restarts": number("NRestarts"),
        "uptime": up,
    }


# ── Snapshot and history ──────────────────────────────────────────────────────

def snapshot(with_cpu: bool = True) -> dict:
    """Everything the dashboard shows in one call. `with_cpu` is False for
    callers that must not disturb the sampler's /proc/stat delta."""
    return {
        "cpu_percent": cpu_percent() if with_cpu else _latest("cpu"),
        "cpu_count": cpu_count(),
        "temperature": temperature(),
        "memory": memory(),
        "disk": disk(),
        "uptime": uptime_seconds(),
        "load": load_average(),
        "model": model(),
    }


def _latest(series: str) -> Optional[float]:
    points = _history.get(series) or []
    return points[-1][1] if points else None


def record(sample: dict) -> None:
    now = time.time()
    values = {
        "cpu": sample.get("cpu_percent"),
        "temp": sample.get("temperature"),
        "memory": (sample.get("memory") or {}).get("percent"),
        "disk": (sample.get("disk") or {}).get("percent"),
    }
    with _lock:
        for key, value in values.items():
            if value is None:
                continue
            series = _history[key]
            series.append((round(now, 1), value))
            if len(series) > HISTORY_POINTS:
                del series[:-HISTORY_POINTS]


def history() -> dict:
    with _lock:
        return {key: list(points) for key, points in _history.items()}


def sample_once() -> dict:
    """Take a reading and fold it into the history. Called by the sampler loop,
    and once at startup so the first dashboard load isn't empty."""
    snap = snapshot()
    record(snap)
    return snap
