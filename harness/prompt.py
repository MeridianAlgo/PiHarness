"""
The spec handed to an AI to convert an existing always-on app into something
PiHarness can import and supervise.

This is the single source. The UI fetches it from /api/prompt and docs/programs.md
quotes it from here, so there is one copy to keep correct instead of three that
drift apart.
"""

SPEC = """\
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
"""


def spec() -> str:
    return SPEC
