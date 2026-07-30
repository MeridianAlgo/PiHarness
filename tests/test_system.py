"""Tunnel, dashboard metrics, and the security boundary around the /apps proxy."""
import json

import pytest

from harness import api, config, metrics, programs, tunnel


def _settle(name, **fields):
    """Move a program out of 'importing' the way the import worker would."""
    d = programs.load()
    d[name].update({"status": "ready", **fields})
    programs.save(d)


def header(headers: dict, name: str):
    """Case-insensitive lookup. Starlette hands headers over lowercased, so a
    plain `"Authorization" not in headers` is true whether or not the value
    leaked — exactly the false pass these tests exist to prevent."""
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


# ── The proxy must not hand the harness's credentials to a program ────────────
# A program is arbitrary third-party code from GitHub running behind the
# harness's own origin. Everything below is about keeping it there.

@pytest.fixture
def proxied(authed, monkeypatch):
    """A running public program, with the upstream fetch captured instead of
    performed. Returns the list of (method, url, headers) it saw."""
    seen = []

    def fake_fetch(method, url, headers, body):
        seen.append({"method": method, "url": url, "headers": headers})
        return 200, {"Content-Type": "text/plain"}, b"ok"

    monkeypatch.setattr(api, "_fetch", fake_fetch)
    authed.post("/api/programs", json={"repo_url": "o/app", "web_port": 3000})
    _settle("app", start_command="python3 main.py", public=True)
    return authed, seen


def test_proxy_strips_the_harness_session_cookie(proxied):
    """The bug this guards: a program could read harness_session from the
    forwarded Cookie header and act as the admin."""
    client, seen = proxied
    r = client.get("/apps/app/", cookies={"harness_session": "stolen", "theirs": "keep"})
    assert r.status_code == 200
    cookie = header(seen[-1]["headers"], "cookie") or ""
    assert "harness_session" not in cookie
    assert "stolen" not in cookie
    assert "theirs=keep" in cookie      # the program's own cookies still work


def test_proxy_strips_a_harness_api_token(authed, proxied):
    client, seen = proxied
    token = client.post("/api/tokens", json={"label": "t"}).json()["token"]
    client.get("/apps/app/", headers={"Authorization": f"Bearer {token}"})
    assert header(seen[-1]["headers"], "authorization") is None
    assert not any(token in str(v) for v in seen[-1]["headers"].values())


def test_proxy_forwards_a_foreign_authorization_header(proxied):
    """A program's own bearer auth has to keep working; only ours is removed."""
    client, seen = proxied
    client.get("/apps/app/", headers={"Authorization": "Bearer not-a-harness-token"})
    assert header(seen[-1]["headers"], "authorization") == "Bearer not-a-harness-token"


def test_proxy_sets_forwarded_headers(proxied):
    client, seen = proxied
    client.get("/apps/app/thing")
    h = seen[-1]["headers"]
    assert header(h, "x-forwarded-prefix") == "/apps/app"
    assert header(h, "x-forwarded-proto") in ("http", "https")


def test_proxy_refuses_a_program_setting_the_harness_cookie(authed, monkeypatch):
    """Same origin means a program could otherwise overwrite the session cookie
    in the user's browser and sign them out, or fixate a session."""
    def evil_fetch(method, url, headers, body):
        return 200, {"Set-Cookie": f"{config.COOKIE_NAME}=attacker; Path=/"}, b"x"

    monkeypatch.setattr(api, "_fetch", evil_fetch)
    authed.post("/api/programs", json={"repo_url": "o/evil", "web_port": 3001})
    _settle("evil", start_command="x", public=True)
    r = authed.get("/apps/evil/")
    assert r.status_code == 200
    assert config.COOKIE_NAME not in r.headers.get("set-cookie", "")


def test_programs_are_private_until_published(authed, monkeypatch):
    """With a tunnel up, a public default would put every freshly imported
    program on the internet."""
    monkeypatch.setattr(api, "_fetch", lambda *a: (200, {}, b"ok"))
    authed.post("/api/programs", json={"repo_url": "o/app", "web_port": 3000})
    _settle("app", start_command="x")
    assert programs.listing()[0]["public"] is False

    from fastapi.testclient import TestClient
    from harness.main import app
    assert TestClient(app).get("/apps/app/").status_code == 401

    authed.put("/api/programs/app", json={"public": True})
    assert TestClient(app).get("/apps/app/").status_code == 200


# ── Rate limiting and body size ───────────────────────────────────────────────

def test_api_is_rate_limited(authed, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT", 5)
    codes = [authed.get("/api/programs").status_code for _ in range(8)]
    assert 429 in codes
    assert codes.count(200) <= 5


def test_oversized_body_is_refused(authed, monkeypatch):
    monkeypatch.setattr(config, "MAX_BODY_BYTES", 100)
    r = authed.put("/api/programs/x/secrets", json={"env": "K=" + "v" * 500})
    assert r.status_code == 413


# ── Tunnel ────────────────────────────────────────────────────────────────────

@pytest.fixture
def cloudflared(monkeypatch, no_shell):
    """Pretend cloudflared is installed and systemd cooperates."""
    monkeypatch.setattr(tunnel, "binary", lambda: "/usr/bin/cloudflared")
    monkeypatch.setattr(tunnel, "unit_state", lambda: "active")
    return no_shell


def test_quick_tunnel_writes_a_unit_and_reports_its_address(authed, cloudflared, monkeypatch):
    monkeypatch.setattr(tunnel, "quick_hostname", lambda: "brave-pi-42.trycloudflare.com")
    r = authed.post("/api/tunnel", json={"mode": "quick"})
    assert r.status_code == 200
    body = r.json()
    assert body["url"] == "https://brave-pi-42.trycloudflare.com"
    assert body["ephemeral"] is True        # the address changes on restart

    unit = (config.UNIT_DIR / "harness-tunnel.service").read_text()
    assert f"--url http://127.0.0.1:{config.PORT}" in unit
    assert "tunnel --no-autoupdate" in unit


def test_named_tunnel_keeps_the_token_out_of_the_unit_file(authed, cloudflared):
    token = "eyJhIjoi" + "x" * 120
    r = authed.post("/api/tunnel", json={"mode": "named", "token": token,
                                         "hostname": "pi.example.com"})
    assert r.status_code == 200
    assert r.json()["url"] == "https://pi.example.com"

    unit = (config.UNIT_DIR / "harness-tunnel.service").read_text()
    assert token not in unit                       # not in the unit
    assert "ExecStart" in unit and "--token" not in unit   # not in argv either
    env = (config.CONFIG_DIR / "tunnel.env").read_text()
    assert env.strip() == f"TUNNEL_TOKEN={token}"  # only in the 0600 env file


def test_named_tunnel_rejects_a_junk_token(authed, cloudflared):
    r = authed.post("/api/tunnel", json={"mode": "named", "token": "nope"})
    assert r.status_code == 409
    assert "connector token" in r.json()["detail"]


def test_tunnel_requires_cloudflared(authed, monkeypatch, no_shell):
    monkeypatch.setattr(tunnel, "binary", lambda: None)
    r = authed.post("/api/tunnel", json={"mode": "quick"})
    assert r.status_code == 409
    assert "cloudflared isn't installed" in r.json()["detail"]


def test_disabling_the_tunnel_removes_the_unit_and_the_token(authed, cloudflared):
    authed.post("/api/tunnel", json={"mode": "named", "token": "eyJhIjoi" + "x" * 120})
    assert (config.CONFIG_DIR / "tunnel.env").exists()

    r = authed.delete("/api/tunnel")
    assert r.status_code == 200
    assert r.json()["enabled"] is False
    assert not (config.UNIT_DIR / "harness-tunnel.service").exists()
    assert not (config.CONFIG_DIR / "tunnel.env").exists()


def test_global_links_prefer_the_tunnel_over_tailscale(authed, cloudflared, monkeypatch):
    monkeypatch.setattr(tunnel, "quick_hostname", lambda: "abc.trycloudflare.com")
    authed.post("/api/tunnel", json={"mode": "quick"})
    authed.post("/api/programs", json={"repo_url": "o/app", "web_port": 3000})
    _settle("app", start_command="x")

    entry = programs.listing()[0]
    assert entry["global_url"] == "https://abc.trycloudflare.com/apps/app/"
    assert entry["global_via"] == "cloudflare"


def test_configured_public_url_still_wins(authed, cloudflared, monkeypatch):
    monkeypatch.setattr(tunnel, "quick_hostname", lambda: "abc.trycloudflare.com")
    monkeypatch.setattr(config, "PUBLIC_URL", "https://chosen.example.com")
    authed.post("/api/tunnel", json={"mode": "quick"})
    authed.post("/api/programs", json={"repo_url": "o/app", "web_port": 3000})
    _settle("app", start_command="x")
    assert programs.listing()[0]["global_via"] == "configured"


# ── Metrics ───────────────────────────────────────────────────────────────────

def test_metrics_endpoint_shape(authed):
    body = authed.get("/api/metrics").json()
    assert set(body) == {"host", "throttled", "history", "interval", "programs"}
    assert set(body["history"]) == {"cpu", "temp", "memory", "disk"}
    # Every host reader returns None rather than raising off-Linux, so the
    # endpoint answers on any platform — which is what makes it testable here.
    assert "cpu_percent" in body["host"]


def test_history_is_bounded(authed):
    for i in range(metrics.HISTORY_POINTS + 40):
        metrics.record({"cpu_percent": float(i % 100), "temperature": 50.0,
                        "memory": {"percent": 10.0}, "disk": {"percent": 20.0}})
    hist = metrics.history()
    assert len(hist["cpu"]) == metrics.HISTORY_POINTS
    # The buffer keeps the newest samples, not the oldest.
    assert hist["cpu"][-1][1] == float((metrics.HISTORY_POINTS + 39) % 100)


def test_cpu_percent_needs_two_samples():
    """The first reading after start has no previous total to diff against, and
    reporting a number there would be inventing one."""
    import time

    metrics._prev_cpu = None
    if metrics._read("/proc/stat") is None:
        pytest.skip("not Linux")
    assert metrics.cpu_percent() is None
    # /proc/stat counts in jiffies (10ms). Two reads in the same jiffy give a
    # zero delta and correctly report None, so wait for the clock to move.
    time.sleep(0.15)
    assert metrics.cpu_percent() is not None


def test_no_module_binds_run_at_import():
    """`from harness.programs import _run` binds the original function, so a
    monkeypatch of programs._run does not reach that module and the test shells
    out for real. That bug is invisible wherever systemctl is absent (_run
    returns -1, which no caller treats as failure) and only appears on a machine
    that has it, so it needs a check that does not depend on the platform."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "harness"
    offenders = [p.name for p in root.glob("*.py")
                 if "from harness.programs import" in p.read_text(encoding="utf-8")
                 and "_run" in p.read_text(encoding="utf-8").split(
                     "from harness.programs import")[1].split("\n")[0]]
    assert not offenders, f"{offenders} bind _run at import; call programs._run instead"


def test_program_stats_parses_systemd_output(monkeypatch):
    monkeypatch.setattr(programs, "_run", lambda *a, **k: (0, "\n".join([
        "MainPID=1234",
        "MemoryCurrent=52428800",
        "CPUUsageNSec=90000000000",
        "NRestarts=3",
        "ActiveEnterTimestampMonotonic=1000000",
        "ActiveState=active",
    ])))
    monkeypatch.setattr(metrics, "uptime_seconds", lambda: 3601.0)
    s = metrics.program_stats("harness-prog-app")
    assert s["pid"] == 1234
    assert s["memory"] == 52428800
    assert s["cpu_seconds"] == 90.0
    assert s["restarts"] == 3
    assert s["uptime"] == 3600.0


def test_program_stats_treats_systemd_unknowns_as_missing(monkeypatch):
    """systemd reports an undeterminable unsigned property as 2**64-1, which
    would otherwise render as 18 exabytes of memory use."""
    monkeypatch.setattr(programs, "_run", lambda *a, **k: (0, "\n".join([
        "MainPID=0",
        f"MemoryCurrent={2 ** 64 - 1}",
        "CPUUsageNSec=[not set]",
        "NRestarts=0",
    ])))
    s = metrics.program_stats("harness-prog-app")
    assert s["memory"] is None
    assert s["cpu_seconds"] is None
    assert s["pid"] is None


# ── The AI spec ───────────────────────────────────────────────────────────────

def test_prompt_is_served_and_is_the_only_copy(client):
    from harness import prompt
    body = client.get("/api/prompt").json()          # no auth required
    assert body["prompt"] == prompt.SPEC
    assert "LONG-RUNNING PROCESS" in body["prompt"]

    ui = (config.UI_DIR / "assets" / "js" / "app.js").read_text(encoding="utf-8")
    assert "AI_PROMPT" not in ui, "the spec was duplicated back into the UI"
    assert "/api/prompt" in ui


def test_docs_quote_the_spec_verbatim():
    """docs/programs.md inlines the spec for people reading it on GitHub. That
    copy has to be the real one, or the page confidently documents something the
    harness no longer says."""
    from harness import prompt
    docs = (config.UI_DIR.parent / "docs" / "programs.md").read_text(encoding="utf-8")
    assert prompt.SPEC in docs, "docs/programs.md has drifted from harness/prompt.py"
