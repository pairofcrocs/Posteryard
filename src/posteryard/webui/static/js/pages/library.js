// Page: Library. A poster grid (or list) of the managed libraries with filters kept in the query string.
import { api, imageUrl } from '../api.js';
import {
  h, mount, icon, button, statusBadge, toast, emptyState, errorState, segmented, posterTile, timeAgo, debounce,
  confirmDialog, formatNumber, KIND_LABEL,
} from '../ui.js';
import { bustFor, markChanged } from './item-cache.js';

const PAGE = 60;
const STATUS_FILTERS = [
  ['', 'All'], ['uploaded', 'Uploaded'], ['preview', 'Preview'], ['manual', 'By hand'], ['failed', 'Failed'],
  ['none', 'Not yet'], ['custom', 'Custom art'],
];
const SORTS = [['title', 'Title'], ['added', 'Recently added'], ['updated', 'Recently updated']];
const KIND_PLURAL = { movie: 'Movies', show: 'Shows', collection: 'Collections' };

export default async function render(view, ctx) {
  const root = h('div', { class: 'page-library' });
  mount(view, root);

  const q = ctx.query;
  const state = {
    library: q.get('library') || '',
    kind: q.get('kind') || '',
    status: q.get('status') || '',
    q: q.get('q') || '',
    sort: q.get('sort') || 'title',
    view: q.get('view') === 'list' ? 'list' : 'grid',
    source: q.get('source') === 'server' ? 'server' : 'auto',
    items: [],
    total: 0,
    loading: false,
    generation: 0,
    error: null,
    pageError: false,
    selecting: false,
    selected: new Set(),
  };
  let status = ctx.status;
  const unsubscribe = ctx.onStatus((s) => { status = s; });

  let libraries = null;
  while (libraries === null) {
    try {
      libraries = (await api.get('/api/libraries')).libraries || [];
    } catch (err) {
      if (!ctx.isCurrent()) return unsubscribe;
      await new Promise((resolve) => mount(root, errorState(err, resolve)));
    }
    if (!ctx.isCurrent()) return unsubscribe;
  }

  const managed = libraries.filter((lib) => lib.managed);
  const others = libraries.filter((lib) => !lib.managed);
  if (!managed.length && !state.library) {
    mount(root, emptyState({
      icon: 'grid', title: 'No managed libraries',
      message: 'Posteryard makes art for the libraries named in LIBRARIES. Choose them in Settings.',
      action: h('a', { class: 'btn primary', href: '/settings', 'data-link': '' }, icon('gear'), 'Open Settings'),
    }));
    return unsubscribe;
  }
  if (state.library && !libraries.some((lib) => lib.key === state.library)) state.library = '';

  const syncQuery = () => {
    const params = new URLSearchParams();
    if (state.library) params.set('library', state.library);
    if (state.kind) params.set('kind', state.kind);
    if (state.status) params.set('status', state.status);
    if (state.q) params.set('q', state.q);
    if (state.sort !== 'title') params.set('sort', state.sort);
    if (state.view !== 'grid') params.set('view', state.view);
    if (state.source !== 'auto') params.set('source', state.source);
    ctx.setQuery(params);
  };

  // ---- Toolbar ----

  const picker = h('select', { class: 'input', 'aria-label': 'Library', onChange: () => {
    state.library = picker.value;
    if (!kindOptions().some(([value]) => value === state.kind)) state.kind = '';
    paintKind();
    reload();
  } },
  h('option', { value: '' }, managed.length > 1 ? 'All libraries' : managed.length ? managed[0].title : 'All libraries'),
  managed.length > 1 ? managed.map((lib) => h('option', { value: lib.key }, `${lib.title}`)) : null,
  others.length ? h('optgroup', { label: 'Not managed' }, others.map((lib) => h('option', { value: lib.key }, lib.title))) : null);
  picker.value = state.library;

  const kindSlot = h('div', { class: 'kind-slot' });
  const kindOptions = () => {
    const pool = state.library ? libraries.filter((lib) => lib.key === state.library) : managed;
    const kinds = new Set(pool.map((lib) => lib.type));
    const options = [['', 'All']];
    if (kinds.has('movie')) options.push(['movie', 'Movies']);
    if (kinds.has('show')) options.push(['show', 'Shows']);
    if (status?.features?.collection_posters || state.kind === 'collection') options.push(['collection', 'Collections']);
    return options;
  };
  const paintKind = () => {
    const options = kindOptions();
    mount(kindSlot, options.length > 2 ? segmented(options, state.kind, (value) => { state.kind = value; reload(); }) : null);
    kindSlot.querySelector('.segmented')?.setAttribute('aria-label', 'Kind');
  };

  const search = h('input', {
    class: 'input', type: 'search', placeholder: 'Filter titles', 'aria-label': 'Filter titles in this view', value: state.q,
  });
  search.addEventListener('input', debounce(() => {
    if (search.value.trim() === state.q) return;
    state.q = search.value.trim();
    reload();
  }, 300));

  const sort = h('select', { class: 'input', 'aria-label': 'Sort by', onChange: () => { state.sort = sort.value; reload(); } },
    SORTS.map(([value, label]) => h('option', { value }, label)));
  sort.value = state.sort;

  const sourceToggle = segmented([['auto', 'Posteryard'], ['server', 'Original']], state.source, (value) => {
    state.source = value;
    syncQuery();
    repaintImages();
  });
  sourceToggle.setAttribute('aria-label', 'Which poster to show');
  sourceToggle.title = 'Posteryard: the poster Posteryard made. Original: the poster the server has now.';

  const viewToggle = h('div', { class: 'segmented view-toggle', role: 'radiogroup', 'aria-label': 'Layout' },
    [['grid', 'grid', 'Poster grid'], ['list', 'menu', 'List']].map(([value, iconName, label]) => h('button', {
      type: 'button', role: 'radio', 'aria-label': label, title: label, class: state.view === value ? 'active' : '',
      'aria-checked': String(state.view === value),
      onClick: (event) => {
        viewToggle.querySelectorAll('button').forEach((b) => {
          b.classList.toggle('active', b === event.currentTarget);
          b.setAttribute('aria-checked', String(b === event.currentTarget));
        });
        state.view = value;
        syncQuery();
        paintResults();
      },
    }, icon(iconName))));

  const selectButton = button({ label: 'Select', icon: 'check', kind: 'ghost', small: true, onClick: () => setSelecting(!state.selecting) });

  const chips = h('div', { class: 'chips', role: 'group', 'aria-label': 'Status' },
    STATUS_FILTERS.map(([value, label]) => h('button', {
      type: 'button', class: ['chip', state.status === value && 'active'], 'aria-pressed': String(state.status === value),
      dataset: { value },
      onClick: () => {
        state.status = value;
        chips.querySelectorAll('.chip').forEach((chip) => {
          const on = chip.dataset.value === value;
          chip.classList.toggle('active', on);
          chip.setAttribute('aria-pressed', String(on));
        });
        reload();
      },
    }, value && h('span', { class: ['dot', `dot-${value}`] }), label)));

  const count = h('div', { class: 'count small muted', 'aria-live': 'polite' });
  const retryFailed = button({ label: 'Retry failed', icon: 'refresh', small: true, onClick: () => retry(null) });
  retryFailed.classList.add('hidden');
  const notManaged = h('div', { class: 'banner info hidden' }, icon('info'),
    h('span', { class: 'grow' }, 'Posteryard does not manage this library. Add it to LIBRARIES in Settings to give it posters.'));

  const bulkProgress = h('div', { class: 'progress hidden', role: 'progressbar', 'aria-label': 'Render progress' }, h('span', { style: { width: '0%' } }));
  const bulkCount = h('span', { class: 'grow small' });
  const bulkRender = button({ label: 'Render now', icon: 'wand', kind: 'primary', small: true, onClick: () => renderSelected() });
  const bulkRetry = button({ label: 'Retry failed', icon: 'refresh', small: true, onClick: () => retry([...state.selected]) });
  const bulkAll = button({ label: 'Select all', kind: 'ghost', small: true, onClick: () => {
    const all = state.items.every((item) => state.selected.has(item.key));
    if (all) state.selected.clear();
    else state.items.forEach((item) => state.selected.add(item.key));
    paintSelection();
  } });
  const bulkBar = h('div', { class: 'bulk-bar hidden', role: 'region', 'aria-label': 'Selected titles' },
    h('div', { class: 'row wrap' }, bulkCount, bulkAll, bulkRetry, bulkRender,
      button({ label: 'Done', kind: 'ghost', small: true, onClick: () => setSelecting(false) })),
    bulkProgress);

  const results = h('div', { class: 'results' });
  const sentinel = h('div', { class: 'sentinel', 'aria-hidden': 'true' });
  const more = h('div', { class: 'more' });

  mount(root,
    h('div', { class: 'toolbar' }, h('div', { class: 'picker' }, picker), kindSlot,
      h('div', { class: 'filter' }, icon('search'), search), h('div', { class: 'sort' }, sort)),
    h('div', { class: 'statusbar' }, chips,
      h('div', { class: 'row wrap statusbar-end' }, count, retryFailed, selectButton, sourceToggle, viewToggle)),
    notManaged, bulkBar, results, sentinel, more);
  paintKind();

  // ---- Loading ----

  async function load({ append = false } = {}) {
    const generation = append ? state.generation : ++state.generation;
    const params = new URLSearchParams({ sort: state.sort, offset: String(append ? state.items.length : 0), limit: String(PAGE) });
    if (state.library) params.set('library', state.library);
    if (state.kind) params.set('kind', state.kind);
    if (state.status) params.set('status', state.status);
    if (state.q) params.set('q', state.q);
    state.loading = true;
    state.error = null;
    paintCount();
    if (!append) {
      state.items = [];
      paintResults();
    } else {
      paintMore();
    }
    try {
      const data = await fetchPage(params);
      if (generation !== state.generation || !ctx.isCurrent()) return;
      state.total = data.total ?? 0;
      const seen = new Set(state.items.map((item) => item.key));
      const fresh = (data.items || []).filter((item) => !seen.has(item.key));
      state.items = append ? state.items.concat(fresh) : fresh;
      if (append && !fresh.length) state.total = state.items.length; // The list moved under us; stop paging.
      state.loading = false;
      state.pageError = false;
      if (append && state.view === 'grid') appendTiles(fresh);
      else if (append) appendRows(fresh);
      else paintResults();
    } catch (err) {
      if (generation !== state.generation || !ctx.isCurrent()) return;
      state.loading = false;
      if (append) {
        state.pageError = true; // Wait for "Load more" instead of retrying on every scroll.
        toast(err.message, 'error');
      } else {
        state.error = err;
        paintResults();
      }
    }
    paintMore();
    paintCount();
    // The observer only fires on changes, so check whether the end of the list is still in view.
    if (generation === state.generation) setTimeout(maybeMore, 0);
  }

  function maybeMore() {
    if (!ctx.isCurrent() || state.loading || state.error || state.pageError || !hasMore()) return;
    if (sentinel.getBoundingClientRect().top < window.innerHeight + 600) load({ append: true });
  }

  // "Custom art" is an override, not a record status, so it is its own filter.
  async function fetchPage(params) {
    if (params.get('status') === 'custom') {
      params.delete('status');
      params.set('override', 'custom');
    }
    return api.get(`/api/items?${params}`);
  }

  function reload() {
    state.selected.clear();
    syncQuery();
    paintCount();
    load();
  }

  const hasMore = () => state.items.length < state.total;

  function paintCount() {
    const library = libraries.find((lib) => lib.key === state.library);
    notManaged.classList.toggle('hidden', !library || library.managed);
    retryFailed.classList.toggle('hidden', state.status !== 'failed' || !state.total || state.loading || state.selecting);
    if (state.loading && !state.items.length) {
      count.textContent = 'Loading…';
      return;
    }
    const noun = state.kind ? (KIND_PLURAL[state.kind] || 'titles').toLowerCase() : 'titles';
    count.textContent = `${formatNumber(state.total)} ${state.total === 1 ? noun.replace(/s$/, '') : noun}`;
  }

  function paintMore() {
    const showButton = hasMore() && !state.loading;
    mount(more, state.loading && state.items.length ? h('div', { class: 'loading' }, h('span', { class: 'spinner' }), 'Loading more')
      : showButton ? button({ label: `Load more (${formatNumber(state.total - state.items.length)} left)`, onClick: () => load({ append: true }) })
        : null);
  }

  // ---- Results ----

  const tileSource = () => state.source;

  function tileFor(item) {
    const tile = posterTile({
      href: `/item/${encodeURIComponent(item.key)}`,
      src: imageUrl(item.key, { w: 342, source: tileSource(), bust: bustFor(item.key) }),
      title: item.title,
      subtitle: [item.year, KIND_LABEL[item.type] || item.type].filter(Boolean).join(' · '),
      status: item.status,
      corner: item.override === 'custom' ? h('span', { class: 'badge accent', title: 'Custom art' }, icon('image'), 'Custom')
        : item.override === 'skip' ? h('span', { class: 'badge', title: 'Next art was used' }, icon('skip')) : null,
    });
    tile.dataset.key = item.key;
    tile.setAttribute('aria-label', `${item.title}${item.year ? ` (${item.year})` : ''}, ${statusText(item.status)}`);
    tile.querySelector('.art').append(h('span', { class: 'select-mark', 'aria-hidden': 'true' }, icon('check')));
    return tile;
  }

  function rowFor(item) {
    const href = `/item/${encodeURIComponent(item.key)}`;
    const row = h('tr', { class: 'clickable', dataset: { key: item.key } },
      h('td', { class: 'cell-thumb' }, h('img', {
        class: 'thumb', src: imageUrl(item.key, { w: 80, source: tileSource(), bust: bustFor(item.key) }), alt: '', loading: 'lazy',
        onError: (event) => event.target.replaceWith(h('div', { class: 'thumb' })),
      })),
      h('td', { class: 'cell-title' }, h('a', { href, 'data-link': '', class: 'title-link' }, item.title),
        h('div', { class: 'small muted only-narrow' }, [item.year, KIND_LABEL[item.type]].filter(Boolean).join(' · '))),
      h('td', { class: 'hide-narrow' }, item.year || '—'),
      h('td', { class: 'hide-narrow' }, KIND_LABEL[item.type] || item.type),
      h('td', {}, h('div', { class: 'row gap-s' }, statusBadge(item.status),
        item.override === 'custom' ? h('span', { class: 'badge accent' }, 'Custom') : null)),
      h('td', { class: 'hide-narrow muted small nowrap' }, item.updated_at ? timeAgo(item.updated_at) : '—'));
    return row;
  }

  function appendTiles(items) {
    const grid = results.querySelector('.posters');
    if (!grid) return paintResults();
    grid.append(...items.map(tileFor));
    paintSelection();
  }

  function appendRows(items) {
    const body = results.querySelector('tbody');
    if (!body) return paintResults();
    body.append(...items.map(rowFor));
    paintSelection();
  }

  function paintResults() {
    if (state.error) {
      mount(results, errorState(state.error, () => load()));
      return;
    }
    if (state.loading && !state.items.length) {
      mount(results, state.view === 'grid'
        ? h('div', { class: 'posters', 'aria-busy': 'true', 'aria-label': 'Loading' }, Array.from({ length: 18 }, () => h('div', { class: 'tile skeleton-tile' },
          h('div', { class: 'art skeleton' }), h('div', { class: 'meta' }, h('div', { class: 'skeleton line' }), h('div', { class: 'skeleton line short' })))))
        : h('div', { class: 'card' }, h('div', { class: 'loading' }, h('span', { class: 'spinner' }), 'Loading')));
      return;
    }
    if (!state.items.length) {
      const filtered = state.q || state.status || state.kind;
      mount(results, emptyState({
        icon: filtered ? 'search' : 'grid',
        title: filtered ? 'No titles match' : 'This library is empty',
        message: filtered ? 'Try another filter, or clear them to see every title.' : 'There is nothing here yet.',
        action: filtered ? button({ label: 'Clear filters', onClick: () => {
          state.q = ''; state.status = ''; state.kind = '';
          search.value = '';
          chips.querySelectorAll('.chip').forEach((chip) => {
            chip.classList.toggle('active', chip.dataset.value === '');
            chip.setAttribute('aria-pressed', String(chip.dataset.value === ''));
          });
          paintKind();
          reload();
        } }) : null,
      }));
      return;
    }
    if (state.view === 'grid') {
      mount(results, h('div', { class: 'posters library-grid' }, state.items.map(tileFor)));
    } else {
      mount(results, h('div', { class: 'card list-card' }, h('table', { class: 'table' },
        h('thead', {}, h('tr', {}, h('th', { class: 'cell-thumb' }, h('span', { class: 'sr-only' }, 'Poster')), h('th', {}, 'Title'),
          h('th', { class: 'hide-narrow' }, 'Year'), h('th', { class: 'hide-narrow' }, 'Type'), h('th', {}, 'Status'),
          h('th', { class: 'hide-narrow' }, 'Updated'))),
        h('tbody', {}, state.items.map(rowFor)))));
    }
    paintSelection();
  }

  function repaintImages() {
    for (const img of results.querySelectorAll('[data-key] img')) {
      const key = img.closest('[data-key]').dataset.key;
      const w = img.classList.contains('thumb') ? 80 : 342;
      img.src = imageUrl(key, { w, source: tileSource(), bust: bustFor(key) });
    }
    // Tiles whose image failed show a placeholder; rebuild those so they try the other source.
    if (state.view === 'grid') {
      for (const tile of results.querySelectorAll('.tile[data-key]')) {
        if (!tile.querySelector('img')) {
          const item = state.items.find((i) => i.key === tile.dataset.key);
          if (item) tile.replaceWith(tileFor(item));
        }
      }
      paintSelection();
    }
  }

  // Clicks in select mode toggle the title instead of opening it.
  results.addEventListener('click', (event) => {
    const holder = event.target.closest('[data-key]');
    if (!holder) return;
    if (state.selecting) {
      event.preventDefault();
      const key = holder.dataset.key;
      if (state.selected.has(key)) state.selected.delete(key);
      else state.selected.add(key);
      paintSelection();
      return;
    }
    if (holder.tagName === 'TR' && !event.target.closest('a')) {
      ctx.navigate(`/item/${encodeURIComponent(holder.dataset.key)}`);
    }
  });

  function setSelecting(on) {
    state.selecting = on;
    if (!on) state.selected.clear();
    root.classList.toggle('selecting', on);
    selectButton.classList.toggle('hidden', on);
    bulkBar.classList.toggle('hidden', !on);
    paintSelection();
    paintCount();
  }

  function paintSelection() {
    for (const el of results.querySelectorAll('[data-key]')) {
      const on = state.selected.has(el.dataset.key);
      el.classList.toggle('selected', on);
      if (state.selecting) {
        el.setAttribute('role', 'checkbox');
        el.setAttribute('aria-checked', String(on));
      } else {
        el.removeAttribute('role');
        el.removeAttribute('aria-checked');
      }
    }
    const n = state.selected.size;
    bulkCount.textContent = n ? `${n} selected` : 'Choose titles to render or retry.';
    bulkRender.disabled = !n;
    const failed = state.items.filter((item) => state.selected.has(item.key) && item.status === 'failed').length;
    bulkRetry.disabled = !failed;
    bulkRetry.lastChild.textContent = failed ? `Retry ${failed} failed` : 'Retry failed';
    bulkAll.lastChild.textContent = state.items.length && state.items.every((item) => state.selected.has(item.key)) ? 'Select none' : 'Select all';
  }

  // ---- Bulk actions ----

  async function renderSelected() {
    const keys = state.items.filter((item) => state.selected.has(item.key)).map((item) => item.key);
    if (!keys.length) return;
    const dry = status ? status.dry_run : false;
    const noun = keys.length === 1 ? 'title' : 'titles';
    if (!dry && !await confirmDialog({
      title: `Render ${keys.length} ${noun} now?`,
      message: `Posteryard renders the art of ${keys.length} ${noun} and uploads it to ${status?.server?.name || 'the server'}, even when nothing changed.`,
      confirmLabel: 'Render and upload',
    })) return;
    const bar = bulkProgress.firstChild;
    bulkProgress.classList.remove('hidden');
    const controls = [...bulkBar.querySelectorAll('button')];
    controls.forEach((b) => { b.disabled = true; });
    let done = 0;
    let failed = 0;
    for (const key of keys) {
      if (!ctx.isCurrent()) return;
      const title = state.items.find((item) => item.key === key)?.title || key;
      bulkCount.textContent = `Rendering ${done + 1} of ${keys.length}: ${title}`;
      try {
        await api.job(`/api/items/${encodeURIComponent(key)}/process`);
        markChanged(key);
      } catch (err) {
        failed += 1;
        toast(`${title}: ${err.message}`, 'error');
      }
      done += 1;
      bar.style.width = `${Math.round((done / keys.length) * 100)}%`;
    }
    bulkProgress.classList.add('hidden');
    bar.style.width = '0%';
    controls.forEach((b) => { b.disabled = false; });
    toast(failed ? `Rendered ${done - failed} of ${done} ${noun}. ${failed} failed.` : `Rendered ${done} ${noun}.`, failed ? 'error' : 'success');
    setSelecting(false);
    load();
  }

  async function retry(keys) {
    const data = await api.post('/api/problems/retry', keys ? { keys } : {});
    toast(`${formatNumber(data.queued)} ${data.queued === 1 ? 'title' : 'titles'} queued to try again.`, 'success');
    if (keys) setSelecting(false);
  }

  // ---- Infinite scroll ----

  const observer = new IntersectionObserver((entries) => {
    if (entries.some((entry) => entry.isIntersecting)) maybeMore();
  }, { rootMargin: '600px 0px' });
  observer.observe(sentinel);
  // A scroll check as well, for browsers that throttle the observer.
  let scrollPending = false;
  const onScroll = () => {
    if (scrollPending) return;
    scrollPending = true;
    setTimeout(() => { scrollPending = false; maybeMore(); }, 150);
  };
  window.addEventListener('scroll', onScroll, { passive: true });

  ctx.setTitle('Library');
  syncQuery();
  await load();

  return () => {
    observer.disconnect();
    window.removeEventListener('scroll', onScroll);
    unsubscribe();
    state.generation += 1;
  };
}

function statusText(status) {
  return { uploaded: 'uploaded', uploading: 'uploading', preview: 'preview', manual: 'changed by hand', failed: 'failed' }[status] || 'not rendered yet';
}
