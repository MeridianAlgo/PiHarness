# Agent access

Give a chatbot or a coding agent its own token and it can drive the harness:
import a repo, start and stop programs, read logs when something is failing,
set secrets, put a program on the monitor.

There are two ways in. MCP is the better one if your agent supports it, since
the tools come with descriptions and the agent works out the rest. Plain HTTP
works with anything that can make a request.

## Tokens

A browser session expires and dies when the harness restarts, which is fine for
a browser and useless for an agent. API tokens don't expire, survive restarts,
and are revoked one at a time.

Make one in the web UI under **API tokens**, or over the API with a signed-in
session:

```bash
curl -sS -X POST $HARNESS/api/tokens -b cookies.txt \
  -H 'Content-Type: application/json' \
  -d '{"label":"claude-code","scope":"full"}'
```

The token comes back once, in that response, and is stored only as a SHA-256
hash. If you lose it, revoke it and make another.

Two scopes:

| Scope | Can do |
|---|---|
| `read` | `GET` only. List programs, read logs, check for updates, watch metrics. Anything that would change something is refused with a 403 that says so. |
| `full` | Everything the web UI can do, except the three things below. |

Three things no token can do, whatever its scope:

- **Read secret values.** A token can set secrets and list their names, but
  `GET /api/programs/{name}/secrets` needs a signed-in session. A leaked token
  can't be used to harvest every credential on the Pi.
- **Manage tokens.** Creating, listing and revoking tokens needs a session too.
  Otherwise an agent holding a `read` token could mint itself a `full` one.
- **Change the password.** Same reason.

Give an agent `read` when you want it to diagnose but not act, and `full` when
you want it to fix things. Use a separate token per agent, so revoking one
doesn't disturb the others, and the "used" column tells you which is which.

## MCP

`agent/piharness_mcp.py` is an MCP server that runs **on your machine**, not on
the Pi, and talks to the harness over HTTP. It's a single file with no
dependencies beyond the Python standard library, so there's nothing to install.

Download it from your own harness:

```bash
curl -O http://piharness.local:8080/agent/piharness_mcp.py
```

### Claude Code

```bash
claude mcp add piharness \
  --env PIHARNESS_URL=http://piharness.local:8080 \
  --env PIHARNESS_TOKEN=phk_... \
  -- python3 "$PWD/piharness_mcp.py"
```

### Codex

In `~/.codex/config.toml`:

```toml
[mcp_servers.piharness]
command = "python3"
args = ["/full/path/to/piharness_mcp.py"]
env = { PIHARNESS_URL = "http://piharness.local:8080", PIHARNESS_TOKEN = "phk_..." }
```

### Anything else

The server speaks JSON-RPC over stdin and stdout, so any MCP client can run it.
It reads three environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `PIHARNESS_URL` | `http://piharness.local:8080` | Where the harness is |
| `PIHARNESS_TOKEN` | *(empty)* | Your API token (`PIHARNESS_KEY` also accepted) |
| `PIHARNESS_TIMEOUT` | `60` | Seconds to wait on a request |

You can check it works without an agent:

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
  | PIHARNESS_URL=http://piharness.local:8080 PIHARNESS_TOKEN=phk_... \
    python3 piharness_mcp.py
```

### Tools

| Tool | What it does |
|---|---|
| `list_programs` | Everything installed, with status, repo, port and update mode |
| `get_program` | One program in full, including any import error |
| `import_program` | Clone a GitHub repo and start running it |
| `control_program` | Start, stop or restart |
| `edit_program` | Change start command, port, update mode, link visibility, monitor command |
| `update_program` | Pull the latest commit, reinstall dependencies, restart |
| `check_updates` | Which programs have a newer commit on GitHub |
| `remove_program` | Stop and delete. Requires `confirm=true` |
| `program_logs` | Journal tail. The first place to look when something fails |
| `list_secret_names` | Secret names, never values |
| `set_secrets` | Replace a program's secrets |
| `show_on_monitor` | Put a program fullscreen on the Pi's screen, or clear it |

`remove_program` refuses without `confirm=true`, and says what it would delete.
That's deliberate: it's the one tool that destroys work, and an agent that
misreads a request shouldn't be one call away from wiping a program.

Failures come back as tool results with `isError: true` and a message the model
can act on, rather than as transport errors. A read-scoped token trying to write
gets told exactly that.

## Plain HTTP

Any chatbot that can make an HTTP call can use the API with the same key. Point
it at the OpenAPI document and it can work out the rest:

```
http://piharness.local:8080/openapi.json
```

`GET /api/agent` is a smaller, unauthenticated summary: base URL, how the
bearer scheme works, what the scopes mean, where the MCP server lives, and where
to fetch the program spec. It's a reasonable thing to paste into a system
prompt.

Every call is one header:

```bash
curl -sS http://piharness.local:8080/api/programs \
  -H "Authorization: Bearer phk_..."
```

Note that a session token is *not* accepted as a bearer credential. Tokens and
sessions are separate; the API takes tokens.

See [api.md](api.md) for the full endpoint list.

## Reaching the Pi from outside

The examples above assume the agent is on your network. If it isn't, don't put
the harness straight on the internet. Either run Tailscale on both ends and use
the tailnet address, or front it with a tunnel and set `HARNESS_PUBLIC_URL` so
links point at the right host. Set `HARNESS_COOKIE_SECURE=1` if you terminate
TLS at the harness itself.

## What an agent can do to you

A token with `full` scope can import a program, and importing a program runs
its code on your Pi as root. That is the whole point of the harness, and it
means a `full` token is roughly as powerful as a shell.

So:

- Prefer `read` when the job is diagnosis. Most "why is this broken" work needs
  nothing more.
- One token per agent, labelled for what it's for.
- Revoke instead of rotating credentials. Revocation takes effect immediately.
- The tokens list shows when each was last used. An unexpected timestamp is
  worth a look.
