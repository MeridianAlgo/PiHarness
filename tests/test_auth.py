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
