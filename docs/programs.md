# Writing a program for the harness

Import a program from GitHub and PiHarness keeps it running on the Pi, in the
background, through crashes and reboots.

If you're making a repo importable, this page is the only one you need. Hand it
to a developer or an AI as-is. [api.md](api.md) is a different thing: it's for
driving the harness from a script, not for writing a program.

## The AI prompt

Paste this into any AI along with your code, or click **Copy prompt** in the web
UI. What comes back imports cleanly.

```text
Convert this project into a self-hostable, always-running program that a home
server (PiHarness, on a Raspberry Pi) can import from GitHub and keep running
24/7 as a background service. Apply ALL of the following:

1. Long-running: the app must be a persistent process (a server or a worker
   loop) that never exits on its own. No one-shot scripts — the supervisor
   restarts exited processes forever, so a script that finishes becomes a
   crash loop. Crashing on a fatal error is fine; it gets restarted.
2. Start command: make it auto-detectable — a package.json "start" script, a
   main.py / app.py / server.py entry file (Python), or index.js (Node). If
   none of those fit, state the exact one-line start command in the README
   (it runs from the repo root via bash).
3. Dependencies: declare ALL of them in requirements.txt (Python — installed
   into a private venv) or package.json (Node — npm install --omit=dev).
   Nothing else gets installed for you.
4. Config and secrets: read every key, token and setting from environment
   variables (os.environ / process.env), with sensible defaults where
   possible. Never commit secrets — the host injects them as env vars.
5. If it serves a web page: listen on 0.0.0.0 at the port given by the PORT
   environment variable (fall back to a fixed default and say what it is).
   It is also reverse-proxied under the path /apps/<name>/, so use relative
   URLs for every asset and link (no absolute /static/... paths), and stick
   to plain HTTP — WebSockets and server-sent events don't pass the proxy.
   A web page is optional; a headless worker is fine.
6. Data: write any files or state to a path inside the app's own folder
   (relative paths) or one taken from an env var — it runs from its cloned
   repo directory.
7. Finish by telling me: the GitHub repo to import, the start command (if
   not auto-detectable), and the web port (if any).
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
- A global link, `https://<host>/apps/<name>/`, proxied through the harness. The
  host comes from `HARNESS_PUBLIC_URL` if you've set one, otherwise from
  Tailscale when it's running, in which case the link works on any device signed
  in to your tailnet.

The global link is public by default, since sharing it is usually the point.
Flip the chip on the card to make it require a sign-in.

Two things the global link can't do that the LAN link can:

- Serve pages that reference assets by absolute path. `/static/app.js` will 404
  under `/apps/<name>/`. Use relative paths or make the base path configurable.
- Carry WebSockets or server-sent events. Plain HTTP only.

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
- Access tokens reach git as in-memory environment config, so they never touch
  the clone's `.git/config`, the stored repo URL, process argv or an API
  response.
