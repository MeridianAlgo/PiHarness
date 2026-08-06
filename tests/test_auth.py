"""Sign-in: first-run setup, sessions, throttling, password changes."""


def test_setup_required_then_closed(client):
    assert client.get("/api/status").json()["setup_required"] is True
    assert client.get("/api/programs").status_code == 401

    r = client.post("/api/setup", json={"username": "admin", "password": "testpassword"})
    assert r.status_code == 200
    assert client.get("/api/status").json()["setup_required"] is False
    assert client.get("/api/me").json()["username"] == "admin"

    # Setup is a one-shot: it can't be used to add a second account.
    assert client.post("/api/setup", json={"username": "x", "password": "testpassword"}).status_code == 409


def test_setup_rejects_short_password(client):
    assert client.post("/api/setup", json={"username": "admin", "password": "short"}).status_code == 400
    assert client.get("/api/status").json()["setup_required"] is True


def test_login_logout(authed):
    authed.post("/api/logout")
    assert authed.get("/api/me").status_code == 401

    assert authed.post("/api/login", json={"username": "admin", "password": "nope"}).status_code == 401
    r = authed.post("/api/login", json={"username": "admin", "password": "testpassword"})
    assert r.status_code == 200
    assert authed.get("/api/me").json()["username"] == "admin"


def test_api_token_works_without_cookie(authed):
    from fastapi.testclient import TestClient
    from harness.main import app

    token = authed.post("/api/tokens", json={"label": "ci"}).json()["token"]
    assert token.startswith("phk_")

    anon = TestClient(app)
    assert anon.get("/api/programs").status_code == 401
    assert anon.get("/api/programs", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_api_token_is_revocable_and_never_shown_again(authed):
    from fastapi.testclient import TestClient
    from harness.main import app

    token = authed.post("/api/tokens", json={"label": "throwaway"}).json()["token"]
    listed = authed.get("/api/tokens").json()["tokens"]
    assert [t["label"] for t in listed] == ["throwaway"]
    assert not any(token in str(t) for t in listed)   # only metadata comes back

    anon = TestClient(app)
    assert anon.get("/api/programs", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert authed.delete(f"/api/tokens/{listed[0]['id']}").status_code == 200
    assert anon.get("/api/programs", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_session_token_is_not_an_api_credential(authed):
    """A session cookie used to double as a bearer token, which made it
    unrevocable and gave it the session's lifetime. It must not work now."""
    from fastapi.testclient import TestClient
    from harness import config
    from harness.main import app

    session = authed.cookies.get(config.COOKIE_NAME)
    assert session
    # Login no longer hands the session token back to the caller at all.
    body = authed.post("/api/login", json={"username": "admin", "password": "testpassword"}).json()
    assert "token" not in body

    anon = TestClient(app)
    r = anon.get("/api/programs", headers={"Authorization": f"Bearer {session}"})
    assert r.status_code == 401
    assert "API token" in r.json()["detail"]


def test_login_throttled_after_repeated_failures(authed):
    from harness import auth
    for _ in range(auth.MAX_ATTEMPTS):
        authed.post("/api/login", json={"username": "admin", "password": "wrong"})
    r = authed.post("/api/login", json={"username": "admin", "password": "testpassword"})
    assert r.status_code == 429   # right password, still locked out


def test_password_change_invalidates_sessions(authed):
    assert authed.post("/api/password", json={
        "current_password": "wrong", "new_password": "brandnewpass"}).status_code == 401

    r = authed.post("/api/password", json={
        "current_password": "testpassword", "new_password": "brandnewpass"})
    assert r.status_code == 200
    assert authed.get("/api/me").status_code == 401   # signed out everywhere
    assert authed.post("/api/login", json={
        "username": "admin", "password": "brandnewpass"}).status_code == 200


# ── Cookie scope ──────────────────────────────────────────────────────────────
# The harness is normally reachable two ways at once: plain HTTP on the LAN and
# HTTPS through the tunnel. Secure was decided from "is a tunnel enabled", which
# is a property of the box rather than of the request — so turning on a tunnel
# marked the cookie Secure for LAN sign-ins too, the browser discarded a cookie
# it had just been handed, and every call after a successful sign-in came back
# 401 with no way to tell why.

def _cookie_header(response):
    return response.headers.get("set-cookie", "")


def test_lan_signin_over_http_gets_a_usable_cookie(client, monkeypatch):
    from harness import tunnel
    monkeypatch.setattr(tunnel, "load", lambda: {"enabled": True, "mode": "quick"})
    monkeypatch.setattr(tunnel, "unit_state", lambda: "active")

    r = client.post("/api/setup", json={"username": "admin", "password": "testpassword"})
    assert r.status_code == 200
    assert "secure" not in _cookie_header(r).lower()   # or the browser drops it
    assert client.get("/api/me").status_code == 200    # and sign-in actually holds


def test_https_signin_gets_a_secure_cookie(client):
    r = client.post("/api/setup", json={"username": "admin", "password": "testpassword"},
                    headers={"X-Forwarded-Proto": "https"})
    assert r.status_code == 200
    assert "secure" in _cookie_header(r).lower()


def test_hsts_only_on_https(client, monkeypatch):
    from harness import tunnel
    monkeypatch.setattr(tunnel, "load", lambda: {"enabled": True, "mode": "quick"})
    monkeypatch.setattr(tunnel, "unit_state", lambda: "active")
    # Pinning HSTS on a plain-HTTP LAN hostname makes the browser force HTTPS to
    # a Pi with no certificate, and that lockout outlives the tunnel.
    assert "strict-transport-security" not in client.get("/api/status").headers
    r = client.get("/api/status", headers={"X-Forwarded-Proto": "https"})
    assert "strict-transport-security" in r.headers
