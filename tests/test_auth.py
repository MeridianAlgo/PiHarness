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


def test_bearer_token_works_without_cookie(authed):
    from fastapi.testclient import TestClient
    from harness.main import app

    token = authed.post("/api/login", json={"username": "admin", "password": "testpassword"}).json()["token"]
    anon = TestClient(app)
    assert anon.get("/api/programs").status_code == 401
    assert anon.get("/api/programs", headers={"Authorization": f"Bearer {token}"}).status_code == 200


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
