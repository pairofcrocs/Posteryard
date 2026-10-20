// Page: a title. Header with poster and actions, then Overview, Art candidates, Seasons/Episodes and Details tabs.
import { api, imageUrl } from '../api.js';
import {
  h, mount, icon, button, statusBadge, emptyState, errorState, tabs, runJob, confirmDialog, toast, KIND_LABEL, TARGET_LABEL,
} from '../ui.js';
import { markChanged, bustFor } from './item-cache.js';
import { ownImageDialog, previewDialog } from './item-dialogs.js';
import { LABEL_HELP, overrideBadges } from './item-labels.js';
import { overviewTab, candidatesTab, childrenTab, detailsTab } from './item-tabs.js';

const ART_KINDS = new Set(['movie', 'show', 'season']);
// Art-candidate results by key, so a check survives tab switches and visits within the session.
const whyStates = new Map();

export default async function render(view, ctx) {
  const key = ctx.params.key;
  const root = h('div', { class: 'page-item' });
  mount(view, root, h('div', { class: 'loading' }, h('span', { class: 'spinner' }), 'Loading'));
  let status = ctx.status;
  let item = null;
  let bust = null;
  let tab = ctx.query.get('tab') || 'overview';
  const why = whyStates.get(key) || { result: null, stale: false, error: null, running: false };
  whyStates.set(key, why);

  const live = () => !(status ? status.dry_run : false);
  const serverName = () => status?.server?.name || 'the server';
  const unsubscribe = ctx.onStatus((s) => {
    const changed = !status || s.dry_run !== status.dry_run;
    status = s;
    if (changed && item) paintHeader();
  });

  const header = h('div', { class: 'hero-wrap' });
  const tabBar = h('div', { class: 'tab-bar' });
  const panel = h('div', { class: 'panel', role: 'tabpanel' });

  while (!item) {
    try {
      item = await api.get(`/api/items/${encodeURIComponent(key)}`);
    } catch (err) {
      if (!ctx.isCurrent()) return unsubscribe;
      ctx.setTitle(err.status === 404 ? 'Not found' : 'Title');
      if (err.status === 404) {
        mount(view, h('div', { class: 'page-item' }, emptyState({
          icon: 'search', title: 'Title not found',
          message: 'The server has no title with this rating key. It may have been removed or renamed.',
          action: h('a', { class: 'btn primary', href: '/library', 'data-link': '' }, icon('grid'), 'Back to the library'),
        })));
        return unsubscribe;
      }
      await new Promise((resolve) => mount(view, h('div', { class: 'page-item' }, errorState(err, resolve))));
    }
    if (!ctx.isCurrent()) return unsubscribe;
  }
  bust = bustFor(key, item.records);
  mount(view, root);
  mount(root, header, tabBar, panel);

  const titleFor = (it) => {
    if (it.type === 'season' && it.parent) return `${it.parent.title} · ${it.title}`;
    if (it.type === 'episode' && it.grandparent) return `${it.grandparent.title} · ${it.title}`;
    return it.title;
  };

  async function refresh() {
    try {
      item = await api.get(`/api/items/${encodeURIComponent(key)}`);
    } catch (err) {
      toast(err.message, 'error');
      return;
    }
    if (!ctx.isCurrent()) return;
    bust = bustFor(key, item.records);
    paintHeader();
    paintTabs();
  }

  // ---- Actions ----

  /** Run a job that may change the server: confirm first when live, then reload. */
  async function act({ path, body = {}, label, confirm = null, blob = null }) {
    if (confirm && live() && !await confirmDialog({ ...confirm, confirmLabel: confirm.confirmLabel || 'Continue' })) return;
    const result = await runJob(`/api/items/${encodeURIComponent(key)}/${path}`, body, `${label}: ${item.title}`, { blob });
    markChanged(key);
    if (result && why.result) why.stale = true;
    await refresh();
  }

  const actions = {
    process: () => act({
      path: 'process', label: 'Render now',
      confirm: { title: 'Render and upload now?', confirmLabel: 'Render and upload',
        message: `Posteryard renders every image of ${item.title} and uploads it to ${serverName()}, even when nothing changed.` },
    }),
    next: () => act({
      path: 'art/next', label: 'Next art',
      confirm: { title: 'Switch to the next art?', confirmLabel: 'Switch art',
        message: `Posteryard skips the art in use and uploads the next best one to ${serverName()}. Run it again for the one after. Reset to automatic art undoes it.` },
    }),
    reset: () => act({
      path: 'art/reset', label: 'Reset to automatic art',
      confirm: { title: 'Go back to automatic art?', confirmLabel: 'Reset',
        message: `Posteryard forgets ${item.override?.custom ? 'your own image' : 'the art skipped with Next art'} and uploads the automatic art to ${serverName()}.` },
    }),
    forget: () => act({
      path: 'forget', label: 'Hand back to Posteryard',
      confirm: { title: 'Hand back to Posteryard?', confirmLabel: 'Hand back', danger: true,
        message: `This image was changed by hand on ${serverName()}, so Posteryard left it alone. Posteryard now renders it again and replaces it.` },
    }),
    own: async () => {
      const choice = await ownImageDialog({ title: item.title, live: live(), serverName: serverName() });
      if (!choice) return;
      if (choice.blob) await act({ path: 'art/set', label: 'Use my own image', blob: choice.blob });
      else await act({ path: 'art/set', label: 'Use my own image', body: { url: choice.url } });
    },
    preview: async () => {
      const result = await runJob(`/api/items/${encodeURIComponent(key)}/render`, {}, `Preview: ${item.title}`, { quiet: true });
      if (result && ctx.isCurrent()) previewDialog({ title: item.title, renders: result.renders || [] });
    },
    candidate: async (candidate) => {
      if (live() && !await confirmDialog({
        title: 'Use this art?', confirmLabel: 'Use and upload',
        message: `Posteryard uses this picture as the art of ${item.title} and uploads the new poster to ${serverName()}. Reset to automatic art undoes it.`,
      })) return;
      await act({ path: 'art/set', label: 'Use this art', body: { candidate: candidate.path } });
      if (tab === 'art' && ctx.isCurrent()) runWhy();
    },
  };

  // ---- Header ----

  function paintHeader() {
    ctx.setTitle(titleFor(item));
    // Keep keyboard focus on the same control across a repaint, or on the title when it is gone.
    const focused = header.contains(document.activeElement) ? document.activeElement.textContent : null;
    const records = item.records || [];
    const hasManual = records.some((r) => r.status === 'manual');
    const artKind = ART_KINDS.has(item.type);
    const posterTarget = (item.targets || []).includes('poster') || !(item.targets || []).length ? 'poster' : item.targets[0];
    const wideHero = posterTarget !== 'poster';

    const crumbs = [h('a', { href: item.library_key ? `/library?library=${encodeURIComponent(item.library_key)}` : '/library', 'data-link': '' }, item.library || 'Library')];
    if (item.grandparent) crumbs.push(h('a', { href: `/item/${encodeURIComponent(item.grandparent.key)}`, 'data-link': '' }, item.grandparent.title));
    if (item.parent) crumbs.push(h('a', { href: `/item/${encodeURIComponent(item.parent.key)}`, 'data-link': '' }, item.parent.title));
    crumbs.push(h('span', { 'aria-current': 'page' }, item.title));
    const breadcrumb = h('nav', { class: 'breadcrumb', 'aria-label': 'Breadcrumb' },
      crumbs.map((crumb, i) => [i ? icon('chevronRight') : null, crumb]));

    const targetBadges = (item.targets || []).map((target) => {
      const rec = records.find((r) => r.target === target);
      return h('span', { class: 'target-status', title: rec?.last_error || null },
        h('span', { class: 'target-name' }, TARGET_LABEL[target] || target), statusBadge(rec?.status ?? null));
    });
    if (item.item_error) {
      targetBadges.push(h('span', { class: 'target-status', title: item.item_error.last_error || null },
        h('span', { class: 'target-name' }, 'Title'), statusBadge('failed')));
    }

    const facts = [KIND_LABEL[item.type] || item.type,
      item.children_count ? `${item.children_count} ${item.type === 'show' ? (item.children_count === 1 ? 'season' : 'seasons') : (item.children_count === 1 ? 'episode' : 'episodes')}` : null,
    ].filter(Boolean);

    const tmdbKind = item.type === 'movie' ? 'movie' : ['show', 'season', 'episode'].includes(item.type) ? 'tv' : null;
    const links = h('div', { class: 'links row wrap' },
      item.server_url ? h('a', { class: 'btn ghost small', href: item.server_url, target: '_blank', rel: 'noopener noreferrer' },
        icon('external'), `Open in ${serverName()}`) : null,
      item.tmdb_id && tmdbKind ? h('a', { class: 'btn ghost small', href: `https://www.themoviedb.org/${tmdbKind}/${item.tmdb_id}`, target: '_blank', rel: 'noopener noreferrer' },
        icon('external'), 'TMDB') : null);

    const labelNotes = (item.labels || []).filter((label) => label.startsWith('posteryard-') && !(item.ignored && label === 'posteryard-ignore')).map((label) => h('li', {},
      h('span', { class: 'badge accent mono-badge' }, label), h('span', { class: 'small' }, LABEL_HELP[label] || 'A Posteryard label.')));

    const overrides = overrideBadges(item.override).map((b) => h('span', { class: ['badge', b.tone], title: b.title }, b.text));

    const primary = [
      button({ label: 'Render now', icon: 'wand', kind: 'primary', onClick: actions.process }),
      artKind ? button({ label: 'Next art', icon: 'skip', onClick: actions.next }) : null,
      artKind ? button({ label: 'Use my own image', icon: 'upload', onClick: actions.own }) : null,
      item.override ? button({ label: 'Reset to automatic art', icon: 'undo', onClick: actions.reset }) : null,
      hasManual ? button({ label: 'Hand back to Posteryard', icon: 'refresh', onClick: actions.forget }) : null,
      button({ label: 'Preview without changing anything', icon: 'eye', kind: 'ghost', onClick: actions.preview }),
    ];

    const poster = h('div', { class: ['hero-poster', wideHero && 'wide'] },
      h('img', {
        src: imageUrl(key, { target: posterTarget, w: wideHero ? 960 : 600, bust }), alt: `${TARGET_LABEL[posterTarget]} of ${item.title}`,
        onError: (event) => event.target.replaceWith(h('div', { class: 'placeholder-art' }, item.title)),
      }));

    const backdropTarget = (item.targets || []).includes('art') ? 'art' : posterTarget;
    const backdrop = h('div', { class: 'hero-backdrop', 'aria-hidden': 'true' },
      h('img', { src: imageUrl(key, { target: backdropTarget, w: 960, bust }), alt: '', onError: (event) => event.target.remove() }));

    mount(header, h('section', { class: 'hero' }, backdrop,
      h('div', { class: 'hero-inner' }, poster,
        h('div', { class: 'hero-info' },
          breadcrumb,
          h('h2', { class: 'hero-title' }, item.title, item.year && item.type !== 'season' ? h('span', { class: 'year' }, ` ${item.year}`) : null),
          h('div', { class: 'facts small' }, facts.join(' · ')),
          targetBadges.length ? h('div', { class: 'row wrap targets' }, targetBadges) : null,
          overrides.length ? h('div', { class: 'row wrap' }, overrides) : null,
          item.ignored ? h('div', { class: 'banner warn' }, icon('lock'), h('span', { class: 'grow' }, 'This title has the posteryard-ignore label, so Posteryard leaves it alone.')) : null,
          labelNotes.length ? h('ul', { class: 'label-notes' }, labelNotes) : null,
          item.item_error?.last_error ? h('p', { class: 'small error-text' }, icon('alert'), item.item_error.last_error) : null,
          h('div', { class: 'actions row wrap' }, primary),
          h('p', { class: 'small muted mode-note' }, live()
            ? `Live: actions change ${serverName()} right away. You are asked first.`
            : 'DRY_RUN is on: actions save a preview and change nothing on the server.'),
          links))));
    if (focused !== null) {
      const same = [...header.querySelectorAll('button, a')].find((el) => el.textContent === focused);
      const title = header.querySelector('.hero-title');
      title.tabIndex = -1;
      (same || title).focus({ preventScroll: true });
    }
  }

  // ---- Tabs ----

  const tabItems = () => {
    const list = [{ id: 'overview', label: 'Overview' }];
    if (ART_KINDS.has(item.type)) list.push({ id: 'art', label: 'Art candidates' });
    if (item.children_count || item.type === 'show' || item.type === 'season') {
      list.push({ id: 'children', label: item.type === 'show' ? 'Seasons' : 'Episodes' });
    }
    list.push({ id: 'details', label: 'Details' });
    return list;
  };

  function paintTabs() {
    const list = tabItems();
    if (!list.some((t) => t.id === tab)) tab = 'overview';
    const bar = tabs(list, tab, (id) => {
      tab = id;
      const params = new URLSearchParams(location.search);
      if (id === 'overview') params.delete('tab');
      else params.set('tab', id);
      ctx.setQuery(params);
      paintPanel();
    });
    bar.setAttribute('aria-label', 'Sections');
    // Arrow keys move between tabs.
    bar.addEventListener('keydown', (event) => {
      if (event.key !== 'ArrowRight' && event.key !== 'ArrowLeft') return;
      const buttons = [...bar.querySelectorAll('button')];
      const i = buttons.indexOf(document.activeElement);
      if (i < 0) return;
      const next = buttons[(i + (event.key === 'ArrowRight' ? 1 : buttons.length - 1)) % buttons.length];
      next.focus();
      next.click();
    });
    mount(tabBar, bar);
    paintPanel();
  }

  async function runWhy() {
    if (why.running) return;
    why.running = true;
    why.error = null;
    if (tab === 'art') paintPanel();
    try {
      why.result = await api.job(`/api/items/${encodeURIComponent(key)}/why`, {});
      why.stale = false;
    } catch (err) {
      why.error = err.message;
    } finally {
      why.running = false;
    }
    if (ctx.isCurrent() && tab === 'art') paintPanel();
  }

  function paintPanel() {
    panel.dataset.tab = tab;
    if (tab === 'overview') mount(panel, overviewTab(item, { bust, serverName: serverName() }));
    else if (tab === 'art') mount(panel, candidatesTab(item, why, { run: runWhy, use: actions.candidate }));
    else if (tab === 'children') childrenTab(item, panel, { isCurrent: () => ctx.isCurrent() && tab === 'children', bust });
    else mount(panel, detailsTab(item));
  }

  paintHeader();
  paintTabs();
  return unsubscribe;
}
