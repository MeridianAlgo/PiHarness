# Writing a program for the harness

Import a program from GitHub and PiHarness keeps it running on the Pi, in the
background, through crashes and reboots.

If you're making a repo importable, this page is the only one you need. Hand it
to a developer or an AI as-is. [api.md](api.md) is a different thing: it's for
driving the harness from a script, not for writing a program.

## The AI prompt

Paste this into any AI along with your code, or click **Copy spec** in the web
UI. What comes back imports cleanly.

The harness serves this same text at `GET /api/prompt`, unauthenticated, so you
can pipe it straight into a tool:

```bash
curl -sS http://piharness.local:8080/api/prompt | python3 -c 'import json,sys; print(json.load(sys.stdin)["prompt"])'
```

```text
Convert this project into an always-on program that PiHarness can import from
GitHub and supervise on a Raspberry Pi. PiHarness clones the repo, installs the
dependencies it declares, writes a systemd unit with Restart=always, and runs it
24/7. Apply ALL of the following, then tell me what you changed.

1. LONG-RUNNING PROCESS
   The entry point must be a process that stays alive: a server, or a worker with
   its own scheduling loop. It must never exit on success. The supervisor
   restarts anything that exits, so a script that finishes its work and returns 0
   becomes a restart loop that burns the CPU forever. If the work is periodic, do
   NOT rely on cron or a one-shot run: sleep inside the process between cycles.
   Exiting non-zero on an unrecoverable error is correct and expected; it will be
   restarted after 5 seconds.

2. START COMMAND
   Make it auto-detectable, in this order of preference:
     - Node: a "start" script in package.json, or index.js at the repo root
     - Python: main.py, app.py or server.py at the repo root
   If none fits, state the exact one-line command in the README. It runs from the
   repo root through `bash -lc`, as root, with no shell aliases available.

3. DEPENDENCIES
   Declare every dependency in requirements.txt (Python; installed into a private
   venv next to the clone) or package.json (Node; `npm install --omit=dev`).
   Nothing else is installed. Do not assume system packages, a global pip, a
   compiler toolchain, or anything preinstalled beyond python3, node and git.
   Prefer pure-Python wheels: the Pi is ARM and building from source is slow.

4. CONFIGURATION AND SECRETS
   Read every key, token, path and tunable from environment variables
   (os.environ / process.env), each with a sensible default where one exists.
   Never commit a secret and never read one from a file in the repo: PiHarness
   injects them as environment variables from storage outside the clone. Fail
   fast and loudly at startup if a required variable is missing, naming the
   variable.

   If a credential CHANGES while the program runs — an OAuth access token you
   refresh, a session key a service reissues, a device registration — write it
   back, or it is lost on the next restart and you re-authenticate every time.
   PiHarness gives every program three variables for exactly this:

     HARNESS_URL      where the harness listens, e.g. http://127.0.0.1:8080
     HARNESS_PROGRAM  this program's name in the harness
     HARNESS_TOKEN    a token scoped to this program and nothing else

   PATCH {HARNESS_URL}/api/programs/{HARNESS_PROGRAM}/secrets with
   `Authorization: Bearer {HARNESS_TOKEN}` and a body of
   {"env": {"KEY": "new value"}}. Only the keys you name change; a null value
   deletes one. Do NOT pass {"restart": true} when saving your own credential —
   you already hold the new value, and restarting yourself here is an infinite
   loop. Treat the three variables as absent (skip the write-back, keep working)
   rather than failing to start: they are missing when the program is run
   outside PiHarness.

5. IF IT SERVES A WEB UI
   Listen on 0.0.0.0 at the port in the PORT environment variable, falling back
   to a fixed default that you state in the README. The app is also reverse
   proxied at /apps/<name>/, so:
     - use relative URLs for every asset, link and API call (no absolute /static/…
       paths, which break under the prefix)
     - plain HTTP request/response only. WebSockets and server-sent events do not
       pass through the proxy; poll instead if you need live updates
   A web UI is optional. A headless worker is a first-class program.

6. LOGGING
   Write to stdout and stderr, unbuffered, and nothing else. Do not write log
   files, do not rotate logs, do not require a log directory. The output is
   captured by journald and shown in the PiHarness UI. In Python, either set
   PYTHONUNBUFFERED or call print(..., flush=True); a buffered process appears
   silent for minutes, which looks like a hang.

7. SHUTDOWN
   Handle SIGTERM: stop accepting work, finish or abandon what is in flight, and
   exit within a few seconds. systemd sends SIGTERM on stop, restart and update,
   then SIGKILLs after a timeout. Anything not flushed by then is lost.

8. STATE ON DISK
   Write files only to paths inside the app's own directory (use relative paths
   or a directory from an env var). The clone is the working directory. Anything
   written elsewhere may be outside the backup and will not survive a re-import.
   Be aware that `git pull` runs on update: never write into a tracked path, or
   the update will conflict and fail.

9. RESOURCE BEHAVIOUR
   This shares a Pi with other programs. Do not busy-wait, do not poll a remote
   API more than once a minute without a reason, and do not hold large data
   structures in memory. The unit runs at Nice=15 with a reduced CPU weight, so
   a greedy loop will not freeze the Pi, but it will heat it and throttle
   everything. Sleep between cycles.

10. FINALLY, TELL ME
    - the GitHub repository to import
    - the start command, if it is not auto-detectable
    - the web port, if it serves a web UI
    - every environment variable it needs, which are required, and what each does
```

## Requirements

| Requirement | Details |
|---|---|
| Long-running process | The start command has to keep running: a server, a worker loop, a bot. One-shot scripts exit immediately and systemd restarts them forever, which is a crash loop, not a program. |
| Runs in the background | Each program gets its own systemd unit (`harness-prog-<name>`) with `Restart=always`, started on boot. No terminal session required. |
| Declared dependencies | `requirements.txt` (Python, installed into a per-program `.venv`) or `package.json` (Node, `npm install --omit=dev`). Anything else has to already be on the Pi. |
| Config in environment variables | Don't commit keys or tokens. Read them from `os.environ` / `process.env` and set them in the Secrets store. |
| Web UI, optional | If the program serves a page, give it a web port at import time. Headless programs are fine; they just show status and logs. |

## Importing

Paste a GitHub URL (`https://github.com/owner/repo`, or just `owner/repo`) into
**Import from GitHub**. The harness clones it to
`/opt/piharness/programs/<name>`, installs declared dependencies, writes the
systemd unit and starts it.

The start command is detected if you leave it blank, in this order:

1. `package.json` with a `start` script → `npm start`
2. `main.py` / `app.py` / `server.py` → `python3 <file>` (the `.venv` python when there's a `requirements.txt`)
3. `index.js` → `node index.js`

If nothing matches, the program lands in **Needs command**. Click *Set start
command* on its card. It runs from the program's folder via `bash -lc`, so
anything you could type in a shell works.

### Private repositories

Open **Options** on the import form and paste a GitHub access token with read
access to the repo. A fine-grained `github_pat_…` scoped to that one repository
is best; a classic `ghp_…` works too. The clone, the update checks and every
**Update** pull use it.

Where the token goes:

- Into the harness's root-only registry (`0600`) on the Pi. Not into the clone's
  `.git/config`, not into the stored repo URL, and not into the process list.
  It reaches git as base64 environment config.
- Never back out through the API. Cards show a **Private repo** chip instead.
  Click it to replace the token after rotating it on GitHub, or to remove it.
- Deleted with the program.

Programs imported before you had a token can get one later. Click **Add token**
on the card, or `PUT /api/programs/{name}` with `{token}`. Update checks and
pulls start authenticating right away.

## Statuses

| Status | Meaning |
|---|---|
| Importing… | Cloning, installing dependencies or starting. The card shows which. |
| Running | The systemd unit is active. |
| Stopped | Stopped by you. Stop also disables start-on-boot; Start re-enables it. |
| Failed | The process keeps exiting. Check **Logs** on the card. |
| Needs command | Imported, but no start command was detected or set. |
| Import failed | The clone or the dependency install failed, with the error on the card. Remove and re-import after fixing it. |

## Web UIs and the global link

A program listening on its web port (which is also handed to it as the `PORT`
environment variable, so honor that if you can) gets:

- A LAN link, `http://<pi-address>:<port>`, for devices on your network.
- A global link, `https://<host>/apps/<name>/`, proxied through the harness.

The host for the global link is resolved in this order:

1. `HARNESS_PUBLIC_URL`, if you set one.
2. A **Cloudflare tunnel**, if one is running. Turn it on under *Remote access*
   in the web UI. This needs no port forwarding, no static IP and no inbound
   hole in your router: `cloudflared` dials out to Cloudflare and traffic comes
   back down that connection.
3. Tailscale, if it's running, in which case the link works on any device signed
   in to your tailnet. That link is `http://<node>:8080` — Tailscale routes to
   the machine but does not terminate TLS or forward 443, so there is nothing
   listening on an `https://` origin unless you run `tailscale serve` yourself.
   If you do, set `HARNESS_PUBLIC_URL` to say so; it outranks this.

Two tunnel modes:

| Mode | Needs | Address |
|---|---|---|
| Quick | nothing | A random `*.trycloudflare.com` name, **regenerated every restart**. Fine for a look, useless as a bookmark. |
| Named | A Cloudflare account and a connector token | Your own hostname, stable across restarts and reboots. |

For a named tunnel, create one in Cloudflare Zero Trust under *Networks →
Tunnels*, route your hostname to `http://localhost:8080`, and paste the
connector token into *Remote access*. The token is stored at
`/etc/piharness/tunnel.env` with mode 0600 and passed to `cloudflared` as an
environment variable, so it never appears in the unit file or in `ps` output.

> **A program is private until you publish it.** A private program's global link
> requires a harness sign-in; only when you flip the chip on its card to
> **Public** does the link work without one. This is the opposite of the 1.x
> default, and it changed because a tunnel makes "public" mean *the entire
> internet* rather than *anyone already on your LAN*. Existing programs that
> were relying on the old default need the chip flipped once after upgrading.

Two things the global link can't do that the LAN link can:

- Serve pages that reference assets by absolute path. `/static/app.js` will 404
  under `/apps/<name>/`. Use relative paths or make the base path configurable.
  The proxy sets `X-Forwarded-Prefix`, so a framework that understands it can
  build correct URLs on its own.
- Carry WebSockets or server-sent events. Plain HTTP only.

The proxy strips the harness's own session cookie and API tokens from anything
it forwards, so a program can never see the credentials of the person browsing
it. Your program's own cookies and `Authorization` headers pass through
untouched.

## Show on a monitor

Plug a monitor into the Pi and any program with a web port gets a **Show on
monitor** chip. Click it and the program's web UI opens fullscreen: no browser
bars, no desktop. Click **On monitor** to turn it off.

- One-time setup: `sudo apt install cage seatd chromium-browser`. The installer
  offers to do it. The chip tells you if something's missing.
- The chip is there whether or not a monitor is plugged in. With none attached,
  turning it on arms the kiosk and the program appears when you plug one in.
- One program on the screen at a time. Showing a new one replaces the old one.
- It survives reboots. The Pi boots straight back into the program's screen, no
  SSH session or manual command involved.
- No keyboard or mouse needed, though a plugged-in one works for a touchscreen
  dashboard. Turning the kiosk on pulls the program's service up if it isn't
  running, then waits up to 3 minutes for its port to answer before opening the
  browser, so a slow starter shows its page instead of an error. When the
  program on screen is updated or restarted, the kiosk restarts with it.
- It renders `http://127.0.0.1:<port>/` locally, with no login page and no proxy
  path, so absolute asset paths that break the global link are fine here.
- Removing the program or clearing its web port turns the kiosk off. Changing
  the port re-points it.
- USB DisplayLink monitors work, not just HDMI. The launcher checks what's
  attached on every start. For HDMI and DisplayPort, cage drives the connected
  card directly. For DisplayLink, cage renders on the board's GPU and scans out
  on the USB device, found via `/dev/dri/by-path` so plug order can't break it
  across reboots. DisplayLink also needs its driver:
  [displaylink-debian](https://github.com/AdnanHodzic/displaylink-debian) for
  DL-3xxx and newer chips, which is most USB 3.0 monitors.
- Some programs need a command run every time they go on screen: warm a cache,
  regenerate the page, poke an API. Click **Monitor cmd** and enter it. It runs
  from the program's folder via bash on every kiosk start, as its own
  short-lived unit (`harness-kiosk-cmd`, capped at 2 minutes) detached from the
  kiosk, so a command that fails or hangs can't hold the screen black or block a
  restart. Output goes to `journalctl -u harness-kiosk-cmd`.

## Secrets

GitHub repository secrets live in Actions and never leave GitHub, so they aren't
cloned with your code. The harness has its own store for the same job. Click
**Secrets** on a card and enter one `KEY=VALUE` per line. Blank lines and `#`
comments are fine.

- One file per program at `/etc/piharness/program-env/<name>.env`, mode `0600`.
  Not inside the cloned repo folder, so a `git pull` or an **Update** can't
  touch or leak them.
- Injected as environment variables when the program starts, via the unit's
  `EnvironmentFile`. Your code just reads `os.environ["API_KEY"]` or
  `process.env.API_KEY`.
- They stay on the Pi. Not synced anywhere, never returned by
  `GET /api/programs`, deleted when the program is removed.
- Saving restarts a running program so it picks up the new values.

### A program writing its own secrets

Reading is the easy half: the values arrive as environment variables and your
code just reads them. The hard half is a credential that *changes while the
program runs* — an OAuth access token you refresh every hour, a session key the
service reissues, a device registration. Keep it in memory only and you lose it
on the next restart, then re-authenticate from scratch every time the Pi
reboots.

So every program gets three variables of its own, on top of its secrets:

| Variable | What it is |
|---|---|
| `HARNESS_URL` | Where the harness listens, e.g. `http://127.0.0.1:8080`. |
| `HARNESS_PROGRAM` | The program's name in the harness. |
| `HARNESS_TOKEN` | A token scoped to this one program. |

`PATCH /api/programs/<name>/secrets` merges the keys you name into the file and
leaves everything else — other keys, ordering, your comments — untouched:

```python
import os, json, urllib.request

def save_secret(**values):
    """Persist a rotated credential so it survives a restart. No-op when the
    program isn't running under PiHarness."""
    url, name = os.environ.get("HARNESS_URL"), os.environ.get("HARNESS_PROGRAM")
    token = os.environ.get("HARNESS_TOKEN")
    if not (url and name and token):
        return False
    req = urllib.request.Request(
        f"{url}/api/programs/{name}/secrets",
        data=json.dumps({"env": values}).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        method="PATCH")
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status == 200

# after refreshing an OAuth token
save_secret(ACCESS_TOKEN=new_access, REFRESH_TOKEN=new_refresh)
```

Node, same thing:

```js
async function saveSecret(values) {
  const {HARNESS_URL, HARNESS_PROGRAM, HARNESS_TOKEN} = process.env;
  if (!HARNESS_URL || !HARNESS_PROGRAM || !HARNESS_TOKEN) return false;
  const r = await fetch(`${HARNESS_URL}/api/programs/${HARNESS_PROGRAM}/secrets`, {
    method: 'PATCH',
    headers: {Authorization: `Bearer ${HARNESS_TOKEN}`,
              'Content-Type': 'application/json'},
    body: JSON.stringify({env: values}),
  });
  return r.ok;
}
```

Things worth knowing before you wire this up:

- **Don't restart yourself.** The call takes an optional `"restart": true`,
  which defaults to `false`, and a program saving its own credential must leave
  it that way. You already hold the new value in memory; restarting to pick it
  up drops you into a rotate-restart-rotate loop.
- **It's a merge, not a replace.** `{"env": {"ACCESS_TOKEN": "…"}}` changes that
  one key. `null` as a value deletes a key. This matters because a program can't
  read its own values back, so it has nothing to build a full replacement from.
- **The token only reaches this.** `HARNESS_TOKEN` is bound to its own program.
  It can't list programs, read logs, start anything, or touch another program's
  secrets — those all return 403. So a program that gets compromised can rewrite
  its own credentials and nothing else on the Pi.
- **It can't read values back**, only write. Nothing can read them except a
  signed-in browser session; that's unchanged.
- **Handle the variables being absent.** They're missing when someone runs your
  repo on their laptop. Skip the write-back and carry on, don't refuse to start.
- The variables live in `/etc/piharness/program-env/<name>.harness.env` (0600),
  a separate file from your secrets, so saving in the Secrets editor can't
  clobber them. Delete the file and the harness mints a fresh token on its next
  restart.

## Updates

Every program has one of three update modes. Pick one at import time (Options →
Updates) or click the `OTA · …` chip on the card to cycle through them.

**GitHub** is the default. The harness compares your installed copy against the
repository's latest commit with `git ls-remote`. When GitHub is ahead the card
shows an **Update available** badge, and clicking **Update** does
`git pull --ff-only`, reinstalls dependencies and restarts the program.

**Auto** does the same check and applies it for you. A background task runs
every 6 hours and pulls, reinstalls and restarts any auto-mode program whose
GitHub HEAD has moved. Push to `main` and the Pi is running the new code by the
next cycle. Good for your own programs where `main` is always deployable; stay
on GitHub mode where you want to look at a change before it lands.

**Self-managed** is for programs that ship their own updater or update from
somewhere other than the public repo. The harness stops checking and hides the
Update button. Applying an update is easy from the program's side: swap the
files and exit, and `Restart=always` brings it back on the new code.

`--ff-only` means an update never force-resets your clone. If you've committed
changes on the Pi that GitHub can't fast-forward over, the pull is skipped and
logged and the program keeps running, rather than losing your work.

### Updating the harness itself

The harness is a git clone from GitHub too, and updates the same way: **Harness
updates** in the web UI shows the version you're on against the latest on
`main`, and **Update harness** applies it.

Applying pulls, reinstalls the harness's own dependencies, re-renders the
systemd unit (keeping whatever port you set) and restarts the service. The web
UI stops answering for about a minute and comes back on the new version. Your
programs are separate units and keep running throughout.

Same thing from a script or an agent, with a `full`-scoped token:

```bash
curl -sS -H "Authorization: Bearer $TOKEN" http://piharness.local:8080/api/update
curl -sS -X POST -H "Authorization: Bearer $TOKEN" http://piharness.local:8080/api/update
```

Notes:

- The updater runs as its own transient systemd unit, not as a child of the
  harness. It has to: it restarts the harness, and a child would be killed
  along with the service it was restarting, leaving the install half-applied.
  That's also why the response comes back before the update has finished.
- Read `GET /api/update/logs` (or **Update log** in the UI, or
  `journalctl -u piharness-update`) afterwards to see what happened. It survives
  the restart.
- Rollback is printed in that log as a `git checkout <sha>` line for the commit
  you were on.
- There is no unattended mode for the harness, on purpose — an auto-updating
  supervisor that breaks itself takes the recovery UI down with it. If you want
  one anyway, `installer/update.sh --auto` is safe to put on a systemd timer or
  in cron; `--check` exits 1 when an update is available.
- Over SSH, unchanged: `sudo /opt/piharness/installer/update.sh`.
- `HARNESS_BRANCH` picks a branch other than `main` for both the check and the
  pull.

A `full` token can apply a harness update. That's deliberate rather than an
oversight: a token that can import a repository can already run arbitrary code
on the Pi as root, so withholding self-update from it buys nothing and costs
you the ability to update from an agent. `read` tokens can check but not apply.

## Resource use

A program you import runs continuously, so a small inefficiency runs 24/7. On a
Pi that shows up as high CPU and a hot SoC.

### What the harness already does

Every program runs under a unit that de-prioritises it against the harness and
the OS:

```
Nice=15        # lowest CPU scheduling priority, background work
CPUWeight=20   # ~1/5 of the default share when something else wants the CPU
IOWeight=20    # same idea for disk I/O
```

That's a soft limit. When nothing else is busy a program still gets the whole
CPU, because nice defers rather than caps. So it keeps the Pi responsive under
contention and stops a background app from making the UI stutter, but it won't
lower your temperature if the program itself is burning cycles. That part is up
to how the program is written. The harness reapplies these settings to existing
programs on its next restart, so you never have to re-import anything.

### Writing one that idles cheaply

The big one: an always-on program should spend almost all its time asleep.

- Never busy-wait. A bare `while True:` with no sleep pegs a core at 100%. Put a
  `time.sleep()` or `await asyncio.sleep()` between iterations.
- Poll as slowly as the job tolerates. 15 to 60 seconds covers most things. To
  react faster, use a push (webhook, long-poll) rather than a tight poll. One
  blocked-on-read connection costs nothing; a 1-second poll wakes the CPU 86,400
  times a day.
- Make polls cheap. Send `If-None-Match` or `If-Modified-Since` and let the
  server answer `304 Not Modified` when nothing changed.
- Back off on errors. When an API is down, sleep longer between retries: 5s,
  10s, 30s. Hammering in a tight loop is the classic runaway-CPU-and-heat bug.
- Only do work when the input changed. Cache the last result and skip the
  expensive part when a new poll matches it.
- Watch files with `inotify` instead of re-scanning a directory on a timer.
- Don't auto-refresh a dashboard hard. Re-fetching every second keeps both the
  Pi and the viewing device warm.

### Finding the culprit

```bash
systemd-cgtop            # live CPU/memory per unit, spot the hungry harness-prog-*
top -o %CPU              # per-process; press 'c' for full command lines
vcgencmd measure_temp    # current SoC temperature
journalctl -u harness-prog-<name> -f   # is it erroring in a tight retry loop?
```

### Hard caps

If a program genuinely needs a ceiling, add a systemd drop-in. This is a hard
cap, which the soft `Nice`/`CPUWeight` above deliberately isn't:

```bash
sudo systemctl edit harness-prog-<name>
```

```ini
[Service]
CPUQuota=40%      # never use more than 40% of one core
MemoryMax=256M    # killed and restarted if it exceeds this
```

`CPUQuota` limits heat directly, at the cost of the program running slower.
Don't put it on something that needs bursts of full CPU to keep up, or it'll
fall behind. The harness leaves this per program rather than capping everything,
because a blanket cap breaks the programs that legitimately need a whole core.

## Security

- Only `https://github.com/…` repositories are accepted, and program names are
  limited to `a-z 0-9 . _ -`, so nothing can be smuggled into `git clone`, unit
  names or paths.
- Importing a program means running its code on your Pi as root, with the same
  privileges as the harness. Only import repositories you trust. It's the same
  decision as pasting a script into a terminal.
- The `/apps/` proxy only connects to `127.0.0.1:<the registered port>`. It
  can't be pointed at other hosts or unregistered ports.
- The registry is at `/etc/piharness/programs.json` (0600, since it can hold
  access tokens), code at `/opt/piharness/programs/`, secrets at
  `/etc/piharness/program-env/` (0600).
- Each program's own harness token is in
  `/etc/piharness/program-env/<name>.harness.env` (0600), not in its unit file,
  which is world-readable. It is bound to that program: it can merge that
  program's secrets and nothing else, and it is revoked when the program is
  removed.
- Access tokens reach git as in-memory environment config, so they never touch
  the clone's `.git/config`, the stored repo URL, process argv or an API
  response.

### The boundary around a proxied program

A program is third-party code served from the harness's own origin, so the
proxy treats it as untrusted in both directions:

- The harness session cookie and any harness API token are **removed** from
  requests before they reach a program. Without this, any imported program could
  read `harness_session` from the forwarded `Cookie` header and act as you. The
  program's own cookies and a non-harness `Authorization` header are forwarded
  unchanged.
- A `Set-Cookie` from a program that tries to set the harness session cookie is
  **dropped**, so a program can't sign you out or pin a session of its choosing.
- Programs are private by default; the global link needs a sign-in until you
  publish one deliberately.

### Exposing the harness to the internet

Turning on a tunnel puts the sign-in page on the public internet. What protects
it:

- argon2 password hashing, and a per-IP lockout after 8 failed sign-ins.
- Rate limiting on `/api` (240/min, and 20/min on sign-in).
- Sign in over HTTPS — through the tunnel — and the session cookie is marked
  `Secure` and HSTS is sent, so that session is never sent in the clear. Sign in
  over plain HTTP on the LAN and neither is set, because a `Secure` cookie is
  discarded by the browser on a plain-HTTP page, and HSTS pinned to a hostname
  with no certificate would lock you out of your own Pi. It is decided per
  request, so having a tunnel up does not break LAN sign-in.
- Sessions are HttpOnly and in memory: restarting the harness signs out every
  browser. API tokens survive a restart and are revoked individually.

Use a long password, and prefer a named tunnel behind Cloudflare Access if you
want an identity check in front of the sign-in page at all.
