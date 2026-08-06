import importlib
import pytest


@pytest.fixture(autouse=True)
def tmp_paths(tmp_path, monkeypatch):
    """Point every on-disk path at this test's temp dir, so nothing touches the
    real /etc or /opt and no state leaks between tests."""
    monkeypatch.setenv("HARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("HARNESS_PROGRAMS_DIR", str(tmp_path / "programs"))
    monkeypatch.setenv("HARNESS_UNIT_DIR", str(tmp_path / "units"))
    monkeypatch.setenv("HARNESS_PUBLIC_URL", "")

    from harness import config
    importlib.reload(config)
    (tmp_path / "config").mkdir()
    (tmp_path / "units").mkdir()

    # Modules captured `config` as a module object, so reloading it is enough —
    # but sessions, throttles, rate-limit counters, sampled metrics and the
    # import registry are all process state that would otherwise leak between
    # tests. The rate limiter especially: every test signs in from the same
    # client address, so without this the suite throttles itself.
    from harness import auth, metrics, programs, tunnel
    auth._sessions.clear()
    auth._attempts.clear()
    auth._requests.clear()
    programs._imports.clear()
    metrics._prev_cpu = None
    for series in metrics._history.values():
        series.clear()
    tunnel._quick_cache[0], tunnel._quick_cache[1] = 0.0, None
    return tmp_path


@pytest.fixture
def no_shell(monkeypatch):
    """Never invoke git or systemctl in tests."""
    from harness import programs
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        return 0, ""

    monkeypatch.setattr(programs, "_run", fake_run)
    return calls


@pytest.fixture
def client(no_shell, monkeypatch):
    """A TestClient with the import worker stubbed out, so POSTing a program
    registers it without cloning anything."""
    from fastapi.testclient import TestClient
    from harness import programs
    from harness.main import app
    monkeypatch.setattr(programs, "import_worker", lambda *a, **k: None)
    return TestClient(app)


@pytest.fixture
def authed(client):
    r = client.post("/api/setup", json={"username": "admin", "password": "testpassword"})
    assert r.status_code == 200
    return client
