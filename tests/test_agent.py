"""Token scopes, the session-only guards, and the MCP server."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from harness import auth, programs

MCP = Path(__file__).parent.parent / "agent" / "piharness_mcp.py"


def _token(client, label="agent", scope="full") -> str:
    r = client.post("/api/tokens", json={"label": label, "scope": scope})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _anon():
    """A client with no session, so only the bearer token authenticates it."""
    from fastapi.testclient import TestClient
    from harness.main import app
    return TestClient(app)


# ── Scopes ────────────────────────────────────────────────────────────────────

def test_scope_is_stored_and_listed(authed):
    _token(authed, "reader", "read")
    _token(authed, "writer", "full")
    scopes = {t["label"]: t["scope"] for t in authed.get("/api/tokens").json()["tokens"]}
    assert scopes == {"reader": "read", "writer": "full"}


def test_scope_defaults_to_full_and_rejects_nonsense(authed):
    r = authed.post("/api/tokens", json={"label": "plain"})
    assert r.status_code == 200 and r.json()["scope"] == "full"
    assert authed.post("/api/tokens", json={"label": "x", "scope": "admin"}).status_code == 400


def test_read_token_can_look_but_not_touch(authed):
    h = {"Authorization": f"Bearer {_token(authed, 'reader', 'read')}"}
    anon = _anon()
    assert anon.get("/api/programs", headers=h).status_code == 200
    r = anon.post("/api/programs", headers=h, json={"repo_url": "o/app"})
    assert r.status_code == 403
    assert "read-only" in r.json()["detail"]


def test_full_token_can_write(authed):
    h = {"Authorization": f"Bearer {_token(authed)}"}
    assert _anon().post("/api/programs", headers=h,
                        json={"repo_url": "o/app"}).status_code == 200
    assert "app" in programs.load()


# ── Session-only operations ───────────────────────────────────────────────────

def test_a_token_cannot_mint_or_revoke_tokens(authed):
    """Otherwise a read token could simply issue itself a full one."""
    anon = _anon()
    for scope in ("read", "full"):
        h = {"Authorization": f"Bearer {_token(authed, scope, scope)}"}
        assert anon.post("/api/tokens", headers=h,
                         json={"label": "escalated", "scope": "full"}).status_code == 403
        assert anon.get("/api/tokens", headers=h).status_code == 403
        assert anon.delete("/api/tokens/whatever", headers=h).status_code == 403


def test_a_token_cannot_change_the_password(authed):
    h = {"Authorization": f"Bearer {_token(authed)}"}
    assert _anon().post("/api/password", headers=h, json={
        "current_password": "testpassword", "new_password": "somethingelse"}).status_code == 403


def test_a_token_can_write_secrets_but_not_read_their_values(authed):
    authed.post("/api/programs", json={"repo_url": "o/app"})
    programs._imports.clear()
    anon = _anon()
    h = {"Authorization": f"Bearer {_token(authed)}"}

    assert anon.put("/api/programs/app/secrets", headers=h,
                    json={"env": "API_KEY=abc\nDB=x"}).status_code == 200
    assert anon.get("/api/programs/app/secrets", headers=h).status_code == 403
    assert anon.get("/api/programs/app/secret-names", headers=h).json()["names"] == ["API_KEY", "DB"]
    # The signed-in session is still allowed to see the values.
    assert "API_KEY=abc" in authed.get("/api/programs/app/secrets").json()["env"]


def test_read_scope_holds_through_the_apps_proxy(authed):
    """The proxy must not be a way around the scope: a private program's own
    endpoints are reachable through it."""
    read = _token(authed, "reader", "read")
    full = _token(authed, "writer", "full")
    with programs._lock:
        programs.save({"app": {"repo_url": "https://github.com/o/app", "dir": "/x",
                               "web_port": 3111, "public": False, "status": "ready"}})
    anon = _anon()

    # A GET is allowed through, then fails to connect to the program itself.
    assert anon.get("/apps/app/", headers={"Authorization": f"Bearer {read}"}).status_code == 502
    # A write is turned away before any proxying happens.
    assert anon.post("/apps/app/", headers={"Authorization": f"Bearer {read}"}).status_code == 401
    assert anon.post("/apps/app/", headers={"Authorization": f"Bearer {full}"}).status_code == 502


def test_revoked_token_stops_working(authed):
    token = _token(authed)
    token_id = authed.get("/api/tokens").json()["tokens"][0]["id"]
    anon = _anon()
    h = {"Authorization": f"Bearer {token}"}
    assert anon.get("/api/programs", headers=h).status_code == 200

    assert authed.delete(f"/api/tokens/{token_id}").status_code == 200
    assert anon.get("/api/programs", headers=h).status_code == 401


# ── The descriptor ────────────────────────────────────────────────────────────

def test_agent_descriptor_is_public(client):
    d = client.get("/api/agent").json()
    assert d["name"] == "piharness"
    assert d["authentication"]["token_prefix"] == auth.TOKEN_PREFIX
    assert set(d["authentication"]["scopes"]) == set(auth.TOKEN_SCOPES)
    assert d["mcp"]["server"].endswith("/agent/piharness_mcp.py")
    assert d["program_spec"].endswith("/api/prompt")


def test_mcp_server_file_is_downloadable(client):
    r = client.get("/agent/piharness_mcp.py")
    assert r.status_code == 200
    assert "PiHarness MCP server" in r.text


# ── MCP server ────────────────────────────────────────────────────────────────
# Driven as a subprocess over stdio, the way a real client runs it. Nothing is
# listening at the URL below, so tool calls exercise the error path; the
# handshake and the tool schemas are what matter here.

def _mcp(*messages, url="http://127.0.0.1:9", token="phk_test") -> list:
    stdin = "".join(json.dumps(m) + "\n" for m in messages)
    proc = subprocess.run(
        [sys.executable, str(MCP)], input=stdin, capture_output=True, text=True,
        timeout=60, env={"PATH": "/usr/bin:/bin", "PIHARNESS_URL": url,
                         "PIHARNESS_TOKEN": token, "PIHARNESS_TIMEOUT": "5"})
    assert proc.stderr == "", proc.stderr
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def test_mcp_handshake():
    out = _mcp({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "test", "version": "1"}}},
               {"jsonrpc": "2.0", "method": "notifications/initialized"})
    # The notification draws no response, so exactly one message comes back.
    assert len(out) == 1
    result = out[0]["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["serverInfo"]["name"] == "piharness"
    assert "tools" in result["capabilities"]


def test_mcp_negotiates_an_unknown_protocol_version():
    import importlib.util
    out = _mcp({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "1999-01-01", "capabilities": {}}})
    spec = importlib.util.spec_from_file_location("mcpmod", MCP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert out[0]["result"]["protocolVersion"] == mod.DEFAULT_PROTOCOL


def test_mcp_lists_tools_with_valid_schemas():
    tools = _mcp({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})[0]["result"]["tools"]
    names = {t["name"] for t in tools}
    assert {"list_programs", "import_program", "control_program", "program_logs",
            "remove_program", "set_secrets", "show_on_monitor"} <= names
    # No tool reads secret values back; the API would refuse it anyway.
    assert "get_secrets" not in names
    for t in tools:
        schema = t["inputSchema"]
        assert schema["type"] == "object"
        assert t["description"]
        for req in schema["required"]:
            assert req in schema["properties"], f"{t['name']} requires undeclared {req}"


def test_mcp_destructive_tool_needs_confirmation():
    result = _mcp({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "remove_program",
                              "arguments": {"name": "app"}}})[0]["result"]
    assert result["isError"] is True
    assert "confirm=true" in result["content"][0]["text"]


def test_mcp_reports_an_unreachable_harness_clearly():
    result = _mcp({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "list_programs", "arguments": {}}})[0]["result"]
    assert result["isError"] is True
    assert "Could not reach the harness" in result["content"][0]["text"]


@pytest.mark.parametrize("msg,expect", [
    ({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
      "params": {"name": "nope", "arguments": {}}}, "No such tool"),
    ({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
      "params": {"name": "get_program", "arguments": {"wrong": 1}}}, "Bad arguments"),
])
def test_mcp_bad_calls_come_back_as_tool_errors(msg, expect):
    result = _mcp(msg)[0]["result"]
    assert result["isError"] is True
    assert expect in result["content"][0]["text"]


def test_mcp_unknown_method_is_a_jsonrpc_error():
    assert _mcp({"jsonrpc": "2.0", "id": 9,
                 "method": "resources/list"})[0]["error"]["code"] == -32601


def test_mcp_survives_junk_input():
    proc = subprocess.run(
        [sys.executable, str(MCP)],
        input='not json\n{"jsonrpc":"2.0","id":1,"method":"ping"}\n',
        capture_output=True, text=True, timeout=60,
        env={"PATH": "/usr/bin:/bin", "PIHARNESS_URL": "http://127.0.0.1:9"})
    lines = [json.loads(x) for x in proc.stdout.splitlines() if x.strip()]
    # A parse error is reported, and the next valid message is still served.
    assert lines[0]["error"]["code"] == -32700
    assert lines[1]["result"] == {}
