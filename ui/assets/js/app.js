// PiHarness UI: sign in, import programs, manage the installed ones.
// Handlers are delegated off one document listener, so card markup carries
// data-act attributes rather than inline onclick.

let authToken = null;      // only used when the UI is served cross-origin
let pollTimer = null;
let metricsTimer = null;

const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
  .replace(/>/g, '&gt;').replace(/"/g, '&quot;');

// Drawn, not typed: emoji render differently per platform and can't be themed.
const TOAST_ICON = {
  success: '<path d="M20 6 9 17l-5-5"/>',
  error:   '<path d="M18 6 6 18M6 6l12 12"/>',
  info:    '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/>',
};

function toast(msg, type = 'info', dur = 3500) {
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.innerHTML = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
    stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"
    >${TOAST_ICON[type] || TOAST_ICON.info}</svg><span>${esc(msg)}</span>`;
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
  clearInterval(metricsTimer);   // or a signed-out tab keeps polling forever
  setTimeout(() => $('login-user').focus(), 50);
}

function showApp(username) {
  $('view-login').classList.add('hidden');
  $('view-app').classList.remove('hidden');
  $('topbar-ver').textContent = username ? `signed in as ${username}` : '';
  loadPrograms();
  loadMetrics();
  loadTunnel();
  loadTokens();
  loadSelfUpdate();
  // The dashboard is the one thing that has to stay live. Polling matches the
  // server's sample interval, so it never asks for a point that doesn't exist.
  clearInterval(metricsTimer);
  metricsTimer = setInterval(loadMetrics, 5000);
}

async function boot() {
  const me = await fetch('/api/me', {credentials: 'include'});
  if (me.ok) { showApp((await me.json()).username); return; }
  try {
    const st = await (await fetch('/api/status')).json();
    setupMode = !!st.setup_required;
  } catch { setupMode = false; }
  if (setupMode) {
    $('login-sub').textContent = 'First run: create your account';
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
  toast('Password changed. Signing you out.', 'success', 2500);
  setTimeout(() => location.reload(), 1200);
}

// ── Dashboard ─────────────────────────────────────────────────────────────────

const fmtBytes = n => {
  if (n === null || n === undefined) return '—';
  const units = ['B', 'K', 'M', 'G', 'T'];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n < 10 && i > 0 ? n.toFixed(1) : Math.round(n)}${units[i]}`;
};

const fmtDuration = s => {
  if (s === null || s === undefined) return '—';
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  return `${m}m`;
};

// Each tile's scale is fixed, not fitted to the data. A sparkline rescaled to
// its own min/max turns a 2% wobble into a cliff; a constant domain means the
// shape and the height both mean something, and two tiles can be compared.
const TILES = [
  {key: 'cpu',    label: 'CPU',    unit: '%',  domain: [0, 100], warn: 75, crit: 90},
  // The Pi soft-throttles at 80°C and hard-caps at 85, so those are the
  // thresholds that matter — not an arbitrary "hot".
  {key: 'temp',   label: 'Temp',   unit: '°C', domain: [30, 90], warn: 70, crit: 80},
  {key: 'memory', label: 'Memory', unit: '%',  domain: [0, 100], warn: 80, crit: 92},
  {key: 'disk',   label: 'Disk',   unit: '%',  domain: [0, 100], warn: 85, crit: 95},
];

const tileState = (v, t) => v === null || v === undefined ? 'ok'
  : v >= t.crit ? 'crit' : v >= t.warn ? 'warn' : 'ok';

const STATE_WORD = {ok: 'ok', warn: 'high', crit: 'critical'};

function sparkPath(values, domain, w, h) {
  if (values.length < 2) return '';
  const [lo, hi] = domain, span = (hi - lo) || 1;
  const step = w / (values.length - 1);
  return values.map((v, i) => {
    const y = h - ((Math.max(lo, Math.min(hi, v)) - lo) / span) * h;
    return `${i ? 'L' : 'M'}${(i * step).toFixed(2)} ${y.toFixed(2)}`;
  }).join(' ');
}

function tileMarkup(t, value, series, sub) {
  const state = tileState(value, t);
  const values = series.map(p => p[1]);
  const w = 100, h = 26;
  const shown = value === null || value === undefined ? '—'
    : (t.unit === '%' ? Math.round(value) : value.toFixed(1));
  // The state word travels with the colour, so the tile still reads correctly
  // in greyscale or with any colour vision deficiency.
  const label = `${t.label}: ${shown}${t.unit === '%' ? '%' : ' °C'}, ${STATE_WORD[state]}`
    + (values.length > 1 ? `. Last ${values.length} samples ranged ${Math.min(...values).toFixed(0)} to ${Math.max(...values).toFixed(0)}.` : '');
  return `<div class="tile ${state}" data-tile="${t.key}">
    <div class="tile-head">
      <span class="tile-label">${t.label}</span>
      <span class="tile-state ${state}">${STATE_WORD[state]}</span>
    </div>
    <div class="tile-value">${shown}<span class="tile-unit">${t.unit}</span></div>
    <svg class="tile-spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"
         role="img" aria-label="${esc(label)}" data-domain="${t.domain[0]},${t.domain[1]}">
      <path class="spark-line" d="${sparkPath(values, t.domain, w, h)}"/>
      <line class="spark-cursor" x1="0" y1="0" x2="0" y2="${h}"/>
      <circle class="spark-dot" r="2.5" cx="0" cy="0"/>
    </svg>
    <div class="tile-read" data-read="${t.key}">${sub || ''}</div>
  </div>`;
}

let _series = {};

async function loadMetrics() {
  const r = await api('/api/metrics');
  if (!r?.ok) return;
  const m = await r.json();
  _series = m.history || {};
  const host = m.host || {};
  const mem = host.memory || {}, disk = host.disk || {};
  const current = {cpu: host.cpu_percent, temp: host.temperature,
                   memory: mem.percent, disk: disk.percent};
  const subs = {
    cpu: host.load ? `load ${host.load.join(' ')}` : '',
    temp: '',
    memory: mem.total ? `${fmtBytes(mem.used)} of ${fmtBytes(mem.total)}` : '',
    disk: disk.total ? `${fmtBytes(disk.free)} free` : '',
  };
  $('tiles').innerHTML = TILES
    .map(t => tileMarkup(t, current[t.key], _series[t.key] || [], subs[t.key])).join('');

  $('host-line').innerHTML = [
    host.model ? `<span><b>${esc(host.model)}</b></span>` : '',
    host.cpu_count ? `<span>${host.cpu_count} cores</span>` : '',
    host.uptime ? `<span>up <b>${fmtDuration(host.uptime)}</b></span>` : '',
    `<span>sampling every ${m.interval}s</span>`,
  ].filter(Boolean).join('');

  // Undervoltage is the single most common cause of a Pi that behaves oddly
  // under load, and it is invisible unless something says so.
  const th = m.throttled, warn = $('host-warn');
  const notes = [];
  if (th?.under_voltage_now) notes.push('Undervoltage right now — the power supply is not keeping up.');
  else if (th?.under_voltage_since_boot) notes.push('Undervoltage has occurred since boot.');
  if (th?.throttled_now) notes.push('Thermally throttled right now.');
  else if (th?.throttled_since_boot) notes.push('Thermal throttling has occurred since boot.');
  warn.textContent = notes.join(' ');
  warn.classList.toggle('hidden', !notes.length);

  _progStats = m.programs || {};
  paintProgramStats();
}

// A sparkline is a plot, so it gets a readout: hovering reveals the value at
// that point rather than leaving the trend unquantified.
document.addEventListener('mousemove', e => {
  const svg = e.target.closest?.('.tile-spark');
  if (!svg) return;
  const key = svg.closest('.tile')?.dataset.tile;
  const series = _series[key] || [];
  if (series.length < 2) return;
  const box = svg.getBoundingClientRect();
  const frac = Math.max(0, Math.min(1, (e.clientX - box.left) / box.width));
  const i = Math.round(frac * (series.length - 1));
  const [lo, hi] = svg.dataset.domain.split(',').map(Number);
  const v = series[i][1], x = (i / (series.length - 1)) * 100;
  const y = 26 - ((Math.max(lo, Math.min(hi, v)) - lo) / ((hi - lo) || 1)) * 26;
  svg.querySelector('.spark-cursor').setAttribute('x1', x);
  svg.querySelector('.spark-cursor').setAttribute('x2', x);
  const dot = svg.querySelector('.spark-dot');
  dot.setAttribute('cx', x); dot.setAttribute('cy', y);
  const ago = Math.round((series[series.length - 1][0] - series[i][0]));
  const unit = TILES.find(t => t.key === key)?.unit === '%' ? '%' : '°C';
  const read = document.querySelector(`[data-read="${key}"]`);
  if (read) read.textContent = `${v}${unit} · ${ago ? `${ago}s ago` : 'now'}`;
});


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
      <b>Nothing installed</b>
      <div>Import a GitHub repository above and it will appear here.</div>
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
  auto:   {label: 'Auto', cls: '', title: 'New commits are pulled and restarted on their own. Click to switch to self-managed.'},
  self:   {label: 'Self-managed', cls: 'private', title: 'This program runs its own updater and the harness stays out of the way. Click to check GitHub again.'},
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
      ? `<a class="prog-chip global" href="${esc(p.global_url)}" target="_blank" rel="noopener" title="${p.global_via === 'tailscale' ? 'Link through Tailscale. Works on any device signed in to your tailnet.' : 'Link through the harness’s public address'}">
          <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 014 10 15.3 15.3 0 01-4 10 15.3 15.3 0 01-4-10 15.3 15.3 0 014-10z"/></svg>Global${p.global_via === 'tailscale' ? ' · Tailscale' : ''}</a>
         <button class="prog-chip toggle ${p.public ? '' : 'private'}" ${act('public', p.name, p.public)} title="${p.public ? 'Anyone with the link can open it. Click to require a sign-in.' : 'Sign-in required. Click to make the link public.'}">${p.public ? 'Public' : 'Private'}</button>`
      : `<span class="prog-chip dim" title="Set HARNESS_PUBLIC_URL, or run Tailscale, to get a link that works away from home">Global link needs a public address or Tailscale</span>`;
  }
  // Every web-UI program gets the monitor chip. With no monitor attached,
  // turning it on arms the kiosk to display when one is plugged in.
  if (settled && p.web_port) {
    links += `<button class="prog-chip toggle ${p.on_monitor ? '' : 'dim'}" ${act('monitor', p.name, p.on_monitor)}
      title="${p.on_monitor ? 'Showing fullscreen on the Pi’s monitor. Click to turn off.' : mon?.connected ? 'Show this program fullscreen on the monitor plugged into the Pi.' : 'No monitor detected. Turning this on shows the program the moment one is plugged in.'}">
      <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>${p.on_monitor ? 'On monitor' : 'Show on monitor'}</button>`;
  }
  if (settled && p.has_token) {
    links += `<button class="prog-chip toggle private" ${act('token', p.name)}
      title="Cloned with a GitHub access token, used for update checks and pulls too. Click to replace or remove it.">
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
    ${p.web_port ? `<button class="btn btn-ghost btn-xs" ${act('moncmd', p.name, p.monitor_command || '')} title="Optional command run every time this program goes on the monitor">Monitor cmd${p.monitor_command ? ' · set' : ''}</button>` : ''}
    ${!p.has_token ? `<button class="btn btn-ghost btn-xs" ${act('token', p.name)} title="Repo gone private? Add a GitHub access token for update checks and pulls.">Add token</button>` : ''}
    <button class="btn btn-ghost btn-xs" ${act('port', p.name, p.web_port ?? '')} title="The port the program's web UI listens on. The LAN link, global link and kiosk all point here.">Web port${p.web_port ? ` · ${p.web_port}` : ''}</button>
    <button class="btn btn-ghost btn-xs" ${act('logs', p.name)}>Logs</button>`;

  const actions = p.status === 'importing' ? '' : `<div class="prog-actions">
      ${runButtons}${manageButtons}
      <button class="btn btn-ghost btn-xs btn-danger" ${act('remove', p.name)}>Remove</button>
    </div>`;

  return `<div class="prog-card ${st.cls}">
    <div class="prog-head">
      <span class="prog-led ${st.cls}"></span>
      <span class="prog-name">${esc(p.name)}</span>
      <span class="prog-badge ${st.cls}">${p.status === 'importing' && p.phase ? esc(p.phase) + '…' : st.label}</span>
      <span class="prog-badge upd hidden" data-upd="${esc(p.name)}" title="A newer commit is on GitHub. Click Update to pull it.">Update available</span>
      <a class="prog-repo" href="${esc(p.repo_url)}" target="_blank" rel="noopener" title="Open the repository on GitHub">${esc(repoPath)}</a>
    </div>
    ${p.start_command && p.status !== 'importing'
      ? `<div class="prog-cmd" ${act('command', p.name, p.start_command)} title="Start command. Click to change.">${esc(p.start_command)}</div>` : ''}
    <div class="prog-stats hidden" data-stats="${esc(p.name)}"></div>
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

let _progStats = {};

function paintProgramStats() {
  for (const [name, s] of Object.entries(_progStats)) {
    const box = document.querySelector(`[data-stats="${CSS.escape(name)}"]`);
    if (!box) continue;
    const bits = [];
    if (s.uptime) bits.push(`up <b>${fmtDuration(s.uptime)}</b>`);
    if (s.memory) bits.push(`mem <b>${fmtBytes(s.memory)}</b>`);
    if (s.cpu_seconds !== null && s.cpu_seconds !== undefined) bits.push(`cpu <b>${fmtDuration(s.cpu_seconds)}</b>`);
    if (s.restarts) bits.push(`restarts <b>${s.restarts}</b>`);
    if (s.pid) bits.push(`pid <b>${s.pid}</b>`);
    box.innerHTML = bits.join('');
    box.classList.toggle('hidden', !bits.length);
  }
}

// ── Remote access ─────────────────────────────────────────────────────────────

async function loadTunnel() {
  const r = await api('/api/tunnel');
  if (!r?.ok) return;
  const t = await r.json();
  const state = $('tunnel-state'), actions = $('tunnel-actions');
  $('tunnel-pill').classList.toggle('hidden', !t.url);

  if (!t.installed) {
    state.innerHTML = `<span class="prog-badge wait">cloudflared not installed</span>`;
    actions.innerHTML = '';
    $('tunnel-named').classList.add('hidden');
    state.insertAdjacentHTML('beforeend',
      `<span class="tunnel-url">Install it on the Pi, then reload this page.</span>`);
    return;
  }
  $('tunnel-named').classList.remove('hidden');

  const badge = !t.enabled ? '<span class="prog-badge">off</span>'
    : t.state === 'active' && t.url ? '<span class="prog-badge run">connected</span>'
    : t.state === 'failed' ? '<span class="prog-badge fail">failed</span>'
    : '<span class="prog-badge wait">connecting</span>';
  const url = t.url
    ? `<span class="tunnel-url"><a href="${esc(t.url)}" target="_blank" rel="noopener">${esc(t.url)}</a></span>`
    : t.enabled ? '<span class="tunnel-url">Waiting for Cloudflare to assign an address…</span>'
    : '<span class="tunnel-url">No public address. The Pi is reachable on your LAN only.</span>';
  state.innerHTML = badge + url
    + (t.ephemeral && t.url ? '<span class="prog-badge wait">changes on restart</span>' : '');

  actions.innerHTML = t.enabled
    ? `<button class="btn btn-sm" data-act="tunneloff">Turn off</button>
       <button class="btn btn-ghost btn-sm" data-act="tunnellogs">Tunnel logs</button>`
    : `<button class="btn btn-primary btn-sm" data-act="tunnelquick">Get a public address</button>`;
}

// ── Harness updates ───────────────────────────────────────────────────────────

async function loadSelfUpdate() {
  const state = $('selfupdate-state'), actions = $('selfupdate-actions');
  const r = await api('/api/update');
  if (!r?.ok) { state.innerHTML = '<span class="tunnel-url">Could not check for updates.</span>'; return; }
  const u = await r.json();
  const version = `<span class="tunnel-url">v${esc(u.version)}${u.local ? ' · ' + esc(u.local) : ''}</span>`;

  if (u.error) {
    state.innerHTML = `<span class="prog-badge wait">can't check</span>${version}
      <span class="tunnel-url">${esc(u.error)}</span>`;
    actions.innerHTML = '';
    return;
  }
  if (!u.update_available) {
    state.innerHTML = `<span class="prog-badge run">up to date</span>${version}`;
    actions.innerHTML = `<button class="btn btn-ghost btn-sm" data-act="selfcheck">Check again</button>`;
    return;
  }
  state.innerHTML = `<span class="prog-badge wait">update available</span>
    <span class="tunnel-url">v${esc(u.version)} → v${esc(u.remote_version || '?')}
    · ${esc(u.local)} → ${esc(u.remote)} on ${esc(u.branch)}</span>`;
  actions.innerHTML = `<button class="btn btn-primary btn-sm" data-act="selfupdate">Update harness</button>
    <button class="btn btn-ghost btn-sm" data-act="selflogs">Update log</button>`;
}

// ── API tokens ────────────────────────────────────────────────────────────────

async function loadTokens() {
  const r = await api('/api/tokens');
  if (!r?.ok) return;
  const tokens = (await r.json()).tokens;
  $('token-list').innerHTML = tokens.length
    ? tokens.map(t => `<div class="token-row">
        <span class="token-label">${esc(t.label)}</span>
        <span class="token-scope">${esc(t.scope || 'full')}</span>
        <span class="token-meta">${t.last_used ? 'used ' + fmtAgo(t.last_used) : 'never used'}</span>
        ${t.scope === 'program'
          // Issued by the harness, not by you. It dies with its program, and
          // revoking it here would leave that program holding a dead token.
          ? '<span class="token-meta">removed with the program</span>'
          : `<button class="btn btn-ghost btn-xs btn-danger" data-act="revoke" data-name="${esc(t.id)}"
          data-arg="${esc(t.label)}">Revoke</button>`}
      </div>`).join('')
    : '<div class="prog-empty"><b>No tokens</b><div>Create one to drive the harness from a script.</div></div>';
}

const fmtAgo = ts => {
  const s = Date.now() / 1000 - ts;
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
};

async function createToken() {
  const label = $('token-label-input').value.trim() || 'script';
  const scope = $('token-scope-input').value;
  const r = await api('/api/tokens', {method: 'POST', body: JSON.stringify({label, scope})});
  if (!r?.ok) { toast('Could not create the token', 'error'); return; }
  const {token} = await r.json();
  $('token-label-input').value = '';
  // Shown once, on purpose: only a hash is kept, so there is no way to show it
  // again and no point pretending otherwise.
  $('token-new').innerHTML = `<div class="token-new">
    <p>Copy this now. It is not stored and cannot be shown again.</p>
    <code>${esc(token)}</code>
  </div>`;
  loadTokens();
  // Fill the setup snippets in with the real token and open them, so the next
  // step after "here is your token" is visible rather than guessed at.
  renderAgentSetup(token);
  $('agent-setup').open = true;
}

// ── Agent setup ───────────────────────────────────────────────────────────────
// Snippets carry this harness's own address, so they paste as-is. A token is
// substituted only in the browser that just created it; it is never stored.

function agentSnippets(token) {
  const base = location.origin;
  const t = token || '<your token>';
  return {
    curl: `curl -O ${base}/agent/piharness_mcp.py`,
    claude: `claude mcp add piharness \\\n  --env PIHARNESS_URL=${base} \\\n  --env PIHARNESS_TOKEN=${t} \\\n  -- python3 "$PWD/piharness_mcp.py"`,
    codex: `[mcp_servers.piharness]\ncommand = "python3"\nargs = ["/full/path/to/piharness_mcp.py"]\nenv = { PIHARNESS_URL = "${base}", PIHARNESS_TOKEN = "${t}" }`,
    agent: `${base}/api/agent`,
  };
}

function renderAgentSetup(token) {
  if (!$('mcp-curl')) return;
  const s = agentSnippets(token);
  $('mcp-curl').textContent = s.curl;
  $('mcp-claude').textContent = s.claude;
  $('mcp-codex').textContent = s.codex;
  $('mcp-agent-url').textContent = s.agent;
}

async function copyAgentSetup() {
  // If a token was just minted it is still on screen; use it, so the copied
  // block is ready to paste rather than needing a manual substitution.
  const shown = document.querySelector('#token-new code');
  const x = agentSnippets(shown ? shown.textContent : null);
  const text = `# Download the MCP server\n${x.curl}\n\n# Claude Code\n${x.claude}\n\n# Codex, in ~/.codex/config.toml\n${x.codex}\n`;
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); } finally { ta.remove(); }
  }
  toast('Setup copied', 'success', 2500);
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
    toast(`Importing ${(await r.json()).name}, cloning from GitHub`, 'info', 3500);
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
    ? 'Secrets saved. Program restarted with the new values.' : 'Secrets saved', 'success', 3000);
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
    toast(`Updating ${name} from GitHub`, 'info', 2500);
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
    const cmd = prompt(`Start command for "${name}". Runs from the program's folder:`, current || '');
    if (cmd == null) return;
    edit(name, {start_command: cmd}, 'Start command saved');
  },

  moncmd(name, current) {
    const cmd = prompt(`Monitor command for "${name}". Runs from the program's folder every time it goes on the monitor. Leave empty to remove:`, current || '');
    if (cmd == null) return;
    edit(name, {monitor_command: cmd},
      cmd.trim() ? 'Monitor command saved. It runs on every kiosk start.' : 'Monitor command removed');
  },

  token(name) {
    const t = prompt(`GitHub access token for "${name}", used for update checks and pulls. Leave empty to remove it:`);
    if (t == null) return;
    edit(name, {token: t},
      t.trim() ? 'Token saved. It stays on the Pi and is never shown again.' : 'Token removed');
  },

  port(name, current) {
    const v = prompt(`Web port for "${name}". The port its web UI actually listens on (1024–65535). Leave empty to remove the web links:`, current || '');
    if (v == null) return;
    const port = parseInt(v, 10);
    if (v.trim() !== '' && !(port >= 1024 && port <= 65535)) { toast('Port must be 1024–65535', 'error'); return; }
    edit(name, v.trim() === '' ? {clear_port: true} : {web_port: port},
      v.trim() ? `Web port set to ${port}. Links and the kiosk now point there.` : 'Web port removed');
  },

  public(name, arg) {
    const next = arg !== 'true';
    edit(name, {public: next}, next
      ? 'Link is public. Anyone with it can open the program.'
      : 'Link is private. A sign-in is required.');
  },

  ota(name, current) {
    const next = ({github: 'auto', auto: 'self', self: 'github'})[current] || 'github';
    edit(name, {ota: next}, ({
      github: `The harness now checks GitHub for ${name} updates`,
      auto:   `${name} will now update itself automatically.`,
      self:   `${name} now manages its own updates. The harness will stop checking.`,
    })[next]);
  },

  async tunnelquick() {
    toast('Opening a tunnel to Cloudflare…', 'info', 4000);
    const r = await api('/api/tunnel', {method: 'POST', body: JSON.stringify({mode: 'quick'})});
    if (!r) return;
    if (!r.ok) { toast(await detail(r, 'Could not start the tunnel'), 'error', 8000); return; }
    const t = await r.json();
    toast(t.url ? `Public at ${t.url}` : 'Tunnel starting. The address will appear shortly.',
          'success', 6000);
    loadTunnel(); loadPrograms();
  },

  async tunneloff() {
    if (!confirm('Turn off the tunnel? Every global link stops working until you turn it back on.')) return;
    const r = await api('/api/tunnel', {method: 'DELETE'});
    if (!r?.ok) { toast('Could not turn the tunnel off', 'error'); return; }
    toast('Tunnel off. The Pi is reachable on your LAN only.', 'success');
    loadTunnel(); loadPrograms();
  },

  async tunnellogs() {
    const r = await api('/api/tunnel/logs');
    if (!r?.ok) { toast('No tunnel logs', 'error'); return; }
    const box = $('tunnel-state');
    const existing = document.getElementById('tunnel-log-box');
    if (existing) { existing.remove(); return; }
    const pre = document.createElement('pre');
    pre.id = 'tunnel-log-box';
    pre.className = 'prog-logs';
    pre.style.marginLeft = '0';
    pre.textContent = (await r.json()).logs;
    box.after(pre);
    pre.scrollTop = pre.scrollHeight;
  },

  async selfcheck() {
    $('selfupdate-state').innerHTML = '<span class="tunnel-url">Checking GitHub…</span>';
    loadSelfUpdate();
  },

  async selfupdate() {
    if (!confirm('Update the harness now? It pulls the latest code and restarts, '
               + 'so this page stops responding for about a minute. '
               + 'Running programs keep running.')) return;
    const r = await api('/api/update', {method: 'POST'});
    if (!r) return;
    if (!r.ok) { toast(await detail(r, 'Could not start the update'), 'error', 8000); return; }
    toast((await r.json()).detail, 'info', 15000);
    $('selfupdate-state').innerHTML =
      '<span class="prog-badge wait">updating</span>'
      + '<span class="tunnel-url">Reload the page in a minute.</span>';
    $('selfupdate-actions').innerHTML = '';
  },

  async selflogs() {
    const r = await api('/api/update/logs');
    if (!r?.ok) { toast('No update log', 'error'); return; }
    const existing = document.getElementById('selfupdate-log-box');
    if (existing) { existing.remove(); return; }
    const pre = document.createElement('pre');
    pre.id = 'selfupdate-log-box';
    pre.className = 'prog-logs';
    pre.style.marginLeft = '0';
    pre.textContent = (await r.json()).logs;
    $('selfupdate-state').after(pre);
    pre.scrollTop = pre.scrollHeight;
  },

  async revoke(id, label) {
    if (!confirm(`Revoke the token "${label}"? Anything using it stops working immediately.`)) return;
    const r = await api(`/api/tokens/${encodeURIComponent(id)}`, {method: 'DELETE'});
    if (!r?.ok) { toast('Could not revoke the token', 'error'); return; }
    toast('Token revoked', 'success');
    loadTokens();
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

// ── The spec ───────────────────────────────────────────────────────────────────
// Fetched from the harness instead of duplicated here. It is also quoted in
// docs/programs.md, and three hand-maintained copies is three chances to drift.

async function copyPrompt() {
  const btn = $('prompt-btn'), label = btn.textContent;
  btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>';
  try {
    const r = await fetch('/api/prompt');
    if (!r.ok) throw new Error('unavailable');
    const spec = (await r.json()).prompt;
    try {
      await navigator.clipboard.writeText(spec);
    } catch {
      // The clipboard API needs HTTPS or localhost. Fall back for plain-HTTP LAN.
      const ta = document.createElement('textarea');
      ta.value = spec; ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta); ta.select();
      try { document.execCommand('copy'); } finally { ta.remove(); }
    }
    toast('Spec copied. Paste it into any AI along with your project.', 'success', 3500);
  } catch {
    toast('Could not load the spec', 'error');
  } finally {
    btn.disabled = false; btn.textContent = label;
  }
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
$('token-create-btn').addEventListener('click', createToken);
$('token-label-input').addEventListener('keydown', e => { if (e.key === 'Enter') createToken(); });
$('mcp-copy-btn').addEventListener('click', copyAgentSetup);
renderAgentSetup();
$('token-label-input').addEventListener('keydown', e => { if (e.key === 'Enter') createToken(); });
$('tunnel-named-btn').addEventListener('click', async () => {
  const token = $('tunnel-token-input').value.trim();
  const hostname = $('tunnel-host-input').value.trim();
  const r = await api('/api/tunnel', {method: 'POST',
    body: JSON.stringify({mode: 'named', token, hostname})});
  if (!r) return;
  if (!r.ok) { toast(await detail(r, 'Could not connect the tunnel'), 'error', 8000); return; }
  $('tunnel-token-input').value = '';
  toast('Named tunnel connected', 'success');
  loadTunnel(); loadPrograms();
});
$('logout-btn').addEventListener('click', logout);
$('pass-btn').addEventListener('click', changePassword);
$('theme-btn').addEventListener('click', () => {
  const next = document.documentElement.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('ph-theme', next);
});

boot();
