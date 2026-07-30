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

from harness import __version__, api, auth, config, kiosk, programs

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("harness")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Reapply the current unit templates to already-installed programs and to an
    # armed kiosk, so fixes land on restart without re-importing anything.
    kiosk.refresh()
    programs.refresh_units()

    async def _auto_update_loop():
        while True:
            await asyncio.sleep(config.AUTO_UPDATE_INTERVAL)
            try:
                names = await asyncio.to_thread(programs.run_auto_updates)
                if names:
                    log.info("auto-updated: %s", ", ".join(names))
            except Exception:   # noqa: BLE001 - a bad cycle can't kill the loop
                log.exception("auto-update cycle failed")

    task = asyncio.create_task(_auto_update_loop())
    try:
        yield
    finally:
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


@app.middleware("http")
async def _security_headers(request: Request, call_next):
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
            from fastapi.responses import JSONResponse
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
    # Never cache the UI, or a browser keeps stale JS after an update.
    p = request.url.path
    if p == "/" or p.startswith("/assets"):
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp


app.include_router(api.router)
app.include_router(api.proxy)   # /apps/<name>/ web access to programs


# ── Sign-in ───────────────────────────────────────────────────────────────────

def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _set_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        config.COOKIE_NAME, token,
        httponly=True, samesite="lax", secure=config.COOKIE_SECURE,
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
    return {"status": "ok", "username": req.username.strip(), "token": token}


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
    return {"status": "ok", "username": req.username, "token": token}


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
def change_password(req: PasswordRequest, user: str = Depends(auth.require_auth)):
    if not auth.verify_password(user, req.current_password):
        raise HTTPException(401, "Current password is wrong.")
    if len(req.new_password) < 8:
        raise HTTPException(400, "New password must be at least 8 characters.")
    auth.set_password(user, req.new_password)
    auth.invalidate_user(user)   # every session, this one included
    return {"status": "ok"}


# ── Web UI ────────────────────────────────────────────────────────────────────

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
