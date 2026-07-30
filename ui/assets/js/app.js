// PiHarness UI: sign in, import programs, manage the installed ones.
// Handlers are delegated off one document listener, so card markup carries
// data-act attributes rather than inline onclick.

let authToken = null;      // only used when the UI is served cross-origin
let pollTimer = null;

const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
  .replace(/>/g, '&gt;').replace(/"/g, '&quot;');

function toast(msg, type = 'info', dur = 3500) {
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.innerHTML = `<span>${({success: '✓', error: '✕', info: 'ℹ'})[type] || ''}</span><span>${esc(msg)}</span>`;
  $('toast-container').appendChild(el);
  setTimeout(() => {
    el.style.transition = 'opacity 0.3s'; el.style.opacity = '0';
    setTimeout(() => el.remove(), 300);
  }, dur);
}

async function api(path, opts = {}) {
  const headers = {'Content-Type': 'application/json', ...(opts.headers || {})};
  if (authToken) headers['Authorization'] = `Bearer ${authToken}`;
  const r = await fetch(path, {...opts, headers, credentials: 'include'});
  if (r.status === 401) { showLogin(); return null; }
  return r;
}

async function detail(r, fallback) {
  try { return (await r.json()).detail || fallback; } catch { return fallback; }
}

// ── Sign in ───────────────────────────────────────────────────────────────────

let setupMode = false;

function showLogin() {
  $('view-app').classList.add('hidden');
  $('view-login').classList.remove('hidden');
  clearTimeout(pollTimer);
  setTimeout(() => $('login-user').focus(), 50);
}

function showApp(username) {
  $('view-login').classList.add('hidden');
  $('view-app').classList.remove('hidden');
  $('topbar-ver').textContent = username ? `signed in as ${username}` : '';
  loadPrograms();
}

async function boot() {
  const me = await fetch('/api/me', {credentials: 'include'});
  if (me.ok) { showApp((await me.json()).username); return; }
  try {
    const st = await (await fetch('/api/status')).json();
    setupMode = !!st.setup_required;
  } catch { setupMode = false; }
  if (setupMode) {
    $('login-sub').textContent = 'First run — create your account';
    $('login-btn').textContent = 'Create account';
    $('setup-note').classList.remove('hidden');
    $('login-pass').setAttribute('autocomplete', 'new-password');
    $('login-user').value = 'admin';
  }
  showLogin();
}

async function submitLogin() {
  const btn = $('login-btn'), err = $('login-error');
  const username = $('login-user').value.trim();
  const password = $('login-pass').value;
  if (!username || !password) {
    err.textContent = 'Enter a username and password.'; err.classList.add('show'); return;
  }
  const label = btn.textContent;
  btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>'; err.classList.remove('show');
  try {
    const r = await fetch(setupMode ? '/api/setup' : '/api/login', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      credentials: 'include', body: JSON.stringify({username, password}),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || 'Sign-in failed');
    // The harness serves this page, so the HttpOnly session cookie carries
    // every later request. The bearer token is never held in JS, leaving an
    // XSS nothing to steal.
    $('login-pass').value = '';
    setupMode = false;
    showApp(data.username);
  } catch (e) {
    err.textContent = e.message; err.classList.add('show');
  } finally {
    btn.disabled = false; btn.textContent = label;
  }
}

async function logout() {
  try { await api('/api/logout', {method: 'POST'}); } catch {}
  location.reload();
}

async function changePassword() {
  const current = prompt('Current password:');
  if (current == null) return;
  const next = prompt('New password (at least 8 characters):');
  if (next == null) return;
  const r = await api('/api/password', {
    method: 'POST', body: JSON.stringify({current_password: current, new_password: next})});
  if (!r) return;
  if (!r.ok) { toast(await detail(r, 'Could not change the password'), 'error', 5000); return; }
  toast('Password changed — signing you out', 'success', 2500);
  setTimeout(() => location.reload(), 1200);
}

// ── Programs ──────────────────────────────────────────────────────────────────

const STATUS = {
  active:        {label: 'Running',       cls: 'run'},
  inactive:      {label: 'Stopped',       cls: 'stop'},
  failed:        {label: 'Failed',        cls: 'fail'},
  activating:    {label: 'Starting…',     cls: 'wait'},
  importing:     {label: 'Importing…',    cls: 'wait'},
  needs_command: {label: 'Needs command', cls: 'wait'},
  error:         {label: 'Import failed', cls: 'fail'},
  unknown:       {label: 'Unknown',       cls: 'stop'},
};

async function loadPrograms() {
  const box = $('programs-list');
  if (!box) return;
  const r = await api('/api/programs');
  if (!r?.ok) { box.innerHTML = '<div class="loading">Could not load programs.</div>'; return; }
  const data = await r.json();
  const progs = data.programs;
  $('monitor-pill').classList.toggle('hidden', !data.monitor.connected);

  if (!progs.length) {
    box.innerHTML = `<div class="prog-empty">
      <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M21 16V8a2 2 0 00-1-1.73l-7-4a2 2 0 00-2 0l-7 4A2 2 0 003 8v8a2 2 0 001 1.73l7 4a2 2 0 002 0l7-4A2 2 0 0021 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/></svg>
      <b>No programs yet</b>
      <div>Paste a GitHub repository above to import your first one.</div>
    </div>`;
    return;
  }
  box.innerHTML = progs.map(p => card(p, data.monitor)).join('');

  // Keep polling while anything is importing or starting, so status flips live.
  clearTimeout(pollTimer);
  if (progs.some(p => p.status === 'importing' || p.status === 'activating')) {
    pollTimer = setTimeout(loadPrograms, 3000);
  }
  if (progs.some(p => p.ota !== 'self' && p.status !== 'importing')) checkUpdates();
}

async function checkUpdates() {
  const r = await api('/api/programs/updates');
  if (!r?.ok) return;
  for (const [name, u] of Object.entries((await r.json()).updates)) {
    if (!u.update_available) continue;
    document.querySelector(`[data-upd="${CSS.escape(name)}"]`)?.classList.remove('hidden');
    const btn = document.querySelector(`[data-updbtn="${CSS.escape(name)}"]`);
    if (btn) { btn.classList.remove('btn-ghost'); btn.classList.add('btn-primary'); }
  }
}

const OTA_META = {
  github: {label: 'GitHub', cls: '', title: 'The harness checks GitHub for new commits and flags them here. Click to auto-apply them.'},
  auto:   {label: 'Auto', cls: '', title: 'New commits are pulled and restarted on their own — no clicking. Click to switch to self-managed.'},
  self:   {label: 'Self-managed', cls: 'private', title: 'This program runs its own updater — the harness stays out of the way. Click to check GitHub again.'},
};

// Card buttons carry data-act (which handler) and data-name (which program).
// Extra values go in data-arg, so nothing has to be escaped into JS source.
const act = (a, name, arg) =>
  `data-act="${a}" data-name="${esc(name)}"${arg === undefined ? '' : ` data-arg="${esc(arg)}"`}`;

function card(p, mon) {
  const st = STATUS[p.status] || STATUS.unknown;
  const repoPath = p.repo_url.replace('https://github.com/', '');
  const lanUrl = p.web_port ? `http://${location.hostname}:${p.web_port}` : null;
  const settled = !['importing', 'error'].includes(p.status);

  let links = '';
  if (lanUrl) {
    links += `<a class="prog-chip" href="${esc(lanUrl)}" target="_blank" rel="noopener" title="Open on your local network">
      <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>LAN · :${p.web_port}</a>`;
    links += p.global_url
      ? `<a class="prog-chip global" href="${esc(p.global_url)}" target="_blank" rel="noopener" title="${p.global_via === 'tailscale' ? 'Link through Tailscale — works on any device signed in to your tailnet' : 'Link through the harness’s public address'}">
          <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 014 10 15.3 15.3 0 01-4 10 15.3 15.3 0 01-4-10 15.3 15.3 0 014-10z"/></svg>Global${p.global_via === 'tailscale' ? ' · Tailscale' : ''}</a>
         <button class="prog-chip toggle ${p.public ? '' : 'private'}" ${act('public', p.name, p.public)} title="${p.public ? 'Anyone with the link can open it. Click to require a sign-in.' : 'Sign-in required. Click to make the link public.'}">${p.public ? 'Public' : 'Private'}</button>`
      : `<span class="prog-chip dim" title="Set HARNESS_PUBLIC_URL, or run Tailscale, to get a link that works away from home">Global link needs a public address or Tailscale</span>`;
  }
  // Every web-UI program gets the monitor chip. With no monitor attached,
  // turning it on arms the kiosk to display when one is plugged in.
  if (settled && p.web_port) {
    links += `<button class="prog-chip toggle ${p.on_monitor ? '' : 'dim'}" ${act('monitor', p.name, p.on_monitor)}
      title="${p.on_monitor ? 'Showing fullscreen on the Pi’s monitor. Click to turn off.' : mon?.connected ? 'Show this program fullscreen on the monitor plugged into the Pi.' : 'No monitor detected right now — turning this on shows the program the moment one is plugged in.'}">
      <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>${p.on_monitor ? 'On monitor' : 'Show on monitor'}</button>`;
  }
  if (settled && p.has_token) {
    links += `<button class="prog-chip toggle private" ${act('token', p.name)}
      title="Cloned with a GitHub access token — update checks and pulls use it too. Click to replace or remove it.">
      <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0110 0v4"/></svg>Private repo</button>`;
  }
  if (settled) {
    const ota = OTA_META[p.ota] || OTA_META.github;
    links += `<button class="prog-chip toggle ${ota.cls}" ${act('ota', p.name, p.ota)} title="${ota.title}">OTA · ${ota.label}</button>`;
  }

  const canRun = !!p.start_command && settled;
  const runButtons = (p.status === 'needs_command' || !p.start_command)
    ? `<button class="btn btn-primary btn-xs" ${act('command', p.name, p.start_command || '')}>Set start command</button>`
    : (p.status === 'active' || p.status === 'activating')
      ? `<button class="btn btn-ghost btn-xs" ${act('stop', p.name)}>Stop</button>
         <button class="btn btn-ghost btn-xs" ${act('restart', p.name)}>Restart</button>`
      : canRun ? `<button class="btn btn-primary btn-xs" ${act('start', p.name)}>Start</button>` : '';

  const manageButtons = p.status === 'error' ? '' : `
    ${p.ota !== 'self' ? `<button class="btn btn-ghost btn-xs" data-updbtn="${esc(p.name)}" ${act('update', p.name)} title="git pull the latest code, reinstall dependencies, restart">Update</button>` : ''}
    <button class="btn btn-ghost btn-xs" ${act('secrets', p.name)} title="KEY=VALUE environment variables, stored on the Pi and injected at start">Secrets</button>
    ${p.web_port ? `<button class="btn btn-ghost btn-xs" ${act('moncmd', p.name, p.monitor_command || '')} title="Optional command run every time this program goes on the monitor">Monitor cmd${p.monitor_command ? ' ·✓' : ''}</button>` : ''}
    ${!p.has_token ? `<button class="btn btn-ghost btn-xs" ${act('token', p.name)} title="Repo gone private? Add a GitHub access token — update checks and pulls will use it">Add token</button>` : ''}
    <button class="btn btn-ghost btn-xs" ${act('port', p.name, p.web_port ?? '')} title="The port the program's web UI listens on — the LAN link, global link and kiosk all point here">Web port${p.web_port ? ` · ${p.web_port}` : ''}</button>
    <button class="btn btn-ghost btn-xs" ${act('logs', p.name)}>Logs</button>`;

  const actions = p.status === 'importing' ? '' : `<div class="prog-actions">
      ${runButtons}${manageButtons}
      <button class="btn btn-ghost btn-xs btn-danger" ${act('remove', p.name)}>Remove</button>
    </div>`;

  return `<div class="prog-card">
    <div class="prog-head">
      <span class="prog-led ${st.cls}"></span>
      <span class="prog-name">${esc(p.name)}</span>
      <span class="prog-badge ${st.cls}">${p.status === 'importing' && p.phase ? esc(p.phase) + '…' : st.label}</span>
      <span class="prog-badge upd hidden" data-upd="${esc(p.name)}" title="A newer commit is on GitHub — click Update to pull it">Update available</span>
      <a class="prog-repo" href="${esc(p.repo_url)}" target="_blank" rel="noopener" title="Open the repository on GitHub">${esc(repoPath)}</a>
    </div>
    ${p.start_command && p.status !== 'importing'
      ? `<div class="prog-cmd" ${act('command', p.name, p.start_command)} title="Start command — click to change">$ ${esc(p.start_command)}</div>` : ''}
    ${p.error && (p.status === 'error' || p.status === 'failed') ? `<div class="prog-err">${esc(p.error)}</div>` : ''}
    ${links ? `<div class="prog-links">${links}</div>` : ''}
    ${actions}
    <div class="prog-secrets hidden" data-secrets="${esc(p.name)}">
      <textarea spellcheck="false" placeholder="API_KEY=abc123&#10;DATABASE_URL=postgres://…&#10;# one KEY=VALUE per line"></textarea>
      <div class="prog-secrets-foot">
        <span>Stored on the Pi only (root-readable file) · injected as environment variables · saving restarts a running program</span>
        <button class="btn btn-ghost btn-xs" ${act('secrets', p.name)}>Cancel</button>
        <button class="btn btn-primary btn-xs" ${act('savesecrets', p.name)}>Save secrets</button>
      </div>
    </div>
    <pre class="prog-logs hidden" data-logs="${esc(p.name)}"></pre>
  </div>`;
}

// ── Actions ───────────────────────────────────────────────────────────────────

async function importProgram() {
  const repo = $('prog-repo-input').value.trim();
  if (!repo) { toast('Paste a GitHub repository URL first', 'error'); return; }
  const btn = $('prog-import-btn');
  const body = {
    repo_url: repo,
    name: $('prog-name-input').value.trim() || null,
    start_command: $('prog-cmd-input').value.trim() || null,
    web_port: parseInt($('prog-port-input').value, 10) || null,
    token: $('prog-token-input').value.trim() || null,
    ota: $('prog-ota-input').value,
  };
  btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>';
  try {
    const r = await api('/api/programs', {method: 'POST', body: JSON.stringify(body)});
    if (!r) return;
    if (!r.ok) { toast(await detail(r, 'Import failed'), 'error', 5000); return; }
    ['prog-repo-input', 'prog-name-input', 'prog-cmd-input', 'prog-port-input', 'prog-token-input']
      .forEach(id => $(id).value = '');
    toast(`Importing ${(await r.json()).name} — cloning from GitHub…`, 'info', 3500);
    loadPrograms();
  } finally {
    btn.disabled = false; btn.textContent = 'Import';
  }
}

async function edit(name, body, okMsg) {
  const r = await api(`/api/programs/${encodeURIComponent(name)}`,
    {method: 'PUT', body: JSON.stringify(body)});
  if (!r) return false;
  if (!r.ok) { toast(await detail(r, 'Could not save that'), 'error', 5000); return false; }
  if (okMsg) toast(okMsg, 'success', 3000);
  loadPrograms();
  return true;
}

async function programAction(name, action) {
  const r = await api(`/api/programs/${encodeURIComponent(name)}/action`,
    {method: 'POST', body: JSON.stringify({action})});
  if (!r) return;
  if (!r.ok) { toast(await detail(r, `Could not ${action} ${name}`), 'error', 5000); return; }
  toast(`${({start: 'Started', stop: 'Stopped', restart: 'Restarted'})[action]} ${name}`, 'success', 2000);
  loadPrograms();
}

async function toggleSecrets(name) {
  const box = document.querySelector(`[data-secrets="${CSS.escape(name)}"]`);
  if (!box) return;
  if (!box.classList.contains('hidden')) { box.classList.add('hidden'); return; }
  const ta = box.querySelector('textarea');
  const r = await api(`/api/programs/${encodeURIComponent(name)}/secrets`);
  ta.value = r?.ok ? (await r.json()).env : '';
  box.classList.remove('hidden');
  ta.focus();
}

async function saveSecrets(name) {
  const box = document.querySelector(`[data-secrets="${CSS.escape(name)}"]`);
  const r = await api(`/api/programs/${encodeURIComponent(name)}/secrets`,
    {method: 'PUT', body: JSON.stringify({env: box.querySelector('textarea').value})});
  if (!r) return;
  if (!r.ok) { toast(await detail(r, 'Could not save secrets'), 'error', 5000); return; }
  toast((await r.json()).restarted
    ? 'Secrets saved — program restarted with the new values' : 'Secrets saved', 'success', 3000);
  box.classList.add('hidden');
}

async function toggleLogs(name) {
  const pre = document.querySelector(`[data-logs="${CSS.escape(name)}"]`);
  if (!pre) return;
  if (!pre.classList.contains('hidden')) { pre.classList.add('hidden'); return; }
  pre.textContent = 'Loading…'; pre.classList.remove('hidden');
  const r = await api(`/api/programs/${encodeURIComponent(name)}/logs`);
  pre.textContent = r?.ok ? (await r.json()).logs : 'Could not load logs.';
  pre.scrollTop = pre.scrollHeight;
}

const HANDLERS = {
  start:   name => programAction(name, 'start'),
  stop:    name => programAction(name, 'stop'),
  restart: name => programAction(name, 'restart'),
  logs:    name => toggleLogs(name),
  secrets: name => toggleSecrets(name),
  savesecrets: name => saveSecrets(name),

  async update(name) {
    toast(`Updating ${name} from GitHub…`, 'info', 2500);
    const r = await api(`/api/programs/${encodeURIComponent(name)}/update`, {method: 'POST'});
    if (!r) return;
    if (!r.ok) { toast(await detail(r, 'Update failed'), 'error', 6000); return; }
    toast(`${name} updated`, 'success');
    loadPrograms();
  },

  async remove(name) {
    if (!confirm(`Remove "${name}"? This stops it and deletes its files from the Pi. The GitHub repository is untouched.`)) return;
    const r = await api(`/api/programs/${encodeURIComponent(name)}`, {method: 'DELETE'});
    if (!r) return;
    if (!r.ok) { toast('Could not remove the program', 'error'); return; }
    toast(`Removed ${name}`, 'success');
    loadPrograms();
  },

  command(name, current) {
    const cmd = prompt(`Start command for "${name}" — runs from the program's folder:`, current || '');
    if (cmd == null) return;
    edit(name, {start_command: cmd}, 'Start command saved');
  },

  moncmd(name, current) {
    const cmd = prompt(`Monitor command for "${name}" — runs from the program's folder every time it goes on the monitor. Leave empty to remove:`, current || '');
    if (cmd == null) return;
    edit(name, {monitor_command: cmd},
      cmd.trim() ? 'Monitor command saved — it runs on every kiosk start' : 'Monitor command removed');
  },

  token(name) {
    const t = prompt(`GitHub access token for "${name}" — used for update checks and pulls. Leave empty to remove it:`);
    if (t == null) return;
    edit(name, {token: t},
      t.trim() ? 'Token saved — it stays on the Pi and is never shown again' : 'Token removed');
  },

  port(name, current) {
    const v = prompt(`Web port for "${name}" — the port its web UI actually listens on (1024–65535). Leave empty to remove the web links:`, current || '');
    if (v == null) return;
    const port = parseInt(v, 10);
    if (v.trim() !== '' && !(port >= 1024 && port <= 65535)) { toast('Port must be 1024–65535', 'error'); return; }
    edit(name, v.trim() === '' ? {clear_port: true} : {web_port: port},
      v.trim() ? `Web port set to ${port} — links and the kiosk now point there` : 'Web port removed');
  },

  public(name, arg) {
    const next = arg !== 'true';
    edit(name, {public: next}, next
      ? 'Link is public — anyone with it can open the program'
      : 'Link is private — a harness sign-in is required');
  },

  ota(name, current) {
    const next = ({github: 'auto', auto: 'self', self: 'github'})[current] || 'github';
    edit(name, {ota: next}, ({
      github: `The harness now checks GitHub for ${name} updates`,
      auto:   `${name} will now update itself automatically — no clicking`,
      self:   `${name} now manages its own updates — the harness will stop checking`,
    })[next]);
  },

  async monitor(name, arg) {
    const on = arg === 'true';
    const r = await api(`/api/programs/${encodeURIComponent(name)}/monitor`,
      {method: 'POST', body: JSON.stringify({on: !on})});
    if (!r) return;
    if (!r.ok) { toast(await detail(r, 'Could not change the monitor'), 'error', 6000); return; }
    const d = await r.json();
    toast(on ? 'Monitor turned off'
      : d.connected ? `${name} is now fullscreen on the Pi's monitor`
      : `${name} will show fullscreen as soon as a monitor is plugged into the Pi`, 'success', 3500);
    loadPrograms();
  },
};

// ── The prompt ────────────────────────────────────────────────────────────────
// Hand this to any AI along with a project and the result imports cleanly.
// Kept in sync with the copy in docs/programs.md.
const AI_PROMPT = `Convert this project into a self-hostable, always-running program that a home
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
   not auto-detectable), and the web port (if any).`;

async function copyPrompt() {
  try {
    await navigator.clipboard.writeText(AI_PROMPT);
  } catch {
    // The clipboard API needs HTTPS or localhost. Fall back for plain-HTTP LAN.
    const ta = document.createElement('textarea');
    ta.value = AI_PROMPT; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); } finally { ta.remove(); }
  }
  toast('Prompt copied — paste it into any AI along with your project', 'success', 3500);
}

// ── Wiring ────────────────────────────────────────────────────────────────────

document.addEventListener('click', e => {
  const el = e.target.closest('[data-act]');
  if (!el) return;
  const fn = HANDLERS[el.dataset.act];
  if (fn) { e.preventDefault(); fn(el.dataset.name, el.dataset.arg); }
});

$('login-btn').addEventListener('click', submitLogin);
$('login-user').addEventListener('keydown', e => { if (e.key === 'Enter') $('login-pass').focus(); });
$('login-pass').addEventListener('keydown', e => { if (e.key === 'Enter') submitLogin(); });
$('prog-import-btn').addEventListener('click', importProgram);
$('prog-repo-input').addEventListener('keydown', e => { if (e.key === 'Enter') importProgram(); });
$('prompt-btn').addEventListener('click', copyPrompt);
$('logout-btn').addEventListener('click', logout);
$('pass-btn').addEventListener('click', changePassword);
$('theme-btn').addEventListener('click', () => {
  const next = document.documentElement.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('ph-theme', next);
});

boot();
