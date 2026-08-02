#!/usr/bin/env python3
"""
PiHarness MCP server. Gives Claude Code, Codex, or any other MCP client tools
for driving a PiHarness box: import a repo, start and stop programs, read logs,
set secrets, put a program on the monitor.

This runs on YOUR machine, not on the Pi. It talks to the harness over HTTP.
Nothing to install: JSON-RPC over stdin and stdout with the standard library.

    export PIHARNESS_URL=http://piharness.local:8080
    export PIHARNESS_TOKEN=phk_...
    python3 piharness_mcp.py

Make a token in the harness web UI under API tokens, or:

    curl -sS -X POST $PIHARNESS_URL/api/tokens -b cookies.txt \\
      -H 'Content-Type: application/json' \\
      -d '{"label":"claude-code","scope":"full"}'

A read-scoped token can list, inspect and read logs but not change anything.
The write tools come back with a clear error in that case, rather than failing
silently.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

NAME = "piharness"
VERSION = "2.1.0"
# Versions whose shape this server matches. A client asking for one of these
# gets it echoed back; anything else is answered with our newest.
KNOWN_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL = "2025-06-18"

BASE = os.environ.get("PIHARNESS_URL", "http://piharness.local:8080").rstrip("/")
# PIHARNESS_KEY is accepted as an alias so an older config keeps working.
KEY = os.environ.get("PIHARNESS_TOKEN") or os.environ.get("PIHARNESS_KEY", "")
TIMEOUT = float(os.environ.get("PIHARNESS_TIMEOUT", "60"))


# ── Talking to the harness ────────────────────────────────────────────────────

class HarnessError(Exception):
    pass


def call_api(method: str, path: str, body=None, query=None):
    url = BASE + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": "application/json"}
    if data:
        headers["Content-Type"] = "application/json"
    if KEY:
        headers["Authorization"] = "Bearer " + KEY
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read().decode() or "{}"
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:600]
        try:
            detail = json.loads(detail).get("detail", detail)
        except ValueError:
            pass
        if e.code in (401, 403):
            raise HarnessError(
                f"{detail} (HTTP {e.code}). Check PIHARNESS_TOKEN, and that "
                f"its scope allows this.")
        raise HarnessError(f"{detail} (HTTP {e.code})")
    except urllib.error.URLError as e:
        raise HarnessError(
            f"Could not reach the harness at {BASE}: {e.reason}. Is PIHARNESS_URL "
            f"right and the Pi awake?")


# ── Tools ─────────────────────────────────────────────────────────────────────
# Each entry is (description, JSON schema properties, required, handler).
# A handler returns anything JSON-serialisable; it's rendered as pretty JSON.

def _programs():
    return call_api("GET", "/api/programs")


def t_list_programs():
    """Everything installed, trimmed to what's worth reading."""
    data = _programs()
    return {
        "monitor": data.get("monitor"),
        "programs": [
            {k: p.get(k) for k in
             ("name", "status", "repo_url", "start_command", "web_port", "ota",
              "on_monitor", "error")}
            for p in data.get("programs", [])
        ],
    }


def t_get_program(name):
    for p in _programs().get("programs", []):
        if p["name"] == name:
            return p
    raise HarnessError(f"No program named '{name}'. Use list_programs to see what exists.")


def t_import_program(repo, name=None, start_command=None, web_port=None,
                     ota="github", token=None):
    body = {"repo_url": repo, "ota": ota}
    for k, v in (("name", name), ("start_command", start_command),
                 ("web_port", web_port), ("token", token)):
        if v is not None:
            body[k] = v
    result = call_api("POST", "/api/programs", body)
    result["note"] = ("Cloning and installing run in the background. Call "
                      "get_program in a few seconds to see how it settled.")
    return result


def t_control_program(name, action):
    return call_api("POST", f"/api/programs/{urllib.parse.quote(name)}/action",
                    {"action": action})


def t_edit_program(name, start_command=None, web_port=None, ota=None,
                   public=None, monitor_command=None, clear_port=False):
    body = {}
    for k, v in (("start_command", start_command), ("web_port", web_port),
                 ("ota", ota), ("public", public),
                 ("monitor_command", monitor_command)):
        if v is not None:
            body[k] = v
    if clear_port:
        body["clear_port"] = True
    if not body:
        raise HarnessError("Nothing to change. Pass at least one field.")
    return call_api("PUT", f"/api/programs/{urllib.parse.quote(name)}", body)


def t_update_program(name):
    return call_api("POST", f"/api/programs/{urllib.parse.quote(name)}/update")


def t_check_updates():
    return call_api("GET", "/api/programs/updates")


def t_remove_program(name, confirm=False):
    if not confirm:
        raise HarnessError(
            f"This stops '{name}' and deletes its files from the Pi. The GitHub "
            f"repository is untouched. Call again with confirm=true to go ahead.")
    return call_api("DELETE", f"/api/programs/{urllib.parse.quote(name)}")


def t_program_logs(name, lines=80):
    return call_api("GET", f"/api/programs/{urllib.parse.quote(name)}/logs",
                    query={"lines": lines})


def t_list_files(name, path=""):
    return call_api("GET", f"/api/programs/{urllib.parse.quote(name)}/files",
                    query={"path": path})


def t_read_file(name, path):
    return call_api("GET", f"/api/programs/{urllib.parse.quote(name)}/file",
                    query={"path": path})


def t_write_file(name, path, content, restart=False):
    result = call_api("PUT", f"/api/programs/{urllib.parse.quote(name)}/file",
                      {"path": path, "content": content, "restart": restart})
    if not restart:
        result["note"] = ("Written to disk. The running program is still on the "
                          "old code until it restarts.")
    return result


def t_list_secret_names(name):
    """Names only. Values can't be read through an API key, by design."""
    return call_api("GET", f"/api/programs/{urllib.parse.quote(name)}/secret-names")


def t_set_secrets(name, env):
    result = call_api("PUT", f"/api/programs/{urllib.parse.quote(name)}/secrets",
                      {"env": env})
    result["note"] = "This replaced the whole file. It does not merge."
    return result


def t_patch_secrets(name, env, restart=False):
    result = call_api("PATCH", f"/api/programs/{urllib.parse.quote(name)}/secrets",
                      {"env": env, "restart": restart})
    result["note"] = "Only the named keys changed. Everything else was left alone."
    return result


def t_update_harness(check_only=True):
    if check_only:
        return call_api("GET", "/api/update")
    result = call_api("POST", "/api/update")
    result["note"] = ("The harness is restarting, so the API will be unreachable "
                      "for about a minute. Programs keep running. Call again with "
                      "check_only=true afterwards to confirm the new version, or "
                      "read harness_update_logs.")
    return result


def t_harness_update_logs(lines=80):
    return call_api("GET", "/api/update/logs", query={"lines": lines})


def t_show_on_monitor(name, on=True):
    return call_api("POST", f"/api/programs/{urllib.parse.quote(name)}/monitor",
                    {"on": on})


def _s(t, desc, **extra):
    return dict(type=t, description=desc, **extra)


TOOLS = {
    "list_programs": (
        "List every program on the PiHarness box with its live status, repo, "
        "web port and update mode.",
        {}, [], t_list_programs),

    "get_program": (
        "One program in full: status, start command, links, update mode, and "
        "any import error.",
        {"name": _s("string", "Program name.")}, ["name"], t_get_program),

    "import_program": (
        "Import a GitHub repository and start running it. The harness clones "
        "it, installs requirements.txt or package.json, and supervises it with "
        "Restart=always. The program must be long-running: a server or a worker "
        "loop, not a script that exits.",
        {"repo": _s("string", "owner/repo, or a full https://github.com/... URL."),
         "name": _s("string", "Name on the Pi. Defaults to the repo name."),
         "start_command": _s("string", "Leave unset to auto-detect from package.json, main.py, app.py, server.py or index.js."),
         "web_port": _s("integer", "Only if the program serves a web UI. 1024-65535."),
         "ota": _s("string", "github = flag new commits, auto = pull and restart unattended, self = the program updates itself.",
                   enum=["github", "auto", "self"]),
         "token": _s("string", "GitHub access token, for a private repo.")},
        ["repo"], t_import_program),

    "control_program": (
        "Start, stop or restart a program. Stop also disables start-on-boot; "
        "start re-enables it.",
        {"name": _s("string", "Program name."),
         "action": _s("string", "What to do.", enum=["start", "stop", "restart"])},
        ["name", "action"], t_control_program),

    "edit_program": (
        "Change a program's settings: start command, web port, update mode, "
        "whether its public link needs a sign-in, or its monitor command.",
        {"name": _s("string", "Program name."),
         "start_command": _s("string", "Runs from the program's folder via bash."),
         "web_port": _s("integer", "The port its web UI listens on."),
         "ota": _s("string", "Update mode.", enum=["github", "auto", "self"]),
         "public": _s("boolean", "false makes the global link require a sign-in."),
         "monitor_command": _s("string", "Runs each time the program goes on the monitor. Empty string clears it."),
         "clear_port": _s("boolean", "true removes the web port and its links.")},
        ["name"], t_edit_program),

    "update_program": (
        "Pull the latest commit for one program, reinstall its dependencies and "
        "restart it. Uses --ff-only, so local commits on the Pi are never "
        "clobbered.",
        {"name": _s("string", "Program name.")}, ["name"], t_update_program),

    "check_updates": (
        "Compare every program's installed commit against its GitHub HEAD, to "
        "see which ones have an update waiting.",
        {}, [], t_check_updates),

    "remove_program": (
        "Stop a program and delete its files from the Pi. The GitHub repository "
        "is untouched. Needs confirm=true.",
        {"name": _s("string", "Program name."),
         "confirm": _s("boolean", "Must be true. Without it the call is refused.")},
        ["name"], t_remove_program),

    "program_logs": (
        "The tail of a program's journal. The first place to look when "
        "something is failing or restarting.",
        {"name": _s("string", "Program name."),
         "lines": _s("integer", "How many lines. Default 80, capped at 400.")},
        ["name"], t_program_logs),

    "list_files": (
        "The files in a program's clone on the Pi, with their sizes. Skips "
        ".git, virtualenvs, node_modules and other build output.",
        {"name": _s("string", "Program name."),
         "path": _s("string", "Subdirectory to list. Defaults to the whole program.")},
        ["name"], t_list_files),

    "read_file": (
        "Read one text file from a program's clone on the Pi. Paths are "
        "relative to the program's directory.",
        {"name": _s("string", "Program name."),
         "path": _s("string", "Path within the program, e.g. src/main.py.")},
        ["name", "path"], t_read_file),

    "write_file": (
        "Write a file in a program's clone on the Pi, creating it and any "
        "missing folders. This is how you patch code in place. It writes the "
        "whole file, so read_file first and send back the complete new text. "
        "The edit is local to the Pi: an ota='github' or 'auto' program whose "
        "repo later moves on can't fast-forward over it, so commit the same "
        "change upstream if it should last.",
        {"name": _s("string", "Program name."),
         "path": _s("string", "Path within the program, e.g. src/main.py."),
         "content": _s("string", "The complete new contents of the file."),
         "restart": _s("boolean", "Restart the program so it runs the new code. Default false — leave it off until the last file of a multi-file edit.")},
        ["name", "path", "content"], t_write_file),

    "list_secret_names": (
        "The names of a program's secrets, without their values. Values cannot "
        "be read with an API key.",
        {"name": _s("string", "Program name.")}, ["name"], t_list_secret_names),

    "set_secrets": (
        "Replace a program's secrets with KEY=VALUE lines, injected as "
        "environment variables at start. This overwrites the whole file rather "
        "than merging, and restarts the program if it's running.",
        {"name": _s("string", "Program name."),
         "env": _s("string", "One KEY=VALUE per line. Blank lines and # comments allowed. Empty string deletes them all.")},
        ["name", "env"], t_set_secrets),

    "patch_secrets": (
        "Change individual secrets on a program without touching the others. "
        "Prefer this over set_secrets: values can't be read back, so replacing "
        "the whole file means guessing at what was already in it.",
        {"name": _s("string", "Program name."),
         "env": _s("object", "KEY -> new value. A null value deletes that key. Keys not named here are left alone."),
         "restart": _s("boolean", "Restart the program so it picks the values up. Default false.")},
        ["name", "env"], t_patch_secrets),

    "update_harness": (
        "Check for, or apply, an update to PiHarness itself from its GitHub "
        "repository. Applying pulls, reinstalls dependencies and restarts the "
        "harness; the programs it runs are unaffected and keep running.",
        {"check_only": _s("boolean", "true (the default) only reports whether an update is available. false applies it.")},
        [], t_update_harness),

    "harness_update_logs": (
        "The log from the last PiHarness self-update. Survives the restart the "
        "update causes, so this is how you find out whether it worked.",
        {"lines": _s("integer", "How many lines. Default 80, capped at 400.")},
        [], t_harness_update_logs),

    "show_on_monitor": (
        "Put a program's web UI fullscreen on the monitor plugged into the Pi, "
        "or clear the screen. One program at a time.",
        {"name": _s("string", "Program name."),
         "on": _s("boolean", "true to show it, false to turn the monitor off.")},
        ["name"], t_show_on_monitor),
}


def tool_list():
    return [{"name": name,
             "description": desc,
             "inputSchema": {"type": "object", "properties": props,
                             "required": req}}
            for name, (desc, props, req, _fn) in TOOLS.items()]


# ── JSON-RPC over stdio ───────────────────────────────────────────────────────

def _send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _result(msg_id, result):
    _send({"jsonrpc": "2.0", "id": msg_id, "result": result})


def _error(msg_id, code, message):
    _send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}})


def _text(payload, is_error=False):
    body = payload if isinstance(payload, str) else json.dumps(payload, indent=2)
    return {"content": [{"type": "text", "text": body}], "isError": is_error}


def handle(msg):
    """Returns a response dict, or None for a notification."""
    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        wanted = params.get("protocolVersion")
        version = wanted if wanted in KNOWN_PROTOCOLS else DEFAULT_PROTOCOL
        return ("result", msg_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": NAME, "version": VERSION},
            "instructions": (
                "Tools for a PiHarness box, which runs programs from GitHub on a "
                "Raspberry Pi and keeps them running. Programs must be "
                "long-running processes. Start with list_programs; when something "
                "misbehaves, read program_logs before changing anything."),
        })

    if method in ("notifications/initialized", "notifications/cancelled"):
        return None

    if method == "ping":
        return ("result", msg_id, {})

    if method == "tools/list":
        return ("result", msg_id, {"tools": tool_list()})

    if method == "tools/call":
        name = params.get("name")
        entry = TOOLS.get(name)
        if not entry:
            return ("result", msg_id, _text(f"No such tool: {name}", True))
        args = params.get("arguments") or {}
        try:
            return ("result", msg_id, _text(entry[3](**args)))
        except HarnessError as e:
            # A tool-level failure, reported in the result so the model can read
            # and act on it rather than the client treating it as a transport error.
            return ("result", msg_id, _text(str(e), True))
        except TypeError as e:
            return ("result", msg_id, _text(f"Bad arguments for {name}: {e}", True))
        except Exception as e:   # noqa: BLE001 - never take the server down
            return ("result", msg_id, _text(f"{type(e).__name__}: {e}", True))

    if msg_id is None:
        return None   # an unknown notification is ignored, per JSON-RPC
    return ("error", msg_id, (-32601, f"Method not found: {method}"))


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            _error(None, -32700, "Parse error")
            continue
        try:
            out = handle(msg)
        except Exception as e:   # noqa: BLE001
            _error(msg.get("id"), -32603, f"Internal error: {e}")
            continue
        if out is None:
            continue
        kind, msg_id, payload = out
        if kind == "result":
            _result(msg_id, payload)
        else:
            _error(msg_id, payload[0], payload[1])


if __name__ == "__main__":
    main()
