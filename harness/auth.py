"""
Sign-in: one argon2-hashed credential file, in-memory sessions, revocable API
tokens for scripts, and a per-IP throttle on failed logins. Restarting the
harness signs everyone out of the browser but leaves API tokens working.
"""
import hashlib
import json
import os
import secrets
import time
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Cookie, Depends, Header, HTTPException, Request

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


# ── API tokens ────────────────────────────────────────────────────────────────
# Scripts used to authenticate by replaying the session token handed back by
# /api/login. That token is also the browser's cookie value, so it could not be
# revoked without signing the browser out, it inherited the session's expiry,
# and it was stored in plaintext by whatever called it. These are separate:
# independently revocable, no expiry, and only a hash is kept on the Pi.
#
# sha256 rather than argon2 on purpose. Argon2 exists to make low-entropy
# passwords expensive to guess; these carry 256 bits of randomness, so there is
# nothing to brute force and a slow hash would only add latency to every request.

TOKEN_PREFIX = "phk_"

# What a token is allowed to do. Agents made this worth having: a chatbot that
# only needs to answer "why is this program failing" should not also be able to
# delete it.
#
# "program" is not one you can ask for. The harness mints one per program and
# hands it to that program as HARNESS_TOKEN, so a program can save a secret it
# rotated at runtime. It is bound to its own program and reaches exactly one
# endpoint; see require_owner below.
USER_SCOPES = ("read", "full")
TOKEN_SCOPES = (*USER_SCOPES, "program")
READ_METHODS = ("GET", "HEAD", "OPTIONS")


def _token_file():
    return config.CONFIG_DIR / "api-tokens.json"


def _load_tokens() -> dict:
    try:
        return json.loads(_token_file().read_text())
    except Exception:   # noqa: BLE001 - missing or corrupt reads as "none issued"
        return {}


def _save_tokens(data: dict) -> None:
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _token_file().write_text(json.dumps(data, indent=2))
    os.chmod(_token_file(), 0o600)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_api_token(username: str, label: str, scope: str = "full",
                     program: Optional[str] = None) -> str:
    """Mint a token and return it in the clear. This is the only time it can be
    read; only its hash is stored.

    `program` binds a scope="program" token to the one program it belongs to."""
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    data = _load_tokens()
    data[_hash_token(token)] = {
        "label": label[:60] or "unnamed",
        "user": username,
        "scope": scope if scope in TOKEN_SCOPES else "full",
        "program": program,
        "created": time.time(),
        "last_used": None,
    }
    _save_tokens(data)
    return token


def list_api_tokens() -> list[dict]:
    """Metadata only. The tokens themselves are not recoverable."""
    return sorted(
        ({"id": h[:12], "label": v.get("label"), "scope": v.get("scope", "full"),
          "program": v.get("program"),
          "created": v.get("created"), "last_used": v.get("last_used")}
         for h, v in _load_tokens().items()),
        key=lambda t: t.get("created") or 0, reverse=True)


def revoke_api_token(token_id: str) -> bool:
    data = _load_tokens()
    for h in list(data):
        if h.startswith(token_id):
            del data[h]
            _save_tokens(data)
            return True
    return False


def revoke_program_tokens(name: str) -> None:
    """Drop the token issued to a program. Called when it is removed, so a
    deleted program's credential doesn't outlive it."""
    data = _load_tokens()
    doomed = [h for h, v in data.items() if v.get("program") == name]
    if not doomed:
        return
    for h in doomed:
        del data[h]
    _save_tokens(data)


def api_token_record(token: Optional[str]) -> Optional[dict]:
    """The stored record behind a token, or None. Same lookup as
    validate_api_token, but the caller can see the scope."""
    if not token or not token.startswith(TOKEN_PREFIX):
        return None
    data = _load_tokens()
    digest = _hash_token(token)
    # Compare against every stored hash in constant time. The set is tiny, and
    # a dict lookup on a hash of a secret is already not a timing oracle worth
    # worrying about — this just removes the question.
    matched = None
    for stored, record in data.items():
        if secrets.compare_digest(stored, digest):
            matched = (stored, record)
    if not matched:
        return None
    stored, record = matched
    record["last_used"] = time.time()
    data[stored] = record
    _save_tokens(data)
    return record


def validate_api_token(token: Optional[str], method: Optional[str] = None) -> Optional[str]:
    """The user behind a token, or None. Pass a request method to hold a
    read-scoped token to reads."""
    record = api_token_record(token)
    if not record:
        return None
    if record.get("scope") == "read" and method and method not in READ_METHODS:
        return None
    return record.get("user")


# ── Request rate limit ────────────────────────────────────────────────────────
# The login throttle above only guards the password. This guards everything
# else: a valid token that leaks, or an authenticated client stuck in a retry
# loop, should not be able to hammer git and systemctl without limit.

_requests: dict[str, list] = {}   # ip -> [count, window_start]


def rate_limit(ip: str, limit: int, window: int = 60) -> Optional[int]:
    """None when the request may proceed, else seconds until the window resets."""
    now = time.time()
    rec = _requests.get(ip)
    if not rec or now - rec[1] >= window:
        _requests[ip] = [1, now]
        # Opportunistically drop stale entries so this can't grow without bound
        # when traffic comes from many addresses.
        if len(_requests) > 2048:
            for k in [k for k, v in _requests.items() if now - v[1] >= window]:
                _requests.pop(k, None)
        return None
    rec[0] += 1
    if rec[0] > limit:
        return max(1, int(window - (now - rec[1])))
    return None


# ── Dependencies ──────────────────────────────────────────────────────────────

def require_auth(
    request: Request,
    harness_session: Optional[str] = Cookie(default=None),
    authorization: Optional[str] = Header(default=None),
) -> str:
    """The signed-in username, or 401. Takes the browser's session cookie, or
    `Authorization: Bearer phk_…` for scripts.

    Records how the caller authenticated on request.state, so require_session
    below can turn away a token holder without every route having to care."""
    request.state.via_token = False
    request.state.token_scope = None

    bearer = None
    if authorization and authorization.startswith("Bearer "):
        bearer = authorization[7:].strip()
    if bearer:
        record = api_token_record(bearer)
        if record:
            scope = record.get("scope", "full")
            if scope == "program":
                # A program's own token. It reaches its own secrets and nothing
                # else, so it never gets past this general-purpose dependency.
                raise HTTPException(
                    403, f"This is the token belonging to program "
                         f"'{record.get('program')}'. It can only PATCH that "
                         f"program's secrets.")
            if scope == "read" and request.method not in READ_METHODS:
                raise HTTPException(
                    403, f"Token '{record.get('label')}' is read-only. Create a "
                         f"token with scope 'full' to change anything.")
            request.state.via_token = True
            request.state.token_scope = scope
            return record.get("user")
        # A session token presented as a bearer is no longer accepted: tokens
        # and sessions are separate credentials now. Say so, rather than 401ing
        # a script whose author has no way to guess why.
        if validate_session(bearer):
            raise HTTPException(
                401, "Session tokens are no longer valid for the API. "
                     "Create an API token in Settings and send that instead.")
        raise HTTPException(401, "Invalid API token")
    if not harness_session:
        raise HTTPException(401, "Not authenticated")
    user = validate_session(harness_session)
    if not user:
        raise HTTPException(401, "Session expired or invalid")
    return user


def require_owner(
    name: str,
    request: Request,
    harness_session: Optional[str] = Cookie(default=None),
    authorization: Optional[str] = Header(default=None),
) -> str:
    """A signed-in session, a full token, or the token belonging to `name`
    itself.

    This is what lets a program save a credential it rotated — a refreshed
    OAuth token that would otherwise be lost on the next restart — without
    handing it a key to every other program on the Pi. `name` comes from the
    path, so the binding is checked against the program actually being written."""
    bearer = None
    if authorization and authorization.startswith("Bearer "):
        bearer = authorization[7:].strip()
    if bearer:
        record = api_token_record(bearer)
        if record and record.get("scope") == "program":
            if record.get("program") != name:
                raise HTTPException(
                    403, f"This token belongs to program "
                         f"'{record.get('program')}', not '{name}'.")
            request.state.via_token = True
            request.state.token_scope = "program"
            return record.get("user")
    return require_auth(request, harness_session, authorization)


def require_session(request: Request, user: str = Depends(require_auth)) -> str:
    """Like require_auth, but a token is not enough. Guards the operations a
    token must not reach: minting tokens, reading secret values, and changing
    the password. Otherwise a token would be able to widen its own scope, or
    quietly hand over every credential on the Pi."""
    if getattr(request.state, "via_token", False):
        raise HTTPException(
            403, "This needs a signed-in session. An API token can't be used here.")
    return user
