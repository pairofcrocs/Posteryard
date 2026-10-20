// Page: problems. Titles that failed, images changed by hand, and open alerts.
import { api } from '../api.js';
import {
  h, mount, icon, button, card, badge, emptyState, errorState, spinner, runJob, toast,
  timeAgo, formatDate, formatNumber, TARGET_LABEL,
} from '../ui.js';
import { recordThumb } from './dashboard.js';

export default async function render(view, ctx) {
  ctx.setTitle('Problems');
  const root = h('div', { class: 'page-problems' });
  mount(view, root);
  let stopped = false;
  let refreshTimer = null;

  async function load() {
    mount(root, spinner());
    let data;
    try {
      data = await api.get('/api/problems');
    } catch (err) {
      if (!stopped) mount(root, errorState(err, load));
      return;
    }
    if (stopped) return;
    paint(data);
  }

  // Reload a moment after a retry, once the worker has had a chance to pick it up.
  function reloadSoon(ms = 1500) {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(async () => {
      if (stopped) return;
      try {
        const data = await api.get('/api/problems');
        if (!stopped) paint(data);
      } catch { /* the page keeps what it shows */ }
      ctx.refreshStatus();
    }, ms);
  }

  async function retry(keys, label) {
    const result = await api.post('/api/problems/retry', keys ? { keys } : {});
    const n = result?.queued ?? 0;
    toast(n ? `${label}: queued ${n === 1 ? 'for a retry' : `${formatNumber(n)} titles for a retry`}` : `${label}: nothing to queue`, n ? 'success' : 'info');
    reloadSoon();
  }

  function title(r) {
    return r.title || `Title ${r.key}`;
  }

  function failedRow(r) {
    const href = `/item/${encodeURIComponent(r.key)}`;
    return h('li', { class: 'prob-row' },
      h('a', { class: 'prob-thumb', href, 'data-link': '', tabindex: '-1', 'aria-hidden': 'true' }, recordThumb(r.key, r.target, { bust: r.updated_at })),
      h('div', { class: 'prob-main' },
        h('div', { class: 'prob-head' },
          h('a', { class: 'prob-title', href, 'data-link': '' }, title(r)),
          h('span', { class: 'badge' }, TARGET_LABEL[r.target] || r.target),
          r.failures ? h('span', { class: 'badge failed' }, r.failures === 1 ? 'Failed once' : `Failed ${formatNumber(r.failures)} times`) : null),
        h('div', { class: 'prob-error' }, r.last_error || 'No error was recorded.'),
        h('div', { class: 'small muted', title: formatDate(r.updated_at) }, `Last tried ${timeAgo(r.updated_at)}`)),
      h('div', { class: 'prob-actions' },
        labelled(button({ label: 'Retry', icon: 'refresh', small: true, onClick: () => retry([r.key], title(r)) }), `Retry ${title(r)}`)));
  }

  function manualRow(r) {
    const href = `/item/${encodeURIComponent(r.key)}`;
    return h('li', { class: 'prob-row' },
      h('a', { class: 'prob-thumb', href, 'data-link': '', tabindex: '-1', 'aria-hidden': 'true' }, recordThumb(r.key, r.target, { source: 'server', bust: r.updated_at })),
      h('div', { class: 'prob-main' },
        h('div', { class: 'prob-head' },
          h('a', { class: 'prob-title', href, 'data-link': '' }, title(r)),
          h('span', { class: 'badge manual' }, TARGET_LABEL[r.target] || r.target)),
        h('div', { class: 'small muted', title: formatDate(r.updated_at) }, `Changed ${timeAgo(r.updated_at)}`)),
      h('div', { class: 'prob-actions' },
        labelled(button({ label: 'Hand back to Posteryard', icon: 'undo', small: true, onClick: async () => {
          const result = await runJob(`/api/items/${encodeURIComponent(r.key)}/forget`, {}, `Hand back ${title(r)}`);
          if (result) reloadSoon(300);
        } }), `Hand ${title(r)} back to Posteryard`)));
  }

  function paint(data) {
    const failed = data.failed || [];
    const manual = data.manual || [];
    const alerts = data.alerts || [];
    const failedTitles = new Set(failed.map((r) => r.key)).size;
    const summary = failed.length
      ? `${formatNumber(failedTitles)} ${failedTitles === 1 ? 'title' : 'titles'} failed`
      : 'Nothing failed';
    const head = h('div', { class: 'page-head prob-page-head' },
      h('div', { class: 'grow' },
        h('div', { class: 'row wrap' }, h('h2', {}, summary),
          failed.length ? h('span', { class: 'badge failed', 'aria-label': `${failed.length} failed images` }, formatNumber(failed.length)) : null,
          manual.length ? h('span', { class: 'badge manual' }, `${formatNumber(manual.length)} by hand`) : null,
          alerts.length ? h('span', { class: 'badge orange' }, `${formatNumber(alerts.length)} ${alerts.length === 1 ? 'alert' : 'alerts'}`) : null),
        failed.length ? h('p', {}, 'Posteryard retries failed titles on its own at later sweeps. Retry now once you fixed the cause.') : null),
      failed.length ? button({ label: 'Retry all', icon: 'refresh', kind: 'primary', onClick: () => retry(null, 'Retry all') }) : null);

    if (!failed.length && !manual.length && !alerts.length) {
      mount(root, head, h('div', { class: 'card prob-clear' }, emptyState({ icon: 'check', title: 'All clear',
        message: 'Every poster is in order and nothing needs you. Problems show up here if a title fails.',
        action: h('a', { class: 'btn', href: '/activity?tab=changes', 'data-link': '' }, 'See recent changes') })));
      return;
    }

    const sections = [head];
    if (alerts.length) {
      sections.push(card({
        title: alerts.length === 1 ? 'Open alert' : 'Open alerts', className: 'prob-alerts', flush: true,
        body: h('ul', { class: 'list' }, alerts.map((alert) => h('li', { class: 'list-item' },
          h('span', { class: 'prob-alert-icon' }, icon('bell')),
          h('div', { class: 'grow' }, h('div', {}, alert.cause || 'Unknown problem'),
            h('div', { class: 'small muted', title: formatDate(alert.since) }, `Opened ${timeAgo(alert.since)}`))))),
      }));
    }
    if (failed.length) {
      sections.push(card({ title: 'Failed', className: 'prob-failed', flush: true,
        actions: badge(formatNumber(failed.length), 'failed'),
        body: h('ul', { class: 'prob-list' }, failed.map(failedRow)) }));
    } else {
      sections.push(h('div', { class: 'card prob-none' }, h('div', { class: 'row' }, h('span', { class: 'prob-ok' }, icon('check')),
        h('span', {}, 'No title failed. Nice.'))));
    }
    if (manual.length) {
      sections.push(card({ title: 'Changed by hand', className: 'prob-manual', flush: true,
        actions: badge(formatNumber(manual.length), 'manual'),
        body: [
          h('p', { class: 'prob-explain' }, icon('lock'),
            h('span', {}, 'Someone set these images on the server by hand, so Posteryard leaves them alone. Hand one back and Posteryard makes its own again.')),
          h('ul', { class: 'prob-list' }, manual.map(manualRow)),
        ] }));
    }
    mount(root, sections);
  }

  await load();
  // Reload when the shell's status poll shows the counts moved.
  let counts = null;
  const unsubscribe = ctx.onStatus((s) => {
    const next = JSON.stringify([s.images?.failed, s.images?.manual, s.alerts]);
    if (counts !== null && next !== counts) reloadSoon(0);
    counts = next;
  });
  return () => { stopped = true; clearTimeout(refreshTimer); unsubscribe(); };
}

function labelled(el, label) {
  el.setAttribute('aria-label', label);
  return el;
}
