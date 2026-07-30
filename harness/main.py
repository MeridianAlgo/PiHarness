"""The FastAPI application: sign-in, the programs API, the /apps proxy, the web
UI, and the background loop that applies unattended updates."""
import asyncio
import logging
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from harness import __version__, api, auth, config, kiosk, metrics, programs, tunnel

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("harness")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Reapply the current unit templates to already-installed programs, an armed
    # kiosk and an enabled tunnel, so fixes land on restart without any
    # re-importing or re-configuring.
    kiosk.refresh()
    programs.refresh_units()
    await asyncio.to_thread(tunnel.refresh)

    async def _auto_update_loop():
        while True:
            await asyncio.sleep(config.AUTO_UPDATE_INTERVAL)
            try:
                names = await asyncio.to_thread(programs.run_auto_updates)
                if names:
                    log.info("auto-updated: %s", ", ".join(names))
            except Exception:   # noqa: BLE001 - a bad cycle can't kill the loop
                log.exception("auto-update cycle failed")

    async def _metrics_loop():
        # One sampler, one owner of the /proc/stat delta. Every reader gets the
        # history this writes rather than sampling for itself.
        while True:
            try:
                await asyncio.to_thread(metrics.sample_once)
            except Exception:   # noqa: BLE001 - never let a bad read stop sampling
                log.exception("metrics sample failed")
            await asyncio.sleep(config.METRICS_INTERVAL)

    tasks = [asyncio.create_task(_auto_update_loop()),
             asyncio.create_task(_metrics_loop())]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()


app = FastAPI(
    title="PiHarness",
    description="Import programs from GitHub and keep them running on a Raspberry Pi",
    version=__version__,
    lifespan=lifespan,
)

if config.CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# Hosts allowed to make cookie-authenticated, state-changing requests (CSRF guard).
_ALLOWED_ORIGIN_HOSTS = {urlparse(o).netloc for o in config.CORS_ORIGINS}


def _client_addr(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    from fastapi.responses import JSONResponse
    path = request.url.path

    # Body cap, checked before the body is read into memory. A missing or lying
    # Content-Length still can't do damage: the endpoints that accept large
    # input enforce their own limits.
    try:
        if int(request.headers.get("content-length") or 0) > config.MAX_BODY_BYTES:
            return JSONResponse({"detail": "Request body too large"}, status_code=413)
    except ValueError:
        return JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)

    # Rate limit. The proxied programs under /apps are deliberately exempt:
    # they serve their own assets, and a page with thirty images is normal.
    if path.startswith("/api"):
        is_login = path in ("/api/login", "/api/setup")
        limit = config.RATE_LIMIT_LOGIN if is_login else config.RATE_LIMIT
        retry = auth.rate_limit(_client_addr(request) + (":login" if is_login else ""), limit)
        if retry:
            return JSONResponse({"detail": f"Too many requests. Try again in {retry}s."},
                                status_code=429, headers={"Retry-After": str(retry)})

    # CSRF: a cross-site page can't read our token, but it can ride the session
    # cookie on a state-changing request. Reject those unless the Origin is us
    # (or an allowed dev origin). Bearer-token callers carry no cookie and set
    # Origin themselves, so they're unaffected; cookie-less requests have no
    # session to abuse and pass through.
    if request.method in ("POST", "PUT", "PATCH", "DELETE") \
            and request.cookies.get(config.COOKIE_NAME) \
            and not request.headers.get("authorization", "").startswith("Bearer "):
        origin = request.headers.get("origin") or request.headers.get("referer")
        host = urlparse(origin).netloc if origin else ""
        if host and host != request.url.netloc and host not in _ALLOWED_ORIGIN_HOSTS:
            return JSONResponse({"detail": "Cross-origin request blocked"}, status_code=403)

    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'")
    # Reachable from the internet means the browser should refuse to speak plain
    # HTTP to us at all. Not set otherwise: pinning HSTS on a LAN hostname that
    # has no certificate would lock the user out of their own Pi.
    if _public_facing():
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    # Never cache the UI, or a browser keeps stale JS after an update.
    if path == "/" or path.startswith("/assets"):
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp


# Asking systemd whether the tunnel is up on every single request would be a
# subprocess per request. The answer changes only when the tunnel is switched,
# so it is cached briefly.
_public_cache = [0.0, False]


def _public_facing() -> bool:
    import time
    now = time.monotonic()
    if now - _public_cache[0] > 30:
        _public_cache[0] = now
        try:
            _public_cache[1] = bool(config.PUBLIC_URL) or tunnel.is_active()
        except Exception:   # noqa: BLE001 - never fail a response over this
            _public_cache[1] = bool(config.PUBLIC_URL)
    return _public_cache[1]


app.include_router(api.router)
app.include_router(api.system)  # /api/metrics, /api/tunnel, /api/tokens, /api/prompt
app.include_router(api.proxy)   # /apps/<name>/ web access to programs


# ── Sign-in ───────────────────────────────────────────────────────────────────

def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _set_cookie(response: Response, token: str) -> None:
    # Secure is forced on whenever the harness is reachable from the internet,
    # so the session cookie can't be sent in the clear even if someone reaches
    # the Pi over plain HTTP at the same time.
    response.set_cookie(
        config.COOKIE_NAME, token,
        httponly=True, samesite="lax",
        secure=config.COOKIE_SECURE or _public_facing(),
        max_age=config.SESSION_TTL_HOURS * 3600, path="/")


class LoginRequest(BaseModel):
    username: str
    password: str


@app.get("/api/status")
def status():
    """Unauthenticated. Enough for the UI to choose between the setup screen and
    the sign-in screen."""
    return {"version": __version__, "setup_required": not auth.has_any_user()}


@app.post("/api/setup")
def setup(req: LoginRequest, request: Request, response: Response):
    """First run only. Creates the single admin account, then closes for good so
    it can't be used to add a second one."""
    if auth.has_any_user():
        raise HTTPException(409, "Already set up. Sign in instead.")
    if len(req.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters.")
    if not req.username.strip():
        raise HTTPException(400, "Pick a username.")
    auth.set_password(req.username.strip(), req.password)
    token = auth.create_session(req.username.strip())
    _set_cookie(response, token)
    log.info("initial account created from %s", _client_ip(request))
    # The session token is not returned. It is the cookie's value, and handing
    # it to the caller as an API credential is what made sessions unrevocable.
    # Scripts create an API token instead: POST /api/tokens.
    return {"status": "ok", "username": req.username.strip()}


@app.post("/api/login")
def login(req: LoginRequest, request: Request, response: Response):
    ip = _client_ip(request)
    left = auth.throttle_check(ip)
    if left:
        raise HTTPException(429, f"Too many failed attempts. Try again in {left}s.")
    if not auth.verify_password(req.username, req.password):
        auth.throttle_fail(ip)
        log.warning("failed sign-in for %r from %s", req.username, ip)
        raise HTTPException(401, "Invalid username or password")
    auth.throttle_reset(ip)
    token = auth.create_session(req.username)
    _set_cookie(response, token)
    return {"status": "ok", "username": req.username}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    auth.delete_session(request.cookies.get(config.COOKIE_NAME))
    response.delete_cookie(config.COOKIE_NAME, path="/")
    return {"status": "ok"}


@app.get("/api/me")
def me(user: str = Depends(auth.require_auth)):
    return {"username": user, "version": __version__}


class PasswordRequest(BaseModel):
    current_password: str
    new_password: str


@app.post("/api/password")
def change_password(req: PasswordRequest, user: str = Depends(auth.require_session)):
    if not auth.verify_password(user, req.current_password):
        raise HTTPException(401, "Current password is wrong.")
    if len(req.new_password) < 8:
        raise HTTPException(400, "New password must be at least 8 characters.")
    auth.set_password(user, req.new_password)
    auth.invalidate_user(user)   # every session, this one included
    return {"status": "ok"}


# ── Web UI ────────────────────────────────────────────────────────────────────

@app.get("/agent/piharness_mcp.py", include_in_schema=False)
def mcp_server_file():
    """Serve the MCP server so it can be fetched straight from the Pi. It is
    plain source with no secrets in it, so no auth is needed — and it runs on
    the agent's machine, not here."""
    path = config.AGENT_DIR / "piharness_mcp.py"
    if not path.exists():
        raise HTTPException(404, "MCP server file not installed")
    return FileResponse(path, media_type="text/x-python",
                        filename="piharness_mcp.py")


_INDEX = config.UI_DIR / "index.html"

if (config.UI_DIR / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=config.UI_DIR / "assets"), name="assets")


@app.get("/", include_in_schema=False)
def index():
    if not _INDEX.exists():
        return {"name": "PiHarness", "version": __version__, "ui": "not installed"}
    return FileResponse(_INDEX)


def run() -> None:
    """`python -m harness.main`, for a dev run. Production goes through uvicorn
    in the systemd unit."""
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=config.PORT, log_level="info")


if __name__ == "__main__":
    run()
