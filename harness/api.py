"""HTTP surface: /api/programs/… plus the /apps/<name>/ reverse proxy that gives
a program's web UI a link from outside the LAN."""
import os
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from harness import config, kiosk, metrics, programs, prompt, tunnel
from harness.auth import require_auth, require_session, validate_session

router = APIRouter(prefix="/api/programs", tags=["programs"])
proxy = APIRouter(tags=["programs"])
system = APIRouter(prefix="/api", tags=["system"])


def _prog(name: str) -> dict:
    if not programs.NAME_RE.fullmatch(name):
        raise HTTPException(400, "Invalid program name")
    prog = programs.load().get(name)
    if prog is None:
        raise HTTPException(404, "Program not found")
    return prog


def _clean_port(port: Optional[int]) -> Optional[int]:
    if port is None:
        return None
    if not (1024 <= port <= 65535) or port == config.PORT:
        raise HTTPException(400, f"Web port must be 1024-65535, and not "
                                 f"{config.PORT}, which is the harness itself.")
    return port


# ── Listing ───────────────────────────────────────────────────────────────────

@router.get("")
def list_programs(_: str = Depends(require_auth)):
    return {"programs": programs.listing(),
            "monitor": {"connected": kiosk.monitor_connected(), "program": kiosk.current()}}


@router.get("/updates")
def check_updates(_: str = Depends(require_auth)):
    """Compare each program's local commit with its GitHub HEAD. Self-managed
    programs are skipped, since their own updater is in charge."""
    result = {}
    for name, prog in programs.load().items():
        if prog.get("ota", "github") == "self" or prog.get("status") == "importing":
            continue
        local = programs.local_sha(prog)
        remote = programs.remote_sha(prog)
        if not local or not remote:
            continue
        result[name] = {"update_available": local != remote,
                        "local": local[:8], "remote": remote[:8]}
    return {"updates": result}


# ── Import ────────────────────────────────────────────────────────────────────

class AddProgramRequest(BaseModel):
    repo_url: str
    name: Optional[str] = None
    start_command: Optional[str] = None
    web_port: Optional[int] = None
    monitor_command: Optional[str] = None   # runs each time it goes on the monitor
    token: Optional[str] = None             # GitHub access token for private repos
    ota: str = "github"                     # one of programs.OTA_MODES


@router.post("")
def add_program(req: AddProgramRequest, _: str = Depends(require_auth)):
    url = req.repo_url.strip()
    if programs.SHORT_RE.fullmatch(url):
        url = f"https://github.com/{url}"
    if not programs.REPO_RE.fullmatch(url):
        raise HTTPException(400, "Enter a GitHub repository URL (https://github.com/owner/repo).")
    url = url.rstrip("/")

    name = (req.name or url.rsplit("/", 1)[-1].removesuffix(".git")).lower()
    name = re.sub(r"[^a-z0-9._-]", "-", name).strip("-.")
    if not programs.NAME_RE.fullmatch(name):
        raise HTTPException(400, "Program name must be 1–41 chars: letters, digits, dot, dash.")

    req.web_port = _clean_port(req.web_port)
    if req.ota not in programs.OTA_MODES:
        raise HTTPException(400, f"ota must be one of {', '.join(programs.OTA_MODES)}")
    token = (req.token or "").strip() or None
    if token and not programs.TOKEN_RE.fullmatch(token):
        raise HTTPException(400, "That doesn't look like a GitHub access token.")

    if name in programs.load():
        raise HTTPException(409, f"A program named '{name}' already exists.")
    programs.start_import(name, url, req)
    return {"status": "importing", "name": name}


# ── Lifecycle ─────────────────────────────────────────────────────────────────

class ActionRequest(BaseModel):
    action: str   # start | stop | restart


@router.post("/{name}/action")
def program_action(name: str, req: ActionRequest, _: str = Depends(require_auth)):
    prog = _prog(name)
    if req.action not in ("start", "stop", "restart"):
        raise HTTPException(400, "action must be start, stop or restart")
    if not prog.get("start_command"):
        raise HTTPException(409, "Set a start command first.")
    verb = {"start": ["enable", "--now"], "stop": ["disable", "--now"],
            "restart": ["restart"]}[req.action]
    code, out = programs._run(["systemctl", *verb, programs.unit(name)], timeout=30)
    if code > 0:
        raise HTTPException(500, f"systemctl {req.action} failed: {out[-300:]}")
    if req.action in ("start", "restart"):
        kiosk.kick(name)
    return {"status": programs.unit_state(name)}


class EditProgramRequest(BaseModel):
    start_command: Optional[str] = None
    web_port: Optional[int] = None
    monitor_command: Optional[str] = None   # empty string clears it
    token: Optional[str] = None             # empty string clears it
    public: Optional[bool] = None
    ota: Optional[str] = None
    clear_port: bool = False


@router.put("/{name}")
def edit_program(name: str, req: EditProgramRequest, _: str = Depends(require_auth)):
    _prog(name)
    with programs._lock:
        d = programs.load()
        prog = d[name]
        if req.start_command is not None:
            prog["start_command"] = req.start_command.strip() or None
            if prog["start_command"] and prog.get("status") == "needs_command":
                prog["status"] = "ready"
        if req.monitor_command is not None:
            prog["monitor_command"] = req.monitor_command.strip() or None
        if req.token is not None:
            t = req.token.strip() or None
            if t and not programs.TOKEN_RE.fullmatch(t):
                raise HTTPException(400, "That doesn't look like a GitHub access token.")
            prog["token"] = t
        if req.clear_port:
            prog["web_port"] = None
        elif req.web_port is not None:
            prog["web_port"] = _clean_port(req.web_port)
        if req.public is not None:
            prog["public"] = bool(req.public)
        if req.ota is not None:
            if req.ota not in programs.OTA_MODES:
                raise HTTPException(400, f"ota must be one of {', '.join(programs.OTA_MODES)}")
            prog["ota"] = req.ota
        programs.save(d)

    if prog.get("start_command"):
        programs.write_unit(name, prog)
        if programs.unit_state(name) == "active":
            programs._run(["systemctl", "restart", programs.unit(name)], timeout=30)
    # The kiosk unit hardcodes the port and monitor command, so keep it in step.
    if kiosk.current() == name:
        if prog.get("web_port"):
            kiosk.show(name, prog)
        else:
            kiosk.off()
    return {"status": "ok"}


@router.post("/{name}/update")
def update_program(name: str, _: str = Depends(require_auth)):
    """git pull the latest, reinstall declared deps, restart the unit."""
    ok, detail = programs.apply_update(name, _prog(name))
    if not ok:
        raise HTTPException(500, detail)
    return {"status": "updated", "detail": detail}


@router.delete("/{name}")
def remove_program(name: str, _: str = Depends(require_auth)):
    programs.remove(name, _prog(name))
    return {"status": "removed"}


@router.get("/{name}/logs")
def program_logs(name: str, lines: int = 80, _: str = Depends(require_auth)):
    _prog(name)
    code, out = programs._run(
        ["journalctl", "-u", programs.unit(name), "--no-pager", "-n", str(min(lines, 400))],
        timeout=10)
    return {"logs": out if code == 0 else "No logs available."}


# ── Monitor ───────────────────────────────────────────────────────────────────

class MonitorRequest(BaseModel):
    on: bool


@router.post("/{name}/monitor")
def program_monitor(name: str, req: MonitorRequest, _: str = Depends(require_auth)):
    """Show the program's web UI fullscreen on the Pi's attached monitor."""
    prog = _prog(name)
    if not req.on:
        kiosk.off()
        return {"status": "off"}
    if not prog.get("web_port"):
        raise HTTPException(409, "Set a web port first. The monitor shows the program's web UI.")
    # No monitor right now is fine: the kiosk stays armed and displays as soon
    # as one is plugged in. Detection can also miss some display setups.
    try:
        kiosk.show(name, prog)
    except kiosk.KioskError as exc:
        raise HTTPException(409, str(exc))
    return {"status": "on", "program": name, "connected": kiosk.monitor_connected()}


# ── Secrets ───────────────────────────────────────────────────────────────────
# KEY=VALUE lines stored at ENV_DIR/<name>.env (mode 0600, root-only) and handed
# to the program as environment variables at start. GitHub repository secrets
# never leave GitHub, so this is the on-Pi equivalent.
#
# Reading values back takes a signed-in session. A token can write secrets and
# see which names exist, but never pull the values out, so a token that leaks
# doesn't hand over every credential on the Pi with it.

_ENV_LINE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")


def _env_text(name: str) -> str:
    try:
        return programs.env_file(name).read_text()
    except OSError:
        return ""


@router.get("/{name}/secrets")
def get_secrets(name: str, _: str = Depends(require_session)):
    _prog(name)
    return {"env": _env_text(name)}


@router.get("/{name}/secret-names")
def get_secret_names(name: str, _: str = Depends(require_auth)):
    """The KEY names only, no values. Safe for a token, and enough for an agent
    to tell whether the variable a program needs has been set."""
    _prog(name)
    names = [line.split("=", 1)[0].strip()
             for line in _env_text(name).splitlines()
             if line.strip() and not line.strip().startswith("#") and "=" in line]
    return {"names": names}


class SecretsRequest(BaseModel):
    env: str   # KEY=VALUE per line; blank lines and #comments allowed


@router.put("/{name}/secrets")
def put_secrets(name: str, req: SecretsRequest, _: str = Depends(require_auth)):
    prog = _prog(name)
    if len(req.env) > 32_000:
        raise HTTPException(413, "Secrets too large (32 KB max)")
    for i, line in enumerate(req.env.splitlines(), 1):
        line = line.strip()
        if line and not line.startswith("#") and not _ENV_LINE_RE.fullmatch(line):
            raise HTTPException(400, f"Line {i} isn't KEY=VALUE (keys: letters, digits, underscore).")
    path = programs.env_file(name)
    if req.env.strip():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(req.env if req.env.endswith("\n") else req.env + "\n")
            os.chmod(path, 0o600)
        except OSError as exc:
            raise HTTPException(500, f"Could not save secrets: {exc}")
    else:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    # A running program only sees new env on restart.
    if prog.get("start_command") and programs.unit_state(name) == "active":
        programs._run(["systemctl", "restart", programs.unit(name)], timeout=30)
        return {"status": "saved", "restarted": True}
    return {"status": "saved", "restarted": False}


# ── Dashboard metrics ─────────────────────────────────────────────────────────

@system.get("/metrics")
def get_metrics(_: str = Depends(require_auth)):
    """Host health plus per-program resource use, and the sampled history the
    dashboard draws as sparklines.

    The host snapshot reuses the sampler's most recent CPU reading rather than
    taking its own: CPU percent is a delta between two /proc/stat reads, and a
    second reader would race the sampler and hand both of them nonsense."""
    snap = metrics.snapshot(with_cpu=False)
    per_program = {}
    for name, prog in programs.load().items():
        if prog.get("status") == "importing":
            continue
        per_program[name] = metrics.program_stats(programs.unit(name))
    return {
        "host": snap,
        "throttled": metrics.throttled(),
        "history": metrics.history(),
        "interval": config.METRICS_INTERVAL,
        "programs": per_program,
    }


# ── Cloudflare tunnel ─────────────────────────────────────────────────────────

@system.get("/tunnel")
def get_tunnel(_: str = Depends(require_auth)):
    return tunnel.status()


class TunnelRequest(BaseModel):
    mode: str = "quick"                 # quick | named
    token: Optional[str] = None         # named mode: Cloudflare connector token
    hostname: Optional[str] = None      # named mode: the hostname it serves


@system.post("/tunnel")
def start_tunnel(req: TunnelRequest, _: str = Depends(require_auth)):
    try:
        return tunnel.enable(req.mode, req.token, req.hostname)
    except tunnel.TunnelError as exc:
        raise HTTPException(409, str(exc))


@system.delete("/tunnel")
def stop_tunnel(_: str = Depends(require_auth)):
    return tunnel.disable()


@system.get("/tunnel/logs")
def tunnel_logs(lines: int = 80, _: str = Depends(require_auth)):
    return {"logs": tunnel.logs(lines)}


# ── API tokens ────────────────────────────────────────────────────────────────

@system.get("/tokens")
def get_tokens(_: str = Depends(require_session)):
    from harness import auth
    return {"tokens": auth.list_api_tokens()}


class TokenRequest(BaseModel):
    label: str = "script"
    scope: str = "full"   # one of auth.TOKEN_SCOPES


@system.post("/tokens")
def create_token(req: TokenRequest, user: str = Depends(require_session)):
    """Returns the token once. It is stored only as a hash, so it cannot be
    shown again — a lost token is revoked and replaced, not recovered."""
    from harness import auth
    if req.scope not in auth.TOKEN_SCOPES:
        raise HTTPException(400, f"scope must be one of {', '.join(auth.TOKEN_SCOPES)}")
    return {"token": auth.create_api_token(user, req.label.strip(), req.scope),
            "label": req.label.strip() or "unnamed",
            "scope": req.scope}


@system.delete("/tokens/{token_id}")
def delete_token(token_id: str, _: str = Depends(require_session)):
    from harness import auth
    if not auth.revoke_api_token(token_id):
        raise HTTPException(404, "No such token")
    return {"status": "revoked"}


# ── Agent access ──────────────────────────────────────────────────────────────

@system.get("/agent")
def agent_descriptor(request: Request):
    """Unauthenticated on purpose, like /api/prompt: it says how to talk to this
    harness and nothing about what is on it. Point an agent here and it can work
    out the rest."""
    from harness import __version__, auth
    base = str(request.base_url).rstrip("/")
    return {
        "name": "piharness",
        "version": __version__,
        "description": "Runs programs from GitHub on a Raspberry Pi and keeps them running.",
        "base_url": base,
        "openapi": f"{base}/openapi.json",
        "docs": f"{base}/docs",
        "program_spec": f"{base}/api/prompt",
        "authentication": {
            "scheme": "bearer",
            "header": "Authorization: Bearer <token>",
            "token_prefix": auth.TOKEN_PREFIX,
            "how_to_get_one": "Web UI, API tokens panel, or POST /api/tokens with a signed-in session.",
            "scopes": {
                "read": "GET only.",
                "full": "Everything except reading secret values and managing tokens.",
            },
        },
        "mcp": {
            "transport": "stdio",
            "server": f"{base}/agent/piharness_mcp.py",
            "run": "python3 piharness_mcp.py",
            "env": {"PIHARNESS_URL": base, "PIHARNESS_TOKEN": "<your token>"},
        },
    }


# ── The AI spec ───────────────────────────────────────────────────────────────

@system.get("/prompt")
def get_prompt():
    """Unauthenticated on purpose: it's public documentation, it contains
    nothing about this install, and being able to curl it is the point."""
    return {"prompt": prompt.spec()}


# ── Global web access: /apps/<name>/… ─────────────────────────────────────────
# Reverse-proxies a program's web UI through the harness, so one published port
# (or tunnel) reaches every program. A program marked public needs no login,
# which is the point of sharing a link; everything else requires a session.
# Buffered stdlib urllib, so no WebSockets or server-sent events pass through.
#
# SECURITY: an imported program is arbitrary third-party code running behind the
# harness's own origin. It must never see the harness's credentials, and must
# never be able to write them. Hence the credential stripping below in both
# directions.

_HOP_HEADERS = {"connection", "keep-alive", "transfer-encoding", "upgrade",
                "proxy-authorization", "te", "trailers", "host", "content-length"}


def _strip_harness_credentials(headers: dict) -> dict:
    """Remove the harness's own credentials from headers bound for a program.

    The session cookie is dropped from the Cookie header while any other cookies
    are preserved, so a program can still run its own sessions. An Authorization
    header is forwarded only when it is not a harness credential, so a program's
    own bearer auth keeps working."""
    out = {}
    for key, value in headers.items():
        low = key.lower()
        if low == "cookie":
            kept = [c for c in (p.strip() for p in value.split(";"))
                    if c and not c.startswith(config.COOKIE_NAME + "=")]
            if kept:
                out[key] = "; ".join(kept)
            continue
        if low == "authorization":
            token = value.removeprefix("Bearer ").strip()
            from harness import auth as _auth
            if validate_session(token) or _auth.validate_api_token(token):
                continue    # ours — the program has no business seeing it
            out[key] = value
            continue
        out[key] = value
    return out


def _fetch(method: str, url: str, headers: dict, body: bytes):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, data=body or None, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:   # 4xx/5xx from the program are still answers
        return e.code, dict(e.headers), e.read()


@proxy.get("/apps/{name}", include_in_schema=False)
def apps_slash(name: str):
    return RedirectResponse(f"/apps/{name}/")


@proxy.api_route("/apps/{name}/{path:path}", include_in_schema=False,
                 methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
async def apps_proxy(name: str, path: str, request: Request):
    if not programs.NAME_RE.fullmatch(name):
        raise HTTPException(404, "Not found")
    prog = programs.load().get(name)
    if not prog or not prog.get("web_port"):
        raise HTTPException(404, "No such program, or it has no web UI.")
    # Private by default: a program is only reachable without a session when it
    # has been explicitly published. With a tunnel running, the opposite default
    # would put every newly imported program on the public internet.
    if not prog.get("public", False):
        from harness import auth as _auth
        token = request.cookies.get(config.COOKIE_NAME) \
            or request.headers.get("authorization", "").removeprefix("Bearer ")
        if not (validate_session(token)
                or _auth.validate_api_token(token, request.method)):
            raise HTTPException(401, "This program's link is private. Sign in first.")
    q = f"?{request.url.query}" if request.url.query else ""
    url = f"http://127.0.0.1:{prog['web_port']}/{path}{q}"
    fwd = _strip_harness_credentials(
        {k: v for k, v in request.headers.items() if k.lower() not in _HOP_HEADERS})
    # Tell the program where it really sits, so it can build correct absolute
    # URLs and see the caller's address instead of 127.0.0.1.
    client = request.client.host if request.client else ""
    fwd["X-Forwarded-For"] = client
    fwd["X-Forwarded-Proto"] = request.url.scheme
    fwd["X-Forwarded-Host"] = request.headers.get("host", "")
    fwd["X-Forwarded-Prefix"] = f"/apps/{name}"
    body = await request.body()
    from starlette.concurrency import run_in_threadpool
    try:
        status, resp_headers, content = await run_in_threadpool(
            _fetch, request.method, url, fwd, body)
    except OSError:
        raise HTTPException(502, f"'{name}' isn't answering on port {prog['web_port']}. Is it running?")
    out_headers = {}
    for k, v in resp_headers.items():
        if k.lower() in _HOP_HEADERS:
            continue
        # A program shares this origin, so an unfiltered Set-Cookie lets it
        # overwrite the harness session cookie and knock the admin out (or worse,
        # fixate a session it chose). Its own cookies are fine.
        if k.lower() == "set-cookie" and v.strip().startswith(config.COOKIE_NAME + "="):
            continue
        out_headers[k] = v
    return Response(content=content, status_code=status, headers=out_headers)
