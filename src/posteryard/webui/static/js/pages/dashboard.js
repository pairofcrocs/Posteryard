// Page: dashboard. What Posteryard is doing, what it did lately, and the buttons to run it now.
import { api, imageUrl } from '../api.js';
import {
  h, mount, icon, button, card, stat, badge, toast, confirmDialog, emptyState, errorState, posterTile, lazyImage,
  timeAgo, formatDate, formatNumber, TARGET_LABEL,
} from '../ui.js';

// Why a title is in the queue. Shared with the activity page.
export const REASON_LABEL = {
  webhook: 'Webhook', changed: 'New or changed', label: 'Label changed', unignored: 'No longer ignored', ui: 'Web UI',
  daily: 'Full pass', unlisted: 'Gone from the library', retry: 'Retry', 'live tv': 'Live TV',
  'resumed after a restart': 'Resumed after a restart', maintainerr: 'Maintainerr',
};

export function reasonLabel(reason) {
  if (!reason) return 'Other';
  return REASON_LABEL[reason] || reason.charAt(0).toUpperCase() + reason.slice(1);
}

/** A small poster (or wide thumbnail) for a record's target. */
export function recordThumb(key, target, { source = 'auto', bust = null } = {}) {
  const wide = target === 'art' || target === 'thumb';
  const imageTarget = target === 'item' || !target ? 'poster' : target;
  return lazyImage(imageUrl(key, { target: imageTarget, source, w: wide ? 160 : 90, bust }), { className: ['thumb', wide && 'wide'].filter(Boolean).join(' ') });
}

/** An item's poster, or its thumbnail when it has none (episodes), for a key of unknown kind. */
export function itemArt(key, className, w = 120) {
  const img = h('img', { src: imageUrl(key, { w }), alt: '', class: className, loading: 'lazy', decoding: 'async' });
  let tried = false;
  img.addEventListener('error', () => {
    if (tried) { img.replaceWith(h('div', { class: ['placeholder-art', className] })); return; }
    tried = true;
    img.classList.add('wide');
    img.src = imageUrl(key, { target: 'thumb', w: w * 2 });
  });
  return img;
}

/** "12:03" or "1:02:03" for a countdown. */
export function clock(seconds) {
  const s = Math.max(0, Math.round(seconds));
  const hh = Math.floor(s / 3600);
  const mm = Math.floor((s % 3600) / 60);
  const ss = String(s % 60).padStart(2, '0');
  return hh ? `${hh}:${String(mm).padStart(2, '0')}:${ss}` : `${mm}:${ss}`;
}

const CHECKS = {
  threads_running: 'A background thread stopped. Restart Posteryard and check its log.',
  worker_responsive: 'The worker has not answered for a while. It may be stuck on a slow title.',
  sweep_recent: 'No sweep finished recently. Check the log for errors.',
};

const EXTRAS = [
  ['status_labels', 'Status labels', 'JUST ADDED, NEW EPISODE and NEW SEASON'],
  ['maintainerr', 'Maintainerr countdown', 'LEAVING IN 5 DAYS'],
  ['text_check', 'Text check', 'Skips art with printed titles'],
  ['collection_posters', 'Collection posters', null],
  ['service_collections', 'Service collections', 'One per streaming service'],
  ['live_tv', 'Live TV posters', null],
  ['heartbeat', 'Heartbeat', 'HEARTBEAT_URL'],
];

const MODES = {
  live: ['live', 'Live', 'Uploads to the server'],
  dry: ['dry', 'Dry run', 'Saves previews, uploads nothing'],
  paused: ['paused', 'Paused', 'Nothing runs until you resume'],
};

function modeOf(s) {
  return s.paused ? MODES.paused : s.dry_run ? MODES.dry : MODES.live;
}

export default async function render(view, ctx) {
  ctx.setTitle('Dashboard');
  const root = h('div', { class: 'page-dashboard' });
  mount(view, root);

  let status = null;
  let receivedAt = Date.now();
  let unsubscribe = null;
  let ticker = null;
  let stripSignature = '';
  let stripBusy = false;
  let stopped = false;
  const live = {}; // Elements the one-second tick updates.

  const slots = {
    hero: h('section', { class: 'dash-hero card', 'aria-label': 'Server' }),
    notices: h('div', { class: 'dash-notices' }),
    stats: h('div', { class: 'dash-stats' }),
    now: h('div'),
    schedule: h('div'),
    strip: h('div'),
    features: h('div'),
  };

  function skeleton() {
    mount(root,
      h('div', { class: 'dash-hero card' }, h('div', { class: 'skeleton', style: { height: '64px', width: '100%' } })),
      h('div', { class: 'dash-stats' }, [1, 2, 3, 4, 5].map(() => h('div', { class: 'card stat' },
        h('div', { class: 'skeleton', style: { height: '14px', width: '60%' } }),
        h('div', { class: 'skeleton', style: { height: '30px', width: '40%', marginTop: '10px' } })))),
      h('div', { class: 'dash-columns' }, [1, 2].map(() => h('div', { class: 'card', style: { height: '220px' } }))));
  }

  async function load() {
    skeleton();
    try {
      status = ctx.status || await api.get('/api/status');
    } catch (err) {
      if (!stopped) mount(root, errorState(err, load));
      return;
    }
    if (stopped) return;
    receivedAt = Date.now();
    mount(root, slots.hero, slots.notices, slots.stats,
      h('div', { class: 'dash-columns' }, slots.now, slots.schedule), slots.strip, slots.features);
    paint();
    loadStrip();
    if (!unsubscribe) {
      unsubscribe = ctx.onStatus((next) => {
        if (stopped || next === status) return;
        status = next;
        receivedAt = Date.now();
        paint();
        loadStrip();
      });
    }
    if (!ticker) ticker = setInterval(tick, 1000);
  }

  // Each section repaints only when its own data changed, so focus and button states survive the 8 s poll.
  const painted = {};
  function section(name, data, fn) {
    const signature = JSON.stringify(data);
    if (painted[name] === signature) return;
    painted[name] = signature;
    fn();
  }

  function paint() {
    const s = status;
    section('hero', [s.server, s.libraries, s.ok, s.checks, s.paused, s.dry_run, Boolean(s.current)], paintHero);
    section('notices', [s.dry_run, s.alerts], paintNotices);
    section('stats', [s.images, s.queue?.total], paintStats);
    section('now', [s.current, s.paused, s.queue], paintNow);
    section('schedule', [s.paused, s.full_pass_running, s.last_full_pass, s.sweep_minutes, s.daily_at], paintSchedule);
    section('features', [s.features, s.art_sources], paintFeatures);
    tick();
  }

  function paintHero() {
    const s = status;
    const [cls, text, sub] = modeOf(s);
    const failing = Object.entries(s.checks || {}).filter(([, ok]) => !ok);
    const health = s.ok && !failing.length
      ? h('div', { class: 'dash-health ok' }, icon('check'), 'All checks pass')
      : h('div', { class: 'dash-health bad', role: 'alert' }, icon('alert'),
        h('div', { class: 'col gap-s' }, h('strong', {}, 'Something is wrong'),
          failing.length ? failing.map(([name]) => h('span', {}, CHECKS[name] || `Check failed: ${name.replaceAll('_', ' ')}.`))
            : h('span', {}, 'Posteryard reports a problem. Check the log.')));
    const libraries = s.libraries || [];
    mount(slots.hero,
      h('div', { class: 'dash-hero-main' },
        h('div', { class: 'dash-server-icon' }, icon('server', 'l')),
        h('div', { class: 'grow' },
          h('div', { class: 'dash-server-name' }, s.server?.name || 'Media server',
            s.server?.url && h('span', { class: 'muted small dash-server-url truncate' }, s.server.url)),
          h('div', { class: 'dash-libraries', 'aria-label': 'Libraries' },
            libraries.length ? libraries.map((name) => h('span', { class: 'pill' }, icon('film'), name))
              : h('span', { class: 'muted small' }, 'No libraries chosen')))),
      h('div', { class: 'dash-hero-side' },
        h('div', { class: `pill dash-mode ${cls}`, title: sub }, h('span', { class: ['dot', s.current && 'pulse'] }), text),
        h('div', { class: 'small muted' }, sub),
        health));
  }

  function paintNotices() {
    const s = status;
    const parts = [];
    if (s.dry_run) {
      parts.push(h('section', { class: 'dash-preview card' },
        h('div', { class: 'dash-preview-icon' }, icon('eye', 'l')),
        h('div', { class: 'grow' },
          h('h2', {}, 'You are in preview mode'),
          h('p', {}, 'Dry run is on. Posteryard saves every poster it makes as a preview and uploads nothing, so your server stays as it is. Look through the previews, and turn off Dry run in Settings when you like what you see.')),
        h('div', { class: 'row wrap' },
          h('a', { class: 'btn primary', href: '/previews', 'data-link': '' }, icon('image'), 'See previews'),
          h('a', { class: 'btn', href: '/settings', 'data-link': '' }, icon('gear'), 'Go live in Settings'))));
    }
    const alerts = s.alerts || [];
    if (alerts.length) {
      parts.push(card({
        title: alerts.length === 1 ? 'Open alert' : `${alerts.length} open alerts`,
        className: 'dash-alerts',
        actions: h('a', { class: 'btn ghost small', href: '/problems', 'data-link': '' }, 'Problems', icon('chevronRight')),
        flush: true,
        body: h('div', { class: 'list' }, alerts.map((alert) => h('div', { class: 'list-item' },
          h('span', { class: 'dash-alert-icon' }, icon('bell')),
          h('div', { class: 'grow' }, h('div', {}, alert.cause || 'Unknown problem'),
            h('div', { class: 'small muted', title: formatDate(alert.since) }, `Opened ${timeAgo(alert.since)}`))))),
      }));
    }
    mount(slots.notices, parts);
  }

  function paintStats() {
    const images = status.images || {};
    const queue = status.queue || { total: 0 };
    const failed = images.failed || 0;
    const uploading = images.uploading || 0;
    mount(slots.stats,
      stat({ label: 'Uploaded', value: formatNumber(images.uploaded || 0), tone: images.uploaded ? 'green' : '', iconName: 'upload',
        hint: uploading ? `${formatNumber(uploading)} uploading now` : 'On the server' }),
      h('a', { class: 'dash-stat-link', href: '/previews', 'data-link': '', 'aria-label': `${images.preview || 0} previews` },
        stat({ label: 'Previews', value: formatNumber(images.preview || 0), tone: images.preview ? 'blue' : '', iconName: 'eye', hint: 'Saved, not uploaded' })),
      stat({ label: 'By hand', value: formatNumber(images.manual || 0), tone: images.manual ? 'purple' : '', iconName: 'lock', hint: 'Left alone' }),
      h('a', { class: 'dash-stat-link', href: '/problems', 'data-link': '', 'aria-label': `${failed} failed, open Problems` },
        stat({ label: 'Failed', value: formatNumber(failed), tone: failed ? 'red' : '', iconName: 'alert',
          hint: failed ? 'See Problems' : 'Nothing failed' })),
      h('a', { class: 'dash-stat-link', href: '/activity?tab=queue', 'data-link': '', 'aria-label': `${queue.total || 0} in the queue` },
        stat({ label: 'Queue', value: formatNumber(queue.total || 0), tone: queue.total ? 'orange' : '', iconName: 'layers',
          hint: queue.total ? 'Waiting to run' : 'Nothing waiting' })));
  }

  function paintNow() {
    const s = status;
    const current = s.current;
    let body;
    live.elapsed = null;
    if (current) {
      const isJob = current.reason === 'ui' && current.label;
      const name = current.title || current.label || `Title ${current.key}`;
      live.elapsed = h('span', { class: 'tabular' }, '');
      const head = h('div', { class: 'dash-now-item' },
        isJob ? h('div', { class: 'dash-now-job' }, icon('wand', 'l')) : h('a', { href: `/item/${encodeURIComponent(current.key)}`, 'data-link': '', 'aria-label': name, tabindex: '-1' },
          itemArt(current.key, 'dash-now-poster')),
        h('div', { class: 'grow' },
          h('div', { class: 'small muted' }, 'Working on'),
          isJob ? h('div', { class: 'dash-now-title' }, name)
            : h('a', { class: 'dash-now-title', href: `/item/${encodeURIComponent(current.key)}`, 'data-link': '' }, name),
          h('div', { class: 'row wrap', style: { marginTop: '6px' } }, badge(reasonLabel(current.reason), 'accent'),
            h('span', { class: 'small muted' }, 'for ', live.elapsed))));
      body = head;
    } else {
      body = h('div', { class: 'dash-idle' },
        h('div', { class: 'dash-idle-icon' }, icon(s.paused ? 'pause' : 'check', 'l')),
        h('div', {}, h('div', { class: 'dash-now-title' }, s.paused ? 'Paused' : 'Idle'),
          h('div', { class: 'small muted' }, s.paused ? 'Resume to let sweeps and full passes run again.'
            : 'Nothing to do until the next sweep.')));
    }
    mount(slots.now, card({
      title: 'Now', className: 'dash-now',
      actions: h('a', { class: 'btn ghost small', href: '/activity?tab=queue', 'data-link': '' }, 'Queue', icon('chevronRight')),
      body: h('div', { class: 'col gap-l' }, body, queueBreakdown(s.queue || {})),
    }));
  }

  function queueBreakdown(queue) {
    const entries = Object.entries(queue.by_reason || {}).filter(([, n]) => n > 0).sort((a, b) => b[1] - a[1]);
    const total = queue.total || entries.reduce((sum, [, n]) => sum + n, 0);
    const held = queue.held ? h('div', { class: 'small', style: { color: 'var(--orange)' } }, `${formatNumber(queue.held)} held until you resume`) : null;
    if (!total) return held || h('div', { class: 'small muted dash-queue-empty' }, 'The queue is empty.');
    return h('div', { class: 'dash-queue' },
      h('div', { class: 'row between small' }, h('span', { class: 'muted' }, 'Waiting'), h('strong', {}, `${formatNumber(total)} ${total === 1 ? 'title' : 'titles'}`)),
      h('div', { class: 'dash-bar', role: 'img', 'aria-label': entries.map(([r, n]) => `${reasonLabel(r)} ${n}`).join(', ') },
        entries.map(([reason, n], i) => h('span', { class: `seg seg-${i % 6}`, style: { flexGrow: String(n) }, title: `${reasonLabel(reason)}: ${n}` }))),
      h('ul', { class: 'dash-legend' }, entries.map(([reason, n], i) => h('li', {},
        h('span', { class: `swatch seg-${i % 6}` }), h('span', { class: 'grow' }, reasonLabel(reason)), h('span', { class: 'tabular' }, formatNumber(n))))),
      held);
  }

  function paintSchedule() {
    const s = status;
    live.nextSweep = h('div', { class: 'dash-big tabular' }, '');
    live.lastSweep = h('div', { class: 'dash-big' }, '');
    const run = (path, done) => async () => {
      await api.post(path);
      toast(done, 'success');
      await ctx.refreshStatus();
    };
    const sweepButton = button({ label: 'Run sweep now', icon: 'refresh', kind: 'primary', small: true, disabled: s.paused,
      title: s.paused ? 'Resume first' : null, onClick: run('/api/actions/sweep', 'Sweep started') });
    const fullButton = button({ label: 'Run full pass now', icon: 'layers', small: true, disabled: s.paused || s.full_pass_running,
      title: s.paused ? 'Resume first' : s.full_pass_running ? 'A full pass is running' : null,
      onClick: async () => {
        const ok = await confirmDialog({ title: 'Run a full pass now?',
          message: 'Posteryard checks every title in your libraries. On a big library this takes a while; sweeps and webhooks still come first.',
          confirmLabel: 'Run full pass' });
        if (ok) await run('/api/actions/full', 'Full pass started')();
      } });
    const pauseButton = s.paused
      ? button({ label: 'Resume', icon: 'play', small: true, onClick: run('/api/actions/resume', 'Resumed') })
      : button({ label: 'Pause', icon: 'pause', small: true, kind: 'ghost', onClick: run('/api/actions/pause', 'Paused. Jobs from the web UI still run.') });
    const lastFull = s.last_full_pass ? formatDay(s.last_full_pass) : 'Not yet';
    mount(slots.schedule, card({
      title: 'Schedule', className: 'dash-schedule',
      body: h('div', { class: 'col gap-l' },
        h('div', { class: 'dash-bigs' },
          h('div', {}, h('div', { class: 'small muted' }, 'Next sweep'), live.nextSweep),
          h('div', {}, h('div', { class: 'small muted' }, 'Last sweep'), live.lastSweep)),
        h('dl', { class: 'props' },
          h('dt', {}, 'Sweeps'), h('dd', {}, s.sweep_minutes ? `Every ${s.sweep_minutes} minutes` : '—'),
          h('dt', {}, 'Full pass'), h('dd', {}, s.daily_at ? `Daily at ${s.daily_at}` : '—'),
          h('dt', {}, 'Last full pass'), h('dd', { class: 'row wrap gap-s' }, lastFull,
            s.full_pass_running && h('span', { class: 'badge running' }, h('span', { class: 'dot pulse' }), 'Running now'))),
        h('div', { class: 'row wrap dash-actions' }, sweepButton, fullButton, pauseButton)),
    }));
  }

  function paintFeatures() {
    const s = status;
    const f = s.features || {};
    const sources = s.art_sources || [];
    const alertsOn = Boolean(f.notify);
    mount(slots.features, card({
      title: 'Features', className: 'dash-features',
      actions: h('a', { class: 'btn ghost small', href: '/settings', 'data-link': '' }, 'Settings', icon('chevronRight')),
      body: h('div', { class: 'dash-feature-grid' },
        h('div', { class: 'col' }, h('div', { class: 'dash-feature-head' }, 'Art sources'),
          h('div', { class: 'row wrap gap-s' }, sources.length ? sources.map((name) => h('span', { class: 'pill' }, name))
            : h('span', { class: 'muted small' }, 'None'))),
        h('div', { class: 'col' }, h('div', { class: 'dash-feature-head' }, 'Alerts'),
          h('div', { class: ['dash-flag', alertsOn ? 'on' : 'off'] }, icon(alertsOn ? 'bell' : 'x'),
            alertsOn ? 'Set up' : h('span', {}, 'Not set up. ', h('a', { class: 'link', href: '/settings', 'data-link': '' }, 'Add NOTIFY_URLS'))),
          f.only_rating_keys ? h('div', { class: 'small', style: { color: 'var(--orange)' } },
            `Limited to ${f.only_rating_keys} ${f.only_rating_keys === 1 ? 'title' : 'titles'} (ONLY_RATING_KEYS)`) : null),
        h('div', { class: 'col dash-extras' }, h('div', { class: 'dash-feature-head' }, 'Extras'),
          h('ul', { class: 'dash-flags' }, EXTRAS.map(([name, label, hint]) => h('li', { class: ['dash-flag', f[name] ? 'on' : 'off'], title: hint || null },
            icon(f[name] ? 'check' : 'x'), h('span', {}, label), h('span', { class: 'sr-only' }, f[name] ? ' on' : ' off')))))),
    }));
  }

  async function loadStrip() {
    if (stripBusy) return;
    stripBusy = true;
    let records;
    try {
      records = (await api.get('/api/activity?limit=12')).records || [];
    } catch (err) {
      if (!stripSignature) mount(slots.strip, card({ title: 'Recently changed', body: errorState(err, loadStrip) }));
      return;
    } finally {
      stripBusy = false;
    }
    if (stopped) return;
    const signature = records.map((r) => `${r.key}/${r.target}/${r.updated_at}/${r.status}`).join('|') || 'empty';
    if (signature === stripSignature) return;
    stripSignature = signature;
    const hero = records.find((r) => r.target === 'art') || records[0];
    if (hero) {
      slots.hero.style.setProperty('--dash-hero-art', `url("${imageUrl(hero.key, { target: hero.target === 'item' ? 'poster' : hero.target, source: hero.status === 'preview' ? 'preview' : 'auto', w: 600, bust: hero.updated_at })}")`);
      slots.hero.classList.add('has-art');
    }
    const tiles = records.map((r) => {
      const wide = r.target === 'art' || r.target === 'thumb';
      const target = r.target === 'item' ? 'poster' : r.target;
      return posterTile({
        href: `/item/${encodeURIComponent(r.key)}`,
        src: imageUrl(r.key, { target, source: r.status === 'preview' ? 'preview' : 'auto', w: wide ? 480 : 300, bust: r.updated_at }),
        title: r.title || `Title ${r.key}`,
        subtitle: `${TARGET_LABEL[r.target] || r.target} · ${timeAgo(r.updated_at)}`,
        status: r.status,
        wide,
      });
    });
    mount(slots.strip, h('section', { class: 'dash-recent', 'aria-label': 'Recently changed' },
      h('div', { class: 'row between dash-section-head' }, h('h2', {}, 'Recently changed'),
        h('a', { class: 'btn ghost small', href: '/activity?tab=changes', 'data-link': '' }, 'All changes', icon('chevronRight'))),
      tiles.length ? h('div', { class: 'dash-strip' }, tiles)
        : h('div', { class: 'card' }, emptyState({ icon: 'image', title: 'Nothing changed yet',
          message: 'Posters Posteryard makes or uploads show up here, newest first.' }))));
  }

  function tick() {
    if (!status) return;
    const passed = (Date.now() - receivedAt) / 1000;
    if (live.nextSweep) {
      const next = status.next_sweep_seconds;
      if (status.paused) live.nextSweep.textContent = 'Paused';
      else if (next === null || next === undefined) live.nextSweep.textContent = '—';
      else live.nextSweep.textContent = next - passed > 0 ? clock(next - passed) : 'Now';
    }
    if (live.lastSweep) {
      const ago = status.last_sweep_seconds_ago;
      live.lastSweep.textContent = ago === null || ago === undefined ? 'Not yet' : shortAgo(ago + passed);
    }
    if (live.elapsed && status.current?.started) live.elapsed.textContent = clock(Date.now() / 1000 - status.current.started);
  }

  await load();
  return () => {
    stopped = true;
    if (unsubscribe) unsubscribe();
    if (ticker) clearInterval(ticker);
  };
}

/** "Just now", "4 min ago", "2 h ago": short enough for the big schedule figures. */
function shortAgo(seconds) {
  if (seconds < 60) return 'Just now';
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  return `${Math.floor(seconds / 86400)} d ago`;
}

/** "Today", "Yesterday" or the date, for a YYYY-MM-DD day. */
function formatDay(day) {
  const date = new Date(`${day}T00:00:00`);
  if (Number.isNaN(date.getTime())) return String(day);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const days = Math.round((today - date) / 86400000);
  if (days === 0) return 'Today';
  if (days === 1) return 'Yesterday';
  return date.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: date.getFullYear() === today.getFullYear() ? undefined : 'numeric' });
}
