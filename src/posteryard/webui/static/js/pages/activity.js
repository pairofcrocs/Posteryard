// Page: activity. The live log, what changed, the queue and the web UI's jobs, one tab each.
import { api } from '../api.js';
import {
  h, mount, icon, button, card, badge, statusBadge, emptyState, errorState, spinner, tabs, segmented,
  timeAgo, formatDate, formatDuration, formatNumber, debounce, copyText, describeOutcome, toast, modal, TARGET_LABEL,
} from '../ui.js';
import { reasonLabel, recordThumb, itemArt, clock } from './dashboard.js';

const TABS = [
  { id: 'log', label: 'Log' },
  { id: 'changes', label: 'Changes' },
  { id: 'queue', label: 'Queue' },
  { id: 'jobs', label: 'Jobs' },
];

export default async function render(view, ctx) {
  ctx.setTitle('Activity');
  let active = TABS.some((t) => t.id === ctx.query.get('tab')) ? ctx.query.get('tab') : 'log';
  const body = h('div', { class: 'act-body' });
  let cleanup = null;

  const show = (id) => {
    if (cleanup) cleanup();
    cleanup = null;
    active = id;
    const query = new URLSearchParams(location.search);
    if (id === 'log') query.delete('tab');
    else query.set('tab', id);
    if (id !== 'changes') query.delete('status');
    ctx.setQuery(query);
    mount(body);
    const panel = h('div', { class: 'act-panel', role: 'tabpanel', 'aria-label': TABS.find((t) => t.id === id).label });
    body.append(panel);
    cleanup = PANELS[id](panel, ctx);
  };

  mount(view, h('div', { class: 'page-activity' }, tabs(TABS, active, show), body));
  show(active);
  return () => { if (cleanup) cleanup(); };
}

const PANELS = { log: logPanel, changes: changesPanel, queue: queuePanel, jobs: jobsPanel };

// ---- Log ----

const MAX_LINES = 2000;
const LEVEL_SHORT = { debug: 'DBG', info: 'INF', warning: 'WRN', error: 'ERR' };

function logLine(record) {
  const time = new Date(record.time);
  const stamp = Number.isNaN(time.getTime()) ? record.time : time.toLocaleTimeString([], { hour12: false });
  const fields = Object.entries(record.fields || {});
  const text = `${stamp} ${LEVEL_SHORT[record.level] || record.level} ${record.msg}${fields.map(([k, v]) => ` ${k}=${v}`).join('')}`;
  const line = h('div', { class: ['line', `lvl-${record.level}`], title: formatDate(record.time) },
    h('span', { class: 'act-time' }, stamp),
    h('span', { class: 'act-level' }, LEVEL_SHORT[record.level] || record.level),
    h('span', { class: 'act-msg' }, record.msg,
      fields.map(([k, v]) => h('span', { class: 'kv' }, ' ', k, '=', h('b', {}, String(v))))));
  line.dataset.text = text.toLowerCase();
  line.copyText = text;
  return line;
}

function logPanel(panel) {
  let level = 'info';
  let after = 0;
  let following = true;
  let filter = '';
  let pending = [];
  let stopped = false;
  let timer = null;
  let loading = false;
  let generation = 0;

  const box = h('div', { class: 'log act-log', role: 'log', 'aria-live': 'off', tabindex: '0', 'aria-label': 'Log' });
  const empty = h('div', { class: 'act-log-empty muted small' }, 'Loading the log…');
  const followButton = h('button', { type: 'button', class: 'btn small', 'aria-pressed': 'true', onClick: () => setFollowing(!following) });
  const filterInput = h('input', { class: 'input', type: 'search', placeholder: 'Filter lines', 'aria-label': 'Filter log lines',
    onInput: debounce(() => { filter = filterInput.value.trim().toLowerCase(); applyFilter(); }, 150) });
  const levels = segmented([['debug', 'Debug'], ['info', 'Info'], ['warning', 'Warning'], ['error', 'Error']], level, (value) => {
    level = value;
    reset();
  });
  levels.setAttribute('aria-label', 'Lowest level shown');
  const copy = button({ label: 'Copy', icon: 'copy', small: true, kind: 'ghost', onClick: async () => {
    const lines = [...box.querySelectorAll('.line:not(.hidden)')].map((line) => line.copyText);
    if (!lines.length) { toast('Nothing to copy', 'info', 1500); return; }
    await copyLines(lines.join('\n'));
  } });

  mount(panel,
    h('div', { class: 'act-toolbar' }, levels, h('div', { class: 'act-filter' }, icon('filter'), filterInput),
      h('div', { class: 'row gap-s act-toolbar-end' }, followButton, copy)),
    h('div', { class: 'card act-log-card' }, box, empty));

  function paintFollow() {
    followButton.setAttribute('aria-pressed', String(following));
    followButton.classList.toggle('primary', !following && pending.length > 0);
    mount(followButton, icon(following ? 'pause' : 'play'),
      following ? 'Pause' : pending.length ? `Follow (${formatNumber(pending.length)} new)` : 'Follow');
  }

  function setFollowing(on) {
    following = on;
    if (on && pending.length) {
      append(pending);
      pending = [];
    }
    paintFollow();
    if (on) toBottom();
  }

  function matches(line) {
    return !filter || line.dataset.text.includes(filter);
  }

  function applyFilter() {
    let shown = 0;
    for (const line of box.children) {
      const ok = matches(line);
      line.classList.toggle('hidden', !ok);
      if (ok) shown += 1;
    }
    paintEmpty(shown);
    if (following) toBottom();
  }

  function paintEmpty(shown = box.querySelectorAll('.line:not(.hidden)').length) {
    empty.classList.toggle('hidden', shown > 0);
    empty.textContent = box.children.length ? 'No lines match the filter.' : 'No log lines at this level yet.';
  }

  function append(records) {
    const fragment = document.createDocumentFragment();
    for (const record of records) {
      const line = logLine(record);
      if (!matches(line)) line.classList.add('hidden');
      fragment.append(line);
    }
    box.append(fragment);
    while (box.children.length > MAX_LINES) box.firstChild.remove();
    paintEmpty();
    if (following) toBottom();
  }

  async function poll() {
    if (loading || stopped) return;
    loading = true;
    const mine = generation;
    try {
      const data = await api.get(`/api/logs?after=${after}&level=${level}&limit=500`);
      if (stopped || mine !== generation) return;
      const records = data.records || [];
      if (records.length) after = Math.max(after, ...records.map((r) => r.seq));
      if (typeof data.last === 'number' && data.last > after) after = data.last;
      if (following) append(records);
      else {
        pending = pending.concat(records).slice(-MAX_LINES);
        if (records.length) paintFollow();
      }
    } catch (err) {
      if (mine === generation && !box.children.length) {
        empty.classList.remove('hidden');
        mount(empty, errorState(err, () => { mount(empty, 'Loading the log…'); poll(); }));
      }
    } finally {
      loading = false;
    }
  }

  function reset() {
    generation += 1;
    after = 0;
    pending = [];
    box.replaceChildren();
    empty.textContent = 'Loading the log…';
    empty.classList.remove('hidden');
    loading = false;
    paintFollow();
    poll();
  }

  let ownScroll = false;
  function toBottom() {
    if (box.scrollHeight - box.clientHeight - box.scrollTop < 1) return;
    ownScroll = true;
    box.scrollTop = box.scrollHeight;
  }

  // Scrolling up by hand stops following.
  box.addEventListener('scroll', () => {
    if (ownScroll) { ownScroll = false; return; }
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 24;
    if (following && !atBottom) { following = false; paintFollow(); }
  });

  paintFollow();
  poll();
  const loop = () => {
    timer = setTimeout(async () => {
      if (!document.hidden) await poll();
      if (!stopped) loop();
    }, 2000);
  };
  loop();
  return () => { stopped = true; clearTimeout(timer); };
}

// The Clipboard API needs HTTPS or localhost; a LAN address over HTTP falls back to the old copy command.
async function copyLines(text) {
  try {
    await copyText(text);
    return;
  } catch { /* try the fallback */ }
  const area = h('textarea', { readonly: true, 'aria-hidden': 'true', style: { position: 'fixed', top: '0', left: '0', opacity: '0' } }, text);
  document.body.append(area);
  area.select();
  let copied = false;
  try { copied = document.execCommand('copy'); } catch { /* not allowed */ }
  area.remove();
  if (copied) { toast('Copied', 'success', 1500); return; }
  const shown = h('textarea', { class: 'input mono', readonly: true, rows: 14, 'aria-label': 'Log lines' }, text);
  modal({ title: 'Copy the log', wide: true, content: [h('p', { class: 'small muted', style: { marginBottom: '10px' } }, 'This browser does not let the page copy. Select the lines and copy them yourself.'), shown] });
  shown.focus();
  shown.select();
}

// ---- Changes ----

const STATUS_FILTERS = [['', 'All'], ['uploaded', 'Uploaded'], ['preview', 'Previews'], ['manual', 'By hand'], ['failed', 'Failed'], ['uploading', 'Uploading']];
const PAGE = 50;

function changesPanel(panel, ctx) {
  let status = new URLSearchParams(location.search).get('status') || '';
  if (!STATUS_FILTERS.some(([v]) => v === status)) status = '';
  let offset = 0;
  let total = 0;
  let stopped = false;
  let generation = 0;

  const chips = h('div', { class: 'chips', role: 'group', 'aria-label': 'Status' });
  const rows = h('tbody');
  const table = h('table', { class: 'table act-table' },
    h('thead', {}, h('tr', {}, h('th', {}, h('span', { class: 'sr-only' }, 'Picture')), h('th', {}, 'Title'), h('th', {}, 'Image'),
      h('th', {}, 'Status'), h('th', {}, 'When'))),
    rows);
  const content = h('div');
  const foot = h('div', { class: 'act-foot' });

  function paintChips() {
    mount(chips, STATUS_FILTERS.map(([value, label]) => h('button', {
      type: 'button', class: ['chip', value === status && 'active'], 'aria-pressed': String(value === status),
      onClick: () => {
        if (value === status) return;
        status = value;
        const query = new URLSearchParams(location.search);
        if (status) query.set('status', status); else query.delete('status');
        ctx.setQuery(query);
        paintChips();
        load(true);
      },
    }, label)));
  }

  function row(r) {
    const href = `/item/${encodeURIComponent(r.key)}`;
    return h('tr', {},
      h('td', { class: 'act-thumb' }, h('a', { href, 'data-link': '', tabindex: '-1', 'aria-hidden': 'true' },
        recordThumb(r.key, r.target, { source: r.status === 'preview' ? 'preview' : 'auto', bust: r.updated_at }))),
      h('td', { class: 'act-title' },
        h('a', { class: 'act-title-link', href, 'data-link': '' }, r.title || `Title ${r.key}`),
        r.status === 'failed' && r.last_error ? h('div', { class: 'act-error' }, r.last_error) : null),
      h('td', { class: 'act-target muted' }, TARGET_LABEL[r.target] || r.target),
      h('td', { class: 'act-status' }, statusBadge(r.status),
        r.failures > 1 ? h('span', { class: 'small muted' }, ` ${r.failures}×`) : null),
      h('td', { class: 'act-when small muted nowrap', title: formatDate(r.updated_at) }, timeAgo(r.updated_at)));
  }

  async function load(fresh) {
    const mine = ++generation;
    if (fresh) {
      offset = 0;
      rows.replaceChildren();
      mount(content, spinner());
      mount(foot);
    }
    let data;
    try {
      const query = new URLSearchParams({ limit: String(PAGE), offset: String(offset) });
      if (status) query.set('status', status);
      data = await api.get(`/api/activity?${query}`);
    } catch (err) {
      if (stopped || mine !== generation) return;
      if (fresh) mount(content, errorState(err, () => load(true)));
      else toast(err.message, 'error');
      return;
    }
    if (stopped || mine !== generation) return;
    const records = data.records || [];
    total = data.total ?? records.length;
    offset += records.length;
    rows.append(...records.map(row));
    if (!rows.children.length) {
      mount(content, emptyState({ icon: 'activity', title: status ? 'Nothing with this status' : 'No changes yet',
        message: status ? 'Pick another status, or All.' : 'Posters, backgrounds and thumbnails show up here as Posteryard makes them.' }));
      mount(foot);
      return;
    }
    if (fresh) mount(content, h('div', { class: 'card act-table-card' }, table));
    mount(foot,
      h('span', { class: 'small muted' }, `Showing ${formatNumber(rows.children.length)} of ${formatNumber(total)}`),
      offset < total ? button({ label: 'Load more', icon: 'chevronDown', small: true, onClick: () => load(false) }) : null);
  }

  mount(panel, h('div', { class: 'act-toolbar' }, chips,
    h('div', { class: 'act-toolbar-end' }, button({ label: 'Refresh', icon: 'refresh', small: true, kind: 'ghost', onClick: () => load(true) }))),
  content, foot);
  paintChips();
  load(true);
  return () => { stopped = true; };
}

// ---- Queue ----

const PRIORITY = { 0: ['Urgent', 'accent'], 1: ['Normal', ''], 2: ['Background', ''] };

function queuePanel(panel) {
  let stopped = false;
  let signature = '';
  let timer = null;
  let started = null;
  const elapsed = h('span', { class: 'tabular' });
  const content = h('div', {}, spinner());
  mount(panel, content);

  async function load() {
    let data;
    try {
      data = await api.get('/api/queue');
    } catch (err) {
      if (!stopped && !signature) mount(content, errorState(err, load));
      return;
    }
    if (stopped) return;
    const sig = JSON.stringify([data.total, data.held, data.current?.key, data.current?.label, (data.items || []).map((i) => `${i.key}/${i.reason}`)]);
    if (sig === signature) return;
    signature = sig;
    paint(data);
  }

  function paint(data) {
    const items = data.items || [];
    const current = data.current;
    started = current?.started || null;
    tickElapsed();
    const now = current
      ? h('div', { class: 'act-now card' },
        current.reason === 'ui' && current.label ? h('div', { class: 'act-now-job' }, icon('wand', 'l'))
          : itemArt(current.key, 'act-now-poster'),
        h('div', { class: 'grow' }, h('div', { class: 'small muted' }, 'Working on'),
          current.reason === 'ui' && current.label ? h('div', { class: 'act-now-title' }, current.label)
            : h('a', { class: 'act-now-title', href: `/item/${encodeURIComponent(current.key)}`, 'data-link': '' }, current.title || `Title ${current.key}`),
          h('div', { class: 'row wrap', style: { marginTop: '6px' } }, badge(reasonLabel(current.reason), 'accent'),
            started ? h('span', { class: 'small muted' }, 'for ', elapsed) : null)))
      : null;
    const reasons = Object.entries(data.by_reason || {}).filter(([, n]) => n > 0).sort((a, b) => b[1] - a[1]);
    if (!current && !items.length && !data.held) {
      mount(content, h('div', { class: 'card' }, emptyState({ icon: 'check', title: 'Nothing waiting',
        message: 'Posteryard is idle. Sweeps, webhooks and full passes add titles here.' })));
      return;
    }
    const total = data.total ?? items.length;
    mount(content, now,
      h('div', { class: 'row wrap act-reasons' }, h('strong', {}, `${formatNumber(total)} waiting`),
        reasons.map(([reason, n]) => h('span', { class: 'pill' }, reasonLabel(reason), h('span', { class: 'muted' }, formatNumber(n)))),
        data.held ? h('span', { class: 'pill paused' }, `${formatNumber(data.held)} held until you resume`) : null),
      items.length ? card({ flush: true, body: h('ol', { class: 'list act-queue' }, items.map((item, i) => {
        const [prio, tone] = PRIORITY[item.priority] || ['', ''];
        return h('li', { class: 'list-item' },
          h('span', { class: 'act-pos muted tabular' }, String(i + 1)),
          itemArt(item.key, 'thumb', 80),
          h('div', { class: 'grow' },
            h('a', { class: 'act-title-link', href: `/item/${encodeURIComponent(item.key)}`, 'data-link': '' }, item.title || `Title ${item.key}`),
            h('div', { class: 'small muted' }, reasonLabel(item.reason))),
          prio && item.priority !== 1 ? badge(prio, tone) : null);
      })) }) : null,
      total > items.length ? h('p', { class: 'small muted act-foot' }, `And ${formatNumber(total - items.length)} more.`) : null);
  }

  function tickElapsed() {
    elapsed.textContent = started ? clock(Date.now() / 1000 - started) : '';
  }

  load();
  const loop = () => {
    timer = setTimeout(async () => {
      if (!document.hidden) await load();
      if (!stopped) loop();
    }, 3000);
  };
  loop();
  const ticker = setInterval(tickElapsed, 1000);
  return () => { stopped = true; clearTimeout(timer); clearInterval(ticker); };
}

// ---- Jobs ----

const JOB_STATE = { queued: 'Waiting', running: 'Running', done: 'Done', failed: 'Failed' };

function jobsPanel(panel) {
  let stopped = false;
  let timer = null;
  let signature = '';
  let busy = false;
  const content = h('div', {}, spinner());
  mount(panel,
    h('div', { class: 'act-toolbar' }, h('p', { class: 'small muted grow' }, 'Work started from the web UI, newest first. It runs on the worker between library titles.'),
      button({ label: 'Refresh', icon: 'refresh', small: true, kind: 'ghost', onClick: () => load(true) })),
    content);

  function duration(job) {
    if (!job.started) return job.state === 'queued' ? `waiting ${formatDuration(Date.now() / 1000 - job.created)}` : '—';
    const took = (job.finished || Date.now() / 1000) - job.started;
    return took < 1 ? 'under 1s' : formatDuration(took);
  }

  function result(job) {
    if (job.state === 'failed') return h('div', { class: 'act-error' }, job.error || 'Failed');
    if (job.state === 'done' && job.result && typeof job.result === 'object' && job.result.outcome) {
      return h('span', { class: 'small muted' }, describeOutcome(job.result));
    }
    return null;
  }

  async function load(force = false) {
    if (busy) return 'busy';
    busy = true;
    let data;
    try {
      data = await api.get('/api/jobs?limit=50');
    } catch (err) {
      if (!stopped && !signature) mount(content, errorState(err, () => load(true)));
      return 'error';
    } finally {
      busy = false;
    }
    if (stopped) return 'stopped';
    const jobs = data.jobs || [];
    const running = jobs.some((j) => j.state === 'queued' || j.state === 'running');
    const sig = JSON.stringify(jobs.map((j) => [j.id, j.state])) + (running ? Date.now() : '');
    if (sig !== signature || force) {
      signature = sig;
      paint(jobs);
    }
    return running ? 'running' : 'idle';
  }

  function paint(jobs) {
    if (!jobs.length) {
      mount(content, h('div', { class: 'card' }, emptyState({ icon: 'wand', title: 'No jobs yet',
        message: 'Rendering a title, picking art or sending a test alert from the web UI runs as a job, and shows up here.' })));
      return;
    }
    mount(content, h('div', { class: 'card act-table-card' }, h('table', { class: 'table act-jobs' },
      h('thead', {}, h('tr', {}, h('th', {}, 'State'), h('th', {}, 'Job'), h('th', {}, 'Started'), h('th', {}, 'Took'))),
      h('tbody', {}, jobs.map((job) => h('tr', {},
        h('td', { class: 'act-state' }, h('span', { class: ['badge', job.state] }, job.state === 'running' && h('span', { class: 'dot pulse' }), JOB_STATE[job.state] || job.state)),
        h('td', { class: 'act-title' }, h('div', { class: 'act-job-label' }, job.label || job.kind),
          h('div', { class: 'small faint' }, job.kind), result(job)),
        h('td', { class: 'act-when small muted nowrap', title: formatDate(job.created) }, timeAgo(job.started || job.created)),
        h('td', { class: 'act-took small muted nowrap tabular' }, h('span', { class: 'act-took-label' }, 'Took '), duration(job))))))));
  }

  const loop = async () => {
    const state = document.hidden && signature ? 'idle' : await load();
    if (stopped) return;
    timer = setTimeout(loop, state === 'running' || state === 'busy' ? 2000 : 10000);
  };
  loop();
  return () => { stopped = true; clearTimeout(timer); };
}
