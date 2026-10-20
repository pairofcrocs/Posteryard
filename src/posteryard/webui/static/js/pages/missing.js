// Page: missing. Titles without a logo or a textless poster on TMDB, and the gaps in TMDB collections.
import { api, imageUrl, ApiError } from '../api.js';
import {
  h, mount, icon, button, badge, toast, emptyState, errorState, spinner, timeAgo, formatDate, formatNumber, debounce,
  every, KIND_LABEL,
} from '../ui.js';
import { collectionsPanel, visibleCollections } from './missing-collections.js';

const CHUNK = 100;
const POLL_MS = 2000;
const TABS = ['logos', 'textless', 'collections'];

export default async function render(view, ctx) {
  ctx.setTitle('Missing');
  const root = h('div', { class: 'page-missing' });
  mount(view, root);

  const state = {
    tab: TABS.includes(ctx.query.get('tab')) ? ctx.query.get('tab') : 'logos',
    report: null,
    signature: '',
    filters: { logos: '', textless: '', collections: '' },
    libraries: { logos: '', textless: '' },
    shown: { logos: CHUNK, textless: CHUNK, collections: 30 },
    hideUnreleased: true,
    sort: 'missing',
    unmatchedOpen: false,
  };
  let stopPoll = null;
  let stopped = false;

  const head = h('div', { class: 'page-head miss-head' });
  const progress = h('div', { class: 'miss-progress hidden' });
  const failure = h('div', { class: 'miss-failure hidden' });
  const body = h('div', { class: 'miss-body' });
  mount(root, head, progress, failure, body);

  async function load() {
    mount(body, spinner());
    let report;
    try {
      report = await api.get('/api/missing');
    } catch (err) {
      if (stopped) return;
      mount(head);
      mount(body, errorState(err, load));
      return;
    }
    if (stopped) return;
    update(report, true);
  }

  async function poll() {
    if (stopped) return;
    let report;
    try {
      report = await api.get('/api/missing');
    } catch { return; } // The next tick tries again.
    if (!stopped) update(report, false);
  }

  function update(report, force) {
    state.report = report;
    const scan = report.scan || { state: 'never' };
    paintHead(scan);
    paintProgress(scan);
    paintFailure(scan);
    const signature = JSON.stringify([scan.state, scan.finished, ['logos', 'textless', 'collections', 'unmatched'].map((k) => (report[k] || []).length)]);
    if (force || signature !== state.signature) paintBody();
    state.signature = signature;
    if (scan.state === 'running' && !stopPoll) stopPoll = every(POLL_MS, poll);
    if (scan.state !== 'running' && stopPoll) {
      stopPoll();
      stopPoll = null;
      if (scan.state === 'done') toast('The check is done', 'success');
      ctx.refreshStatus();
    }
  }

  async function startScan() {
    try {
      await api.post('/api/missing/scan');
    } catch (err) {
      // 409: a check is already running, so just follow it.
      if (!(err instanceof ApiError && err.status === 409)) throw err;
    }
    await poll();
  }

  // ---- Head, progress and failure ----

  function paintHead(scan) {
    const running = scan.state === 'running';
    const status = scan.state === 'never' ? 'Never checked'
      : running ? 'Checking now…'
        : scan.state === 'failed' ? `The last check failed ${timeAgo(scan.finished || scan.started)}`
          : `Last checked ${timeAgo(scan.finished)} · ${formatNumber(scan.total ?? 0)} ${scan.total === 1 ? 'title' : 'titles'}`;
    const action = running || scan.state === 'never' ? null : button({
      label: scan.state === 'never' ? 'Check now' : 'Check again', icon: 'refresh', kind: 'primary', onClick: startScan,
    });
    mount(head,
      h('div', { class: 'grow' },
        h('h2', {}, 'Missing art'),
        h('p', {}, 'Logos and textless posters TMDB does not have yet, and the movies missing from your collections.'),
        h('div', { class: 'miss-status small', title: scan.finished ? formatDate(scan.finished) : null },
          icon(running ? 'clock' : scan.state === 'failed' ? 'alert' : scan.state === 'never' ? 'info' : 'check'), status)),
      action);
  }

  function paintProgress(scan) {
    const running = scan.state === 'running';
    progress.classList.toggle('hidden', !running);
    if (!running) return;
    const total = scan.total || 0;
    const checked = Math.min(scan.checked || 0, total || Infinity);
    const pct = total ? Math.round((checked / total) * 100) : 0;
    mount(progress, h('div', { class: 'card miss-progress-card' },
      h('div', { class: 'row' }, h('span', { class: 'spinner' }),
        h('span', { class: 'grow' }, total ? `Checked ${formatNumber(checked)} of ${formatNumber(total)} titles` : 'Getting the list of titles…'),
        total ? h('span', { class: 'small muted tabular' }, `${pct}%`) : null),
      h('div', {
        class: 'progress', role: 'progressbar', 'aria-label': 'Check progress',
        'aria-valuemin': '0', 'aria-valuemax': String(total || 100), 'aria-valuenow': String(checked),
      }, h('span', { style: { width: `${pct}%` } })),
      h('p', { class: 'small muted' }, 'Posters keep being made while this runs. You can leave this page.')));
  }

  function paintFailure(scan) {
    const failed = scan.state === 'failed';
    failure.classList.toggle('hidden', !failed);
    if (failed) {
      mount(failure, h('div', { class: 'banner danger', role: 'alert' }, icon('alert'),
        h('span', { class: 'grow' }, scan.error || 'The check stopped with an unknown error. See Activity for the log.')));
    }
  }

  // ---- Body ----

  function paintBody() {
    const report = state.report;
    const scan = report.scan || { state: 'never' };
    if (scan.state === 'never') {
      mount(body, h('div', { class: 'card miss-intro' }, emptyState({
        icon: 'search', title: 'Find what is missing',
        message: 'The check looks up every movie and show on TMDB and lists the titles without a logo or a textless poster, '
          + 'and the movies missing from your collections. It reads TMDB for each title, so on a big library it can take a few minutes.',
        action: button({ label: 'Check now', icon: 'refresh', kind: 'primary', onClick: startScan }),
      })));
      return;
    }
    const empty = !['logos', 'textless', 'collections', 'unmatched'].some((k) => (report[k] || []).length);
    if (scan.state === 'running' && empty) {
      mount(body, h('div', { class: 'card' }, emptyState({
        icon: 'clock', title: 'Checking your library', message: 'What is missing shows up here when the check is done.',
      })));
      return;
    }

    // Keep focus in a filter box across a repaint.
    const focused = document.activeElement && root.contains(document.activeElement) ? document.activeElement.id : null;
    const selection = focused ? [document.activeElement.selectionStart, document.activeElement.selectionEnd] : null;

    const counts = {
      logos: (report.logos || []).length,
      textless: (report.textless || []).length,
      collections: visibleCollections(report.collections || [], { hideUnreleased: state.hideUnreleased }).length,
    };
    const labels = { logos: 'Logos', textless: ['Textless', h('span', { class: 'miss-wide' }, ' posters')], collections: 'Collections' };
    const tabBar = h('div', { class: 'tabs', role: 'tablist', 'aria-label': 'What is missing' }, TABS.map((id) => h('button', {
      type: 'button', role: 'tab', id: `miss-tab-${id}`, class: id === state.tab ? 'active' : '',
      'aria-selected': String(id === state.tab), 'aria-controls': 'miss-panel', tabindex: id === state.tab ? '0' : '-1',
      onClick: () => selectTab(id),
      onKeydown: (event) => {
        const step = event.key === 'ArrowRight' ? 1 : event.key === 'ArrowLeft' ? -1 : 0;
        if (!step) return;
        event.preventDefault();
        const next = TABS[(TABS.indexOf(state.tab) + step + TABS.length) % TABS.length];
        selectTab(next);
        root.querySelector(`#miss-tab-${next}`)?.focus();
      },
    }, labels[id], h('span', { class: 'miss-count', dataset: { tab: id } }, formatNumber(counts[id])))));

    const panel = h('div', { class: 'miss-panel', id: 'miss-panel', role: 'tabpanel', 'aria-labelledby': `miss-tab-${state.tab}` });
    mount(body, unmatchedNote(report.unmatched || []), tabBar, panel);
    paintPanel(panel);

    if (focused) {
      const again = root.querySelector(`#${CSS.escape(focused)}`);
      if (again) {
        again.focus();
        if (selection && again.setSelectionRange) try { again.setSelectionRange(...selection); } catch { /* not a text box */ }
      }
    }
  }

  function unmatchedNote(titles) {
    if (!titles.length) return null;
    const list = h('ul', { class: 'miss-unmatched-list' });
    const more = h('div');
    const paint = (n) => {
      mount(list, titles.slice(0, n).map((t) => h('li', {},
        h('a', { href: `/item/${encodeURIComponent(t.key)}`, 'data-link': '' }, t.title || `Title ${t.key}`),
        h('span', { class: 'muted' }, [t.year, t.library].filter(Boolean).map((v) => ` · ${v}`).join('')))));
      mount(more, titles.length > n ? button({ label: `Show all ${formatNumber(titles.length)}`, small: true, kind: 'ghost', onClick: () => paint(titles.length) }) : null);
    };
    paint(CHUNK);
    const note = h('details', { class: 'miss-unmatched', open: state.unmatchedOpen },
      h('summary', {}, icon('info'), `${formatNumber(titles.length)} ${titles.length === 1 ? 'title' : 'titles'} could not be checked`),
      h('div', { class: 'miss-unmatched-body' },
        h('p', { class: 'small' }, 'The server has not matched these to TMDB, so the check could not look them up. '
          + 'Match them on the server, then check again.'),
        list, more));
    note.addEventListener('toggle', () => { state.unmatchedOpen = note.open; });
    return note;
  }

  function selectTab(id) {
    if (id === state.tab) return;
    state.tab = id;
    const params = new URLSearchParams(location.search);
    params.set('tab', id);
    ctx.setQuery(params);
    paintBody();
  }

  function paintPanel(panel) {
    const report = state.report;
    const running = report.scan?.state === 'running';
    if (state.tab === 'collections') {
      collectionsPanel(panel, {
        collections: report.collections || [],
        state,
        running,
        onCount: (n) => {
          const el = root.querySelector('.miss-count[data-tab="collections"]');
          if (el) el.textContent = formatNumber(n);
        },
      });
      return;
    }
    const logos = state.tab === 'logos';
    const languages = languageList(report.languages || []);
    titlesPanel(panel, {
      kind: state.tab,
      titles: report[state.tab] || [],
      running,
      intro: logos
        ? `TMDB has no logo for these titles in ${languages}, and neither has fanart.tv, so their posters set the name in text. `
          + 'Add a logo on TMDB and the poster gets it at the next daily pass.'
        : 'TMDB has no textless poster for these titles, or each one has text printed on it. Posteryard builds their posters from a '
          + 'backdrop, Apple TV or fanart.tv art instead. Add a textless poster on TMDB and Posteryard picks it up at the next daily pass.',
      linkLabel: logos ? 'Add a logo on TMDB' : 'Add a poster on TMDB',
      allClear: logos ? 'Every title has a logo' : 'Every title has a textless poster',
      allClearMessage: logos ? 'TMDB has a logo for every title in your libraries.' : 'TMDB has a clean textless poster for every title in your libraries.',
    });
  }

  function titlesPanel(panel, { kind, titles, running, intro, linkLabel, allClear, allClearMessage }) {
    if (!titles.length) {
      mount(panel, h('div', { class: 'card' }, emptyState({
        icon: running ? 'clock' : 'check',
        title: running ? 'Nothing missing so far' : allClear,
        message: running ? 'The check is still running.' : allClearMessage,
      })));
      return;
    }
    const libraries = [...new Set(titles.map((t) => t.library).filter(Boolean))].sort((a, b) => a.localeCompare(b));
    if (state.libraries[kind] && !libraries.includes(state.libraries[kind])) state.libraries[kind] = '';

    const filter = h('input', {
      class: 'input', type: 'search', id: `miss-filter-${kind}`, placeholder: 'Filter titles', 'aria-label': 'Filter titles',
      value: state.filters[kind],
    });
    const picker = libraries.length > 1 ? h('select', { class: 'input', 'aria-label': 'Library', onChange: () => {
      state.libraries[kind] = picker.value;
      state.shown[kind] = CHUNK;
      paintList();
    } }, h('option', { value: '' }, 'All libraries'), libraries.map((lib) => h('option', { value: lib }, lib))) : null;
    if (picker) picker.value = state.libraries[kind];
    const count = h('div', { class: 'small muted miss-shown', 'aria-live': 'polite' });
    const list = h('ul', { class: 'miss-list' });
    const more = h('div', { class: 'miss-more' });

    filter.addEventListener('input', debounce(() => {
      if (filter.value.trim() === state.filters[kind]) return;
      state.filters[kind] = filter.value.trim();
      state.shown[kind] = CHUNK;
      paintList();
    }, 200));

    const matching = () => {
      const needle = state.filters[kind].toLowerCase();
      const library = state.libraries[kind];
      return titles.filter((t) => (!library || t.library === library)
        && (!needle || `${t.title} ${t.year ?? ''} ${t.library ?? ''}`.toLowerCase().includes(needle)));
    };

    function paintList() {
      const rows = matching();
      const shown = Math.min(state.shown[kind], rows.length);
      count.textContent = rows.length === titles.length
        ? `${formatNumber(titles.length)} ${titles.length === 1 ? 'title' : 'titles'}`
        : `${formatNumber(rows.length)} of ${formatNumber(titles.length)} titles`;
      if (!rows.length) {
        mount(list);
        mount(more, emptyState({ icon: 'search', title: 'No titles match', message: 'Try another filter.',
          action: button({ label: 'Clear filter', onClick: () => {
            filter.value = '';
            state.filters[kind] = '';
            if (picker) { picker.value = ''; state.libraries[kind] = ''; }
            paintList();
          } }) }));
        return;
      }
      mount(list, rows.slice(0, shown).map((t) => titleRow(t, linkLabel)));
      paintMore(rows, shown);
    }

    function paintMore(rows, shown) {
      const left = rows.length - shown;
      mount(more, left > 0 ? button({ label: `Show more (${formatNumber(left)} left)`, onClick: () => {
        const from = state.shown[kind];
        state.shown[kind] += CHUNK;
        const next = Math.min(state.shown[kind], rows.length);
        list.append(...rows.slice(from, next).map((t) => titleRow(t, linkLabel)));
        list.children[from]?.querySelector('a.miss-title')?.focus();
        paintMore(rows, next);
      } }) : null);
    }

    mount(panel,
      h('p', { class: 'miss-explain' }, icon('info'), h('span', {}, intro)),
      h('div', { class: 'miss-toolbar' },
        h('div', { class: 'miss-filter' }, icon('search'), filter), picker, count),
      h('div', { class: 'card miss-list-card' }, list),
      more);
    paintList();
  }

  await load();
  return () => {
    stopped = true;
    if (stopPoll) stopPoll();
  };
}

function titleRow(t, linkLabel) {
  const href = `/item/${encodeURIComponent(t.key)}`;
  const name = t.title || `Title ${t.key}`;
  const thumb = h('img', {
    class: 'thumb', src: imageUrl(t.key, { w: 120 }), alt: '', loading: 'lazy', decoding: 'async',
    onError: (event) => event.target.replaceWith(h('div', { class: 'thumb' })),
  });
  return h('li', { class: 'miss-row' },
    h('a', { class: 'miss-thumb', href, 'data-link': '', tabindex: '-1', 'aria-hidden': 'true' }, thumb),
    h('div', { class: 'miss-main' },
      h('a', { class: 'miss-title', href, 'data-link': '' }, name),
      h('div', { class: 'small muted' }, [t.year, KIND_LABEL[t.type] || t.type, t.library].filter(Boolean).join(' · ')),
      t.reason ? h('div', { class: 'miss-reason small' }, t.reason) : null),
    t.tmdb_url ? h('a', {
      class: 'btn small miss-tmdb', href: t.tmdb_url, target: '_blank', rel: 'noopener noreferrer',
      'aria-label': `${linkLabel} for ${name} (opens TMDB)`,
    }, icon('external'), h('span', {}, 'Add on TMDB')) : badge('Not on TMDB'));
}

function languageList(codes) {
  let names = codes;
  try {
    const display = new Intl.DisplayNames(['en'], { type: 'language' });
    names = codes.map((code) => display.of(code) || code);
  } catch { /* Old browser: show the codes. */ }
  if (!names.length) return 'your languages';
  if (names.length === 1) return names[0];
  return `${names.slice(0, -1).join(', ')} or ${names[names.length - 1]}`;
}
