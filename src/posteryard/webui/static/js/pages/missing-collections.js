// The Collections tab of the Missing page: TMDB collections with movies not in the library.
import { imageUrl } from '../api.js';
import { h, mount, icon, button, emptyState, lazyImage, copyText, toast, formatNumber, debounce } from '../ui.js';

const CHUNK = 30;
const SORTS = [['missing', 'Most missing'], ['name', 'Name']];

/** Collections that still miss a movie, with the missing list narrowed when unreleased movies are hidden. */
export function visibleCollections(collections, { hideUnreleased }) {
  const out = [];
  for (const c of collections) {
    const missing = (c.missing || []).filter((m) => !hideUnreleased || m.released);
    if (missing.length) out.push({ ...c, owned: c.owned || [], missing });
  }
  return out;
}

export function collectionsPanel(panel, { collections, state, running, onCount }) {
  if (!collections.length) {
    mount(panel, h('div', { class: 'card' }, emptyState({
      icon: running ? 'clock' : 'check',
      title: running ? 'Nothing missing so far' : 'Your collections are complete',
      message: running ? 'The check is still running.' : 'You have every movie of each TMDB collection you own a part of.',
    })));
    return;
  }

  const search = h('input', {
    class: 'input', type: 'search', id: 'miss-filter-collections', placeholder: 'Filter collections or movies',
    'aria-label': 'Filter collections or movies', value: state.filters.collections,
  });
  search.addEventListener('input', debounce(() => {
    if (search.value.trim() === state.filters.collections) return;
    state.filters.collections = search.value.trim();
    state.shown.collections = CHUNK;
    paintList();
  }, 200));

  const sort = h('select', { class: 'input', 'aria-label': 'Sort by', onChange: () => {
    state.sort = sort.value;
    paintList();
  } }, SORTS.map(([value, label]) => h('option', { value }, label)));
  sort.value = state.sort;

  const toggle = h('input', { type: 'checkbox', role: 'switch', checked: state.hideUnreleased, onChange: () => {
    state.hideUnreleased = toggle.checked;
    state.shown.collections = CHUNK;
    onCount(visibleCollections(collections, state).length);
    paintList();
  } });
  const unreleased = h('label', { class: 'miss-switch' }, h('span', { class: 'switch' }, toggle, h('span')), 'Hide unreleased');

  const copyButton = button({ label: 'Copy list', icon: 'copy', small: true, onClick: () => exportList('copy') });
  const csvButton = button({ label: 'Download CSV', icon: 'download', small: true, onClick: () => exportList('csv') });
  copyButton.title = 'Copy the missing movies that are out, one "Title (Year)" per line';
  csvButton.title = 'Download the missing movies that are out as a CSV file';

  const count = h('div', { class: 'small muted miss-shown', 'aria-live': 'polite' });
  const list = h('div', { class: 'miss-collections' });
  const more = h('div', { class: 'miss-more' });

  function matching() {
    const needle = state.filters.collections.toLowerCase();
    const rows = visibleCollections(collections, state).filter((c) => !needle
      || c.name.toLowerCase().includes(needle)
      || c.owned.some((m) => (m.title || '').toLowerCase().includes(needle))
      || c.missing.some((m) => (m.title || '').toLowerCase().includes(needle)));
    const byName = (a, b) => a.name.localeCompare(b.name);
    rows.sort(state.sort === 'name' ? byName : (a, b) => b.missing.length - a.missing.length || byName(a, b));
    return rows;
  }

  function exportList(how) {
    const seen = new Set();
    const movies = [];
    for (const c of matching()) {
      for (const m of c.missing) {
        if (!m.released || seen.has(m.tmdb_id)) continue;
        seen.add(m.tmdb_id);
        movies.push({ ...m, collection: c.name });
      }
    }
    if (!movies.length) {
      toast('No released movie is missing here', 'info');
      return;
    }
    if (how === 'copy') {
      copyText(movies.map((m) => (m.year ? `${m.title} (${m.year})` : m.title)).join('\n'));
      return;
    }
    const rows = [['title', 'year', 'tmdb_id', 'collection'], ...movies.map((m) => [m.title, m.year ?? '', m.tmdb_id, m.collection])];
    const csv = rows.map((row) => row.map(csvField).join(',')).join('\r\n');
    const url = URL.createObjectURL(new Blob(['﻿', csv, '\r\n'], { type: 'text/csv;charset=utf-8' }));
    const link = h('a', { href: url, download: 'missing-movies.csv', class: 'hidden' });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function paintList() {
    const rows = matching();
    const all = visibleCollections(collections, state).length;
    const missingCount = rows.reduce((n, c) => n + c.missing.length, 0);
    count.textContent = `${rows.length === all ? formatNumber(all) : `${formatNumber(rows.length)} of ${formatNumber(all)}`} `
      + `${all === 1 ? 'collection' : 'collections'} · ${formatNumber(missingCount)} ${missingCount === 1 ? 'movie' : 'movies'} missing`;
    copyButton.disabled = !rows.length;
    csvButton.disabled = !rows.length;
    if (!rows.length) {
      mount(list);
      const onlyUnreleased = !all && state.hideUnreleased;
      mount(more, onlyUnreleased
        ? emptyState({ icon: 'clock', title: 'Only unreleased movies are missing',
          message: 'Every movie that is out is in your library. Turn off "Hide unreleased" to see what is coming.' })
        : emptyState({ icon: 'search', title: 'No collections match', message: 'Try another filter.',
          action: button({ label: 'Clear filter', onClick: () => {
            search.value = '';
            state.filters.collections = '';
            paintList();
          } }) }));
      return;
    }
    const shown = Math.min(state.shown.collections, rows.length);
    mount(list, rows.slice(0, shown).map(collectionCard));
    paintMore(rows, shown);
  }

  function paintMore(rows, shown) {
    const left = rows.length - shown;
    mount(more, left > 0 ? button({ label: `Show more (${formatNumber(left)} left)`, onClick: () => {
      const from = state.shown.collections;
      state.shown.collections += CHUNK;
      const next = Math.min(state.shown.collections, rows.length);
      list.append(...rows.slice(from, next).map(collectionCard));
      list.children[from]?.querySelector('.miss-coll-name')?.focus();
      paintMore(rows, next);
    } }) : null);
  }

  mount(panel,
    h('p', { class: 'miss-explain' }, icon('info'), h('span', {},
      'TMDB collections you own at least one movie of, with the movies you do not have yet. Missing movies link to their TMDB page.')),
    h('div', { class: 'miss-toolbar' },
      h('div', { class: 'miss-filter' }, icon('search'), search),
      sort, unreleased),
    h('div', { class: 'miss-summary' }, count, h('div', { class: 'miss-export' }, copyButton, csvButton)),
    list, more);
  paintList();
}

function collectionCard(c) {
  const total = c.owned.length + c.missing.length;
  const pct = total ? Math.round((c.owned.length / total) * 100) : 0;
  const movies = [
    ...c.owned.map((m) => ({ ...m, owned: true, sortKey: m.year ? String(m.year) : '9999' })),
    ...c.missing.map((m) => ({ ...m, owned: false, sortKey: m.release_date || (m.year ? String(m.year) : '9999') })),
  ].sort((a, b) => a.sortKey.localeCompare(b.sortKey) || (a.title || '').localeCompare(b.title || ''));
  const name = c.tmdb_url
    ? h('a', { class: 'miss-coll-name', href: c.tmdb_url, target: '_blank', rel: 'noopener noreferrer', 'aria-label': `${c.name} (opens TMDB)` },
      c.name, icon('external'))
    : h('span', { class: 'miss-coll-name' }, c.name);
  return h('section', { class: 'card miss-coll', 'aria-label': c.name },
    h('div', { class: 'miss-coll-head' },
      h('div', { class: 'miss-coll-poster', 'aria-hidden': 'true' }, lazyImage(c.poster_url, { alt: '' })),
      h('div', { class: 'miss-coll-info' },
        h('h3', {}, name),
        h('div', { class: 'small muted' }, `${formatNumber(c.owned.length)} of ${formatNumber(total)} in your library`),
        h('div', {
          class: 'progress miss-coll-bar', role: 'progressbar', 'aria-label': `${c.name}: in your library`,
          'aria-valuemin': '0', 'aria-valuemax': String(total), 'aria-valuenow': String(c.owned.length),
        }, h('span', { style: { width: `${pct}%` } })))),
    h('ul', { class: 'miss-movies' }, movies.map(movieTile)));
}

function movieTile(m) {
  const label = m.year ? `${m.title} (${m.year})` : m.title;
  if (m.owned) {
    return h('li', {}, h('a', {
      class: 'tile miss-movie owned', href: `/item/${encodeURIComponent(m.key)}`, 'data-link': '', 'aria-label': `${label}, in your library`,
    },
    h('div', { class: 'art' }, lazyImage(imageUrl(m.key, { w: 240 }), { alt: '', placeholder: m.title }),
      h('span', { class: 'miss-flag owned' }, icon('check'), 'In library')),
    h('div', { class: 'meta' }, h('div', { class: 'title', title: m.title }, m.title), h('div', { class: 'sub' }, m.year || '—'))));
  }
  const flag = m.released ? 'Missing' : comingText(m.release_date);
  const attrs = {
    class: ['tile', 'miss-movie', 'missing', !m.released && 'unreleased'], 'aria-label': `${label}, ${flag.toLowerCase()}${m.tmdb_url ? ' (opens TMDB)' : ''}`,
  };
  if (m.tmdb_url) Object.assign(attrs, { href: m.tmdb_url, target: '_blank', rel: 'noopener noreferrer' });
  return h('li', {}, h(m.tmdb_url ? 'a' : 'div', attrs,
    h('div', { class: 'art' }, lazyImage(m.poster_url, { alt: '', placeholder: m.title }),
      h('span', { class: ['miss-flag', m.released ? 'missing' : 'coming'] }, icon(m.released ? 'x' : 'clock'), flag)),
    h('div', { class: 'meta' }, h('div', { class: 'title', title: m.title }, m.title), h('div', { class: 'sub' }, m.year || '—'))));
}

function comingText(date) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date || '');
  if (!match) return 'Not out yet';
  const day = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  return `Coming ${day.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' })}`;
}

function csvField(value) {
  const text = String(value ?? '');
  return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}
