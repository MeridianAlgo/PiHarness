"""
Sign-in: one argon2-hashed credential file, in-memory sessions, and a per-IP
throttle on failed logins. Restarting the harness signs everyone out.
"""
import json
import os
import secrets
import time
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Cookie, Header, HTTPException

from harness import config

_ph = PasswordHasher()
# A real hash to verify against when the username is unknown, so a missing user
# takes the same time as a wrong password (no username enumeration via timing).
_DUMMY_HASH = _ph.hash("piharness-dummy")


# ── Credentials ───────────────────────────────────────────────────────────────

def _load() -> dict:
    try:
        return json.loads(config.CREDENTIALS_FILE.read_text())
    except Exception:   # noqa: BLE001 - missing or corrupt reads as "no users"
        return {}


def _save(data: dict) -> None:
    config.CREDENTIALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.CREDENTIALS_FILE.write_text(json.dumps(data))
    os.chmod(config.CREDENTIALS_FILE, 0o600)


def set_password(username: str, password: str) -> None:
    data = _load()
    data[username] = _ph.hash(password)
    _save(data)


def verify_password(username: str, password: str) -> bool:
    data = _load()
    if username not in data:
        try:   # spend the same time as a real verify, then fail
            _ph.verify(_DUMMY_HASH, password)
        except Exception:   # noqa: BLE001
            pass
        return False
    try:
        _ph.verify(data[username], password)
    except (VerifyMismatchError, Exception):
        return False
    # Transparently upgrade the stored hash if argon2's parameters have moved on.
    if _ph.check_needs_rehash(data[username]):
        set_password(username, password)
    return True


def has_any_user() -> bool:
    return bool(_load())


# ── Sessions ──────────────────────────────────────────────────────────────────

# token -> {"user": str, "exp": float}
_sessions: dict[str, dict] = {}


def create_session(username: str) -> str:
    now = time.time()
    for t in [t for t, s in _sessions.items() if s["exp"] <= now]:
        del _sessions[t]
    token = secrets.token_hex(32)
    _sessions[token] = {"user": username, "exp": now + config.SESSION_TTL_HOURS * 3600}
    return token


def validate_session(token: Optional[str]) -> Optional[str]:
    s = _sessions.get(token or "")
    if not s:
        return None
    if time.time() > s["exp"]:
        del _sessions[token]
        return None
    return s["user"]


def delete_session(token: Optional[str]) -> None:
    _sessions.pop(token or "", None)


def invalidate_user(username: str) -> None:
    """Drop every session for a user, after a password change."""
    for t in [t for t, s in _sessions.items() if s["user"] == username]:
        _sessions.pop(t, None)


# ── Login throttle ────────────────────────────────────────────────────────────
# Failed attempts per IP, with a lockout once enough pile up. In memory like the
# sessions, so a restart clears it. An attacker can't cause one.

MAX_ATTEMPTS = 8
LOCKOUT_SECONDS = 300
_attempts: dict[str, list] = {}   # ip -> [count, first_attempt_ts]


def throttle_check(ip: str) -> Optional[int]:
    """Seconds left in the lockout, or None when the IP may try again."""
    rec = _attempts.get(ip)
    if not rec:
        return None
    count, since = rec
    if count < MAX_ATTEMPTS:
        return None
    left = int(LOCKOUT_SECONDS - (time.time() - since))
    if left <= 0:
        _attempts.pop(ip, None)
        return None
    return left


def throttle_fail(ip: str) -> None:
    rec = _attempts.get(ip)
    if not rec or time.time() - rec[1] > LOCKOUT_SECONDS:
        _attempts[ip] = [1, time.time()]
    else:
        rec[0] += 1


def throttle_reset(ip: str) -> None:
    _attempts.pop(ip, None)


# ── Dependency ────────────────────────────────────────────────────────────────

def require_auth(
    harness_session: Optional[str] = Cookie(default=None),
    authorization: Optional[str] = Header(default=None),
) -> str:
    """The signed-in username, or 401. Takes either the browser's session
    cookie or `Authorization: Bearer <token>` for scripts."""
    token = harness_session
    if not token and authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
    if not token:
        raise HTTPException(401, "Not authenticated")
    user = validate_session(token)
    if not user:
        raise HTTPException(401, "Session expired or invalid")
    return user
