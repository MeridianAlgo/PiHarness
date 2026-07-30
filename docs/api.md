# HTTP API

Everything the web UI does goes through this API, so anything here can be
scripted. Interactive docs are served at `/docs` on a running harness.

## Authenticating

Scripts use an **API token**. Create one in the web UI under *API tokens*, or
over the API from a signed-in session, then send it as a bearer token:

```bash
HARNESS=http://piharness.local:8080
curl -sS $HARNESS/api/programs -H "Authorization: Bearer phk_…"
```

A token does not expire, survives a harness restart, and can be revoked on its
own without signing your browser out. It is shown once at creation and stored
only as a hash, so a lost token is replaced rather than recovered.

> **Changed in 2.0.** `/api/login` used to return the session token, and that
> token doubled as the bearer credential. It no longer does either: session
> tokens are rejected on the API with a 401 explaining why. Sessions are for
> browsers, tokens are for scripts. Anything scripted against 1.x needs a token.

The browser flow is unchanged: `/api/login` sets an HttpOnly session cookie, and
cookie-authenticated writes are checked against the request Origin for CSRF.

| Endpoint | Method | Auth | Purpose |
|---|---|---|---|
| `/api/status` | GET | none | Version, and whether first-run setup is still pending |
| `/api/setup` | POST | none, first run only | Create the single account: `{username, password}` |
| `/api/login` | POST | none | Sign in: `{username, password}`. Sets the session cookie |
| `/api/logout` | POST | session | Drop the current session |
| `/api/me` | GET | yes | The signed-in username |
| `/api/password` | POST | yes | `{current_password, new_password}`. Signs out everywhere |
| `/api/tokens` | GET | session | List token metadata. Never returns the tokens themselves |
| `/api/tokens` | POST | session | `{label, scope}` → `{token}`, shown exactly once |
| `/api/tokens/{id}` | DELETE | session | Revoke one token |
| `/api/agent` | GET | none | How to drive this harness: base URL, auth scheme, scopes, MCP server |

### Token scopes

`scope` is `full` (the default) or `read`. A `read` token may only use `GET`,
`HEAD` and `OPTIONS`; anything else is refused with `403`.

Three operations need a signed-in session and reject a token whatever its
scope: managing tokens, changing the password, and reading secret *values*.
Otherwise a token could widen its own scope or hand over every credential on
the Pi. See [agents.md](agents.md).

## Limits

Requests to `/api` are rate limited per client address: 240/min in general,
20/min against `/api/login` and `/api/setup`, both configurable. Over the limit
returns 429 with a `Retry-After` header. Request bodies above 1 MB are refused
with 413. Proxied program traffic under `/apps/` is not rate limited.

## System

| Endpoint | Method | Auth | Purpose |
|---|---|---|---|
| `/api/metrics` | GET | yes | Host health, sampled history, and per-program resource use |
| `/api/tunnel` | GET | yes | Cloudflare tunnel status |
| `/api/tunnel` | POST | yes | Start: `{mode: "quick" \| "named", token?, hostname?}` |
| `/api/tunnel` | DELETE | yes | Stop the tunnel and delete its stored token |
| `/api/tunnel/logs` | GET | yes | Journal tail for the tunnel (`?lines=`) |
| `/api/prompt` | GET | none | The spec for making a repo importable (see [programs.md](programs.md)) |

## Programs

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/programs` | GET | List programs with status and links, plus monitor state |
| `/api/programs` | POST | Import: `{repo_url, name?, start_command?, web_port?, monitor_command?, token?, ota?}` |
| `/api/programs/updates` | GET | Update check: local commit vs GitHub HEAD, per program |
| `/api/programs/{name}/action` | POST | `{action: "start" \| "stop" \| "restart"}` |
| `/api/programs/{name}` | PUT | Edit: `{start_command?, web_port?, monitor_command?, token?, public?, ota?, clear_port?}` |
| `/api/programs/{name}/update` | POST | Pull the latest code, reinstall deps, restart |
| `/api/programs/{name}` | DELETE | Stop and remove the program (the GitHub repo is untouched) |
| `/api/programs/{name}/logs` | GET | Journal tail (`?lines=`, capped at 400) |
| `/api/programs/{name}/monitor` | POST | `{on: true \| false}`, show on or clear the attached monitor |
| `/api/programs/{name}/secrets` | GET (session) / PUT | Read or replace the program's `KEY=VALUE` secrets |
| `/api/programs/{name}/secret-names` | GET | Secret names without their values. Usable with a token |
| `/apps/{name}/…` | any | Reverse proxy to the program's web port |

### A program in a listing

```json
{
  "name": "dashboard",
  "repo_url": "https://github.com/me/dashboard",
  "start_command": "npm start",
  "web_port": 3000,
  "public": true,
  "ota": "auto",
  "status": "active",
  "phase": null,
  "error": null,
  "created": "2026-07-30T09:14:02",
  "global_url": "https://pi.example.com/apps/dashboard/",
  "global_via": "configured",
  "on_monitor": true,
  "monitor_command": null,
  "has_token": false
}
```

`status` is one of `importing`, `active`, `inactive`, `failed`, `activating`,
`needs_command`, `error`, `unknown`. While `importing`, `phase` says which step
is running: `cloning`, `installing`, `starting`. A stored access token is never
returned, only `has_token`.

### Examples

Import a repo and give it a web port:

```bash
curl -sS -X POST $HARNESS/api/programs -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"repo_url":"me/dashboard","web_port":3000,"ota":"auto"}'
```

That returns immediately with `{"status":"importing","name":"dashboard"}`.
Cloning and installing happen in the background, so poll `GET /api/programs` to
watch it settle.

Restart one, then read its log:

```bash
curl -sS -X POST $HARNESS/api/programs/dashboard/action -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"action":"restart"}'

curl -sS "$HARNESS/api/programs/dashboard/logs?lines=200" -H "Authorization: Bearer $TOKEN"
```

Replace a program's secrets (this replaces the whole file, it doesn't merge):

```bash
curl -sS -X PUT $HARNESS/api/programs/dashboard/secrets -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"env":"API_KEY=abc123\nREGION=eu-west-1\n"}'
```

## Errors

Failures come back as `{"detail": "…"}`. `400` is a malformed request, `401`
authentication, `403` a blocked cross-origin write, `404` an unknown program,
`409` wrong state (duplicate name, missing start command, kiosk packages not
installed), `429` a throttled login, and `502` a program whose web port isn't
answering through the proxy.

## Cookies and CSRF

The browser UI uses an `HttpOnly` session cookie instead of a bearer token. For
those requests the harness checks the origin on every `POST`, `PUT`, `PATCH` and
`DELETE`, and rejects a cross-site `Origin` or `Referer` with `403`.
Bearer-token callers carry no cookie and aren't affected, which is why scripts
should use the token.

## Configuration

Environment variables, read at startup (put them in `/etc/piharness/env`):

| Variable | Default | Meaning |
|---|---|---|
| `HARNESS_PORT` | `8080` | Port the harness listens on; also the port programs may not claim |
| `HARNESS_CONFIG_DIR` | `/etc/piharness` | Credentials, registry, secrets, kiosk state |
| `HARNESS_PROGRAMS_DIR` | `/opt/piharness/programs` | Where program repos are cloned |
| `HARNESS_UNIT_DIR` | `/etc/systemd/system` | Where program units are written |
| `HARNESS_PUBLIC_URL` | *(empty)* | Public origin for `/apps/<name>/` links. Falls back to Tailscale autodetection |
| `HARNESS_SESSION_TTL` | `24` | Session lifetime, in hours |
| `HARNESS_COOKIE_SECURE` | `0` | Set to `1` when serving HTTPS directly, so the cookie gets `Secure` |
| `HARNESS_AUTO_UPDATE_INTERVAL` | `21600` | Seconds between unattended update checks |
| `HARNESS_CORS_ORIGINS` | *(empty)* | Comma-separated extra origins allowed to call the API with cookies |
