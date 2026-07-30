"""Programs: import validation, registry lifecycle, units, OTA, proxy gating."""
import pytest

from harness import config, programs


def _settle(name, **fields):
    """Pretend an import finished, optionally setting registry fields."""
    programs._imports.pop(name, None)
    with programs._lock:
        d = programs.load()
        d[name].update({"status": "ready", **fields})
        programs.save(d)


def test_requires_auth(client):
    assert client.get("/api/programs").status_code == 401


def test_rejects_non_github_repo(authed):
    assert authed.post("/api/programs",
                       json={"repo_url": "https://evil.example/x/y.git"}).status_code == 400


def test_rejects_the_harness_own_port(authed):
    r = authed.post("/api/programs", json={"repo_url": "owner/repo", "web_port": config.PORT})
    assert r.status_code == 400
    assert authed.post("/api/programs", json={"repo_url": "o/x", "web_port": 80}).status_code == 400


def test_shorthand_import_and_duplicate(authed):
    r = authed.post("/api/programs", json={"repo_url": "Owner/My_App", "web_port": 3000})
    assert r.status_code == 200
    name = r.json()["name"]
    assert name == "my_app"

    listed = authed.get("/api/programs").json()["programs"]
    assert [p["name"] for p in listed] == [name]
    assert listed[0]["status"] == "importing"
    assert listed[0]["repo_url"] == "https://github.com/Owner/My_App"

    assert authed.post("/api/programs", json={"repo_url": "Owner/My_App"}).status_code == 409


def test_edit_and_delete(authed):
    authed.post("/api/programs", json={"repo_url": "o/app"})
    _settle("app", status="needs_command")

    assert authed.put("/api/programs/app", json={"start_command": "python3 main.py"}).status_code == 200
    assert programs.load()["app"]["status"] == "ready"

    assert authed.delete("/api/programs/app").status_code == 200
    assert programs.load() == {}
    assert authed.delete("/api/programs/app").status_code == 404


def test_unit_written_with_background_priority(authed):
    authed.post("/api/programs", json={"repo_url": "o/app", "web_port": 3000})
    _settle("app", start_command="python3 main.py")
    authed.put("/api/programs/app", json={"start_command": "python3 main.py"})

    unit = (config.UNIT_DIR / "harness-prog-app.service").read_text()
    assert "ExecStart=/bin/bash -lc 'python3 main.py'" in unit
    assert "Restart=always" in unit
    assert "Nice=15" in unit and "CPUWeight=20" in unit
    assert f"EnvironmentFile=-{config.ENV_DIR}/app.env" in unit
    assert "Environment=PORT=3000" in unit   # the port is handed to the program


def test_refresh_units_is_idempotent(authed, monkeypatch):
    authed.post("/api/programs", json={"repo_url": "o/app"})
    _settle("app", start_command="python3 main.py", dir="/opt/x")

    restarts = []
    monkeypatch.setattr(programs, "unit_state", lambda name: "active")
    monkeypatch.setattr(programs, "_run", lambda cmd, **kw: (
        restarts.append(cmd[-1]) if cmd[:2] == ["systemctl", "restart"] else None, (0, ""))[1])

    programs.refresh_units()
    assert "Nice=15" in (config.UNIT_DIR / "harness-prog-app.service").read_text()
    assert restarts == ["harness-prog-app"]   # a changed unit is restarted once

    restarts.clear()
    programs.refresh_units()                  # unchanged → no churn
    assert restarts == []


def test_secrets_roundtrip(authed):
    authed.post("/api/programs", json={"repo_url": "o/app"})
    _settle("app")

    assert authed.put("/api/programs/app/secrets", json={"env": "not a valid line"}).status_code == 400

    r = authed.put("/api/programs/app/secrets", json={"env": "API_KEY=abc\n# note\nDB_URL=x"})
    assert r.status_code == 200
    env = programs.env_file("app")
    assert "API_KEY=abc" in env.read_text()
    assert env.stat().st_mode & 0o777 == 0o600
    assert authed.get("/api/programs/app/secrets").json()["env"].startswith("API_KEY=abc")

    # A blank payload deletes the file; removing the program would too.
    authed.put("/api/programs/app/secrets", json={"env": ""})
    assert not env.exists()


def test_secrets_die_with_the_program(authed):
    authed.post("/api/programs", json={"repo_url": "o/app"})
    _settle("app")
    authed.put("/api/programs/app/secrets", json={"env": "API_KEY=abc"})
    assert programs.env_file("app").exists()
    authed.delete("/api/programs/app")
    assert not programs.env_file("app").exists()


def test_ota_modes_and_update_check(authed, monkeypatch):
    authed.post("/api/programs", json={"repo_url": "o/checked"})
    authed.post("/api/programs", json={"repo_url": "o/selfmanaged", "ota": "self"})
    _settle("checked")
    _settle("selfmanaged")

    assert authed.post("/api/programs", json={"repo_url": "o/x", "ota": "nightly"}).status_code == 400

    def fake_run(cmd, **kw):
        if cmd[:2] == ["git", "-C"]:
            return 0, "aaaa111122223333"            # local sha
        if cmd[:2] == ["git", "ls-remote"]:
            return 0, "bbbb444455556666\tHEAD"      # remote moved ahead
        return 0, ""
    monkeypatch.setattr(programs, "_run", fake_run)

    ups = authed.get("/api/programs/updates").json()["updates"]
    assert ups["checked"]["update_available"] is True
    assert "selfmanaged" not in ups                 # self-managed is skipped

    assert authed.put("/api/programs/selfmanaged", json={"ota": "github"}).status_code == 200
    assert programs.load()["selfmanaged"]["ota"] == "github"


def test_auto_update_pulls_only_auto_programs_with_new_commits(authed, monkeypatch):
    authed.post("/api/programs", json={"repo_url": "o/auto"})
    authed.post("/api/programs", json={"repo_url": "o/manual"})   # ota=github, left alone
    _settle("auto", start_command="python3 main.py", dir="/opt/auto", ota="auto")
    _settle("manual", start_command="python3 main.py", dir="/opt/manual")

    pulled = []

    def fake_run(cmd, **kw):
        if cmd[:2] == ["git", "-C"] and cmd[3] == "rev-parse":
            return 0, "aaaa1111"                      # local sha
        if cmd[:2] == ["git", "ls-remote"]:
            return 0, "bbbb2222\tHEAD"                # remote moved ahead
        if cmd[:2] == ["git", "-C"] and cmd[3] == "pull":
            pulled.append(cmd[2])
            return 0, "Updating aaaa1111..bbbb2222"
        return 0, ""
    monkeypatch.setattr(programs, "_run", fake_run)
    monkeypatch.setattr(programs, "install_deps", lambda *a, **k: None)

    assert programs.run_auto_updates() == ["auto"]    # only the auto-mode program
    assert len(pulled) == 1 and pulled[0].endswith("auto")


def test_private_repo_token_never_leaves_the_pi(authed, monkeypatch):
    assert authed.post("/api/programs",
                       json={"repo_url": "o/priv", "token": "bad token!"}).status_code == 400

    assert authed.post("/api/programs",
                       json={"repo_url": "o/priv", "token": "ghp_abc123def456"}).status_code == 200
    _settle("priv")

    p = authed.get("/api/programs").json()["programs"][0]
    assert p["has_token"] is True
    assert "token" not in p                                    # never returned
    assert programs.load()["priv"]["token"] == "ghp_abc123def456"
    assert config.REGISTRY_FILE.stat().st_mode & 0o777 == 0o600

    # git runs with the auth header in the environment, not in argv.
    seen = {}

    def fake_run(cmd, **kw):
        if cmd[:2] == ["git", "ls-remote"]:
            seen.update(kw.get("env") or {})
            return 0, "cafe000011112222\tHEAD"
        return 0, "cafe000011112222"
    monkeypatch.setattr(programs, "_run", fake_run)
    authed.get("/api/programs/updates")
    assert seen["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert "AUTHORIZATION: basic " in seen["GIT_CONFIG_VALUE_0"]
    assert "ghp_abc123def456" not in seen["GIT_CONFIG_VALUE_0"]   # base64, not plaintext

    assert authed.put("/api/programs/priv", json={"token": ""}).status_code == 200
    assert programs.load()["priv"]["token"] is None


def test_start_command_detection(tmp_path):
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    assert programs.detect_start_command(repo) is None

    (repo / "index.js").write_text("")
    assert programs.detect_start_command(repo) == "node index.js"

    (repo / "main.py").write_text("")
    assert programs.detect_start_command(repo) == "python3 main.py"

    (repo / "package.json").write_text('{"scripts": {"start": "node ."}}')
    assert programs.detect_start_command(repo) == "npm start"


def test_global_link_uses_configured_public_url(authed, monkeypatch):
    authed.post("/api/programs", json={"repo_url": "o/web", "web_port": 3000})
    _settle("web")
    assert authed.get("/api/programs").json()["programs"][0]["global_url"] is None

    monkeypatch.setattr(config, "PUBLIC_URL", "https://pi.example.com")
    p = authed.get("/api/programs").json()["programs"][0]
    assert p["global_url"] == "https://pi.example.com/apps/web/"
    assert p["global_via"] == "configured"


def test_proxy_unknown_and_private(authed):
    from fastapi.testclient import TestClient
    from harness.main import app

    assert authed.get("/apps/nope/").status_code == 404

    with programs._lock:
        programs.save({"app": {"repo_url": "https://github.com/o/app", "dir": "/x",
                               "web_port": 3111, "public": False, "status": "ready"}})
    # A private program with no session: 401 before any proxying is attempted.
    anon = TestClient(app)
    assert anon.get("/apps/app/").status_code == 401

    # Public, but nothing listening on the port → a clear 502, not a stack trace.
    with programs._lock:
        d = programs.load()
        d["app"]["public"] = True
        programs.save(d)
    assert anon.get("/apps/app/").status_code == 502


def test_monitor_kiosk(authed, monkeypatch):
    from harness import kiosk

    authed.post("/api/programs", json={"repo_url": "o/web", "web_port": 3000})
    authed.post("/api/programs", json={"repo_url": "o/headless"})
    _settle("web")
    _settle("headless")

    # No web UI → nothing to show.
    assert authed.post("/api/programs/headless/monitor", json={"on": True}).status_code == 409

    # Missing kiosk packages → a 409 that names what to install.
    monkeypatch.setattr(kiosk.shutil, "which", lambda b: None)
    r = authed.post("/api/programs/web/monitor", json={"on": True})
    assert r.status_code == 409 and "apt install" in r.json()["detail"]

    monkeypatch.setattr(kiosk.shutil, "which", lambda b: f"/usr/bin/{b}")
    # No monitor detected is still allowed: the kiosk arms and displays when one
    # is plugged in; the response says it isn't connected yet.
    monkeypatch.setattr(kiosk, "monitor_connected", lambda: False)
    r = authed.post("/api/programs/web/monitor", json={"on": True})
    assert r.status_code == 200 and r.json()["connected"] is False

    monkeypatch.setattr(kiosk, "monitor_connected", lambda: True)
    assert authed.post("/api/programs/web/monitor", json={"on": True}).status_code == 200

    unit = (config.UNIT_DIR / "harness-kiosk.service").read_text()
    # Boot-friendly: starts after (and pulls in) the program's own unit, and
    # waits for the port to answer before opening the browser.
    assert "Wants=harness-prog-web.service" in unit
    assert "/dev/tcp/127.0.0.1/3000" in unit
    # No modprobe: loading udl with a DL-3xxx monitor attached wedges the kernel.
    assert "modprobe" not in unit

    # The launcher handles both display kinds: USB DisplayLink (evdi, resolved
    # via /dev/dri/by-path, rendered on the board's GPU through seatd) and
    # plain HDMI (cage on the connected card).
    script = config.KIOSK_SCRIPT.read_text()
    assert "http://127.0.0.1:3000/" in script
    assert "evdi" in script and "seatd" in script and "WLR_DRM_DEVICES" in script

    listed = authed.get("/api/programs").json()
    assert listed["monitor"] == {"connected": True, "program": "web"}
    assert {p["name"]: p["on_monitor"] for p in listed["programs"]} == {"headless": False, "web": True}

    # Monitor command: saved via edit, baked into the kiosk unit as ExecStartPre.
    assert authed.put("/api/programs/web", json={"monitor_command": "./warmup.sh --once"}).status_code == 200
    unit = (config.UNIT_DIR / "harness-kiosk.service").read_text()
    assert "systemd-run --collect --no-block --unit=harness-kiosk-cmd" in unit
    assert "/bin/bash -lc './warmup.sh --once'" in unit

    authed.put("/api/programs/web", json={"monitor_command": ""})   # empty clears it
    assert "warmup.sh" not in (config.UNIT_DIR / "harness-kiosk.service").read_text()
    assert programs.load()["web"]["monitor_command"] is None

    # Removing the program on the monitor turns the kiosk off too.
    assert authed.delete("/api/programs/web").status_code == 200
    assert not (config.UNIT_DIR / "harness-kiosk.service").exists()
    assert authed.get("/api/programs").json()["monitor"]["program"] is None


def test_clearing_the_port_turns_the_kiosk_off(authed, monkeypatch):
    from harness import kiosk

    authed.post("/api/programs", json={"repo_url": "o/web", "web_port": 3000})
    _settle("web")
    monkeypatch.setattr(kiosk.shutil, "which", lambda b: f"/usr/bin/{b}")
    authed.post("/api/programs/web/monitor", json={"on": True})
    assert kiosk.current() == "web"

    authed.put("/api/programs/web", json={"clear_port": True})
    assert kiosk.current() is None


@pytest.mark.parametrize("name", ["../evil", "UPPER", "with space", "-leading", ""])
def test_bad_names_are_rejected(authed, name):
    assert authed.get(f"/api/programs/{name}/logs").status_code in (400, 404)
