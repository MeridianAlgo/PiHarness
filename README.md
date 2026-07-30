# PiHarness

Runs programs from GitHub on a Raspberry Pi and keeps them running.

You give it a repo. It clones the repo, installs the dependencies, writes a
systemd unit and starts it. The program stays up through crashes and reboots.
There's a web UI for managing them, links to any web UIs they serve, a place to
put secrets, over-the-air updates, and a kiosk mode for a monitor plugged into
the Pi.

- **Reach it from anywhere.** One click starts a Cloudflare tunnel: a public
  HTTPS address with no port forwarding, no static IP and nothing opened on your
  router. Bring your own domain, or take a throwaway address.
- **See what the Pi is doing.** CPU, temperature, memory and disk with ten
  minutes of trend, plus per-program memory, CPU time, uptime and restart count.
  Undervoltage and thermal throttling are called out, because they explain most
  Pi weirdness and are otherwise invisible.
- **Drive it from a script.** Revocable API tokens, rate limiting, and a
  documented HTTP API.
- **Hand it to an agent.** Scoped tokens plus a dependency-free MCP server, so
  Claude Code, Codex or any chatbot can import a repo, restart something and
  read the logs when it breaks. Read-only tokens can look without touching.

## Install

On the Pi:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/MeridianAlgo/PiHarness/main/installer/install.sh)
```

That installs Python, git and Node, optionally the kiosk packages, clones the
harness to `/opt/piharness` and starts `piharness.service` on port 8080. Open
`http://<your-pi>.local:8080` and create your account. That's the only time an
account can be created.

To skip the prompts:

```bash
HARNESS_PASSWORD='a-good-password' HARNESS_KIOSK=yes bash <(curl -fsSL …/install.sh)
```

## How it works

Each program is a clone under `/opt/piharness/programs/<name>` with its own
systemd unit, `harness-prog-<name>`, set to `Restart=always` and enabled at
boot. Units run at `Nice=15` / `CPUWeight=20`, so a busy program won't make the
Pi sluggish but still gets the whole CPU when nothing else wants it.

Dependencies come from the repo: `requirements.txt` goes into a private venv,
`package.json` gets `npm install --omit=dev`. The start command is detected from
`package.json`, `main.py` / `app.py` / `server.py`, or `index.js`, and you can
set it by hand if none of those fit.

Programs that serve a web page get a LAN link on their card. Turn on a
Cloudflare tunnel (or set `HARNESS_PUBLIC_URL`, or run Tailscale) and they also
get a link at `/apps/<name>/`, reverse-proxied through the harness. Those links
are **private by default**: they need a harness sign-in until you publish one
deliberately, because a tunnel makes "public" mean the whole internet. The proxy
strips the harness's own session cookie and API tokens from anything it forwards,
so an imported program can never see the credentials of whoever is browsing it.

Secrets are `KEY=VALUE` lines per program, stored `0600` at
`/etc/piharness/program-env/<name>.env` and injected as environment variables at
start. They aren't in the clone, so a `git pull` can't touch them, and the API
never returns them. A program can also write its own back — `HARNESS_TOKEN` in
its environment lets it `PATCH` its own secrets and nothing else, so a refreshed
OAuth token survives a restart instead of being re-negotiated every reboot.

Updates are per program. The harness can check GitHub and flag new commits for a
one-click update, apply them unattended every 6 hours, or stay out of the way
entirely for programs that update themselves. Pulls are `--ff-only`, so local
commits on the Pi don't get clobbered.

The harness updates itself from GitHub the same way, on demand: *Harness
updates* in the web UI, or `POST /api/update`. It pulls, reinstalls, and
restarts; the programs it supervises keep running through it.

For private repos, paste a GitHub access token at import time. It's kept in a
root-only registry and handed to git as environment config, so it never reaches
argv, `.git/config` or an API response.

## The kiosk

Plug a monitor into the Pi and any program with a web port can fill it, using
cage and Chromium with no desktop and no browser chrome. It comes back by itself
after a reboot. HDMI, DisplayPort and USB DisplayLink all work; the launcher
picks the display on every start, since card numbers move between boots.

One program at a time. Needs `cage`, `seatd` and `chromium-browser` installed.

## Cards

| Button | What it does |
|---|---|
| Start / Stop / Restart | Stop also disables start-on-boot; Start re-enables it |
| Update | `git pull --ff-only`, reinstall dependencies, restart |
| Secrets | Edit the program's environment variables. Saving restarts it |
| Logs | Tail of the unit's journal |
| Web port | The port its web UI listens on. Links and the kiosk re-point immediately |
| Monitor cmd | Optional command run every time the program goes on screen |
| Remove | Stops it and deletes its files. The GitHub repo is untouched |

The chips toggle the rest: public or private for the global link, show on
monitor, and the update mode.

## Making a repo importable

1. It has to be a long-running process, a server or a worker loop. A script that
   finishes becomes a crash loop under `Restart=always`.
2. Make the start command detectable: a `package.json` `start` script, a
   `main.py` / `app.py` / `server.py`, or an `index.js`. Otherwise set it on the
   card.
3. Declare dependencies in `requirements.txt` or `package.json`.
4. Read config from environment variables. Don't commit secrets.
5. If it serves a page, listen on the `PORT` env var and use relative asset
   URLs so it works through the `/apps/<name>/` proxy.
6. Write data inside its own folder, or wherever an env var points.

[docs/programs.md](docs/programs.md) covers all of this in detail. There's also
a **Copy prompt** button in the UI that hands the rules to an AI along with your
project, if you'd rather not restructure it yourself.

## Docs

- [docs/programs.md](docs/programs.md) — requirements, statuses, secrets,
  updates, the kiosk, and keeping a 24/7 program from cooking the Pi.
- [docs/agents.md](docs/agents.md) — token scopes, the MCP server, wiring up
  Claude Code and Codex, and what an agent can and can't reach.
- [docs/api.md](docs/api.md) — the HTTP API, for scripting the harness.

## Layout

```
harness/            the application
  config.py         paths and tunables, all env-overridable
  auth.py           argon2 credentials, sessions, scoped tokens, throttles
  programs.py       registry, git, systemd units, imports, OTA
  kiosk.py          the monitor kiosk
  api.py            HTTP routes and the /apps/<name>/ proxy
  main.py           FastAPI app, sign-in, background updater
agent/              MCP server, stdlib only, runs on the agent's machine
ui/                 web UI, no build step
installer/          install.sh, update.sh, piharness.service
docs/               programs.md, agents.md, api.md
tests/              pytest suite
```

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt

HARNESS_CONFIG_DIR=/tmp/ph/config \
HARNESS_PROGRAMS_DIR=/tmp/ph/programs \
HARNESS_UNIT_DIR=/tmp/ph/units \
  .venv/bin/python -m harness.main

.venv/bin/python -m pytest
```

The tests stub out git and systemctl, so they run anywhere. No Pi, no root.

## Updating the harness

From the web UI, under **Harness updates**: it shows the version you're on
against the latest on `main`, and one button applies it. Same thing from a
script or an agent:

```bash
curl -sS      $HARNESS/api/update -H "Authorization: Bearer $TOKEN"   # what's waiting
curl -sS -X POST $HARNESS/api/update -H "Authorization: Bearer $TOKEN"   # apply it
```

Applying pulls, reinstalls dependencies, re-renders the systemd unit keeping
your port, and restarts the service, so the UI drops for about a minute. Your
programs are separate units and keep running. The updater runs as its own
transient unit rather than as a child of the harness — it has to, since it
restarts the thing that started it — and its output survives at
`GET /api/update/logs` or `journalctl -u piharness-update`.

Over SSH, unchanged:

```bash
sudo /opt/piharness/installer/update.sh          # asks first
sudo /opt/piharness/installer/update.sh --auto   # unattended
sudo /opt/piharness/installer/update.sh --check  # exit 1 if an update is waiting
```

There's no unattended mode for the harness itself, deliberately: a supervisor
that auto-updates into a broken state takes the recovery UI down with it. Put
`--auto` on a systemd timer if you want one anyway. Imported programs update on
their own settings, separately from all of this.

## Trust

An imported program runs on your Pi as root, with the same privileges as the
harness. Only import repos you trust. The same goes for a `full` API token,
since it can import: treat one as roughly equivalent to a shell, and hand out
`read` tokens when the job is only diagnosis.

## License

MIT, see [LICENSE](LICENSE).
