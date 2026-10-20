// Tab panels of the title page: Overview, Art candidates, Seasons/Episodes and Details.
import { api, imageUrl } from '../api.js';
import {
  h, mount, icon, button, statusBadge, emptyState, errorState, lazyImage, posterTile, formatDate, timeAgo, copyText,
  TARGET_LABEL,
} from '../ui.js';
import { compareSlider } from './item-compare.js';
import { lightbox } from './item-dialogs.js';
import { bustFor } from './item-cache.js';
import { LABEL_HELP, sourceText } from './item-labels.js';

const record = (item, target) => (item.records || []).find((r) => r.target === target) || null;

// ---- Overview ----

export function overviewTab(item, { bust, serverName }) {
  const cards = (item.targets || []).map((target) => {
    const rec = record(item, target);
    const wide = target !== 'poster';
    const w = wide ? 960 : 600;
    const hasPreview = item.has_preview?.[target];
    const ours = imageUrl(item.key, { target, source: hasPreview ? 'preview' : 'auto', w, bust });
    const theirs = imageUrl(item.key, { target, source: 'server', w, bust });
    let body;
    let caption;
    if (hasPreview) {
      body = compareSlider({ before: { src: theirs, label: serverName }, after: { src: ours, label: 'Posteryard' }, wide, alt: TARGET_LABEL[target] });
      caption = rec?.status === 'uploaded'
        ? `Drag the divider to compare. Posteryard uploaded its version ${timeAgo(rec.updated_at)}, so ${serverName} should show the same.`
        : `Drag the divider to compare Posteryard's preview with the image on ${serverName} now.`;
    } else {
      body = h('div', { class: ['shot', wide && 'wide'] }, lazyImage(theirs, { alt: `${TARGET_LABEL[target]} on ${serverName}`, placeholder: 'No image' }));
      caption = rec?.status === 'uploaded' ? `On ${serverName}. Posteryard uploaded it ${timeAgo(rec.updated_at)}.`
        : rec?.status === 'manual' ? `On ${serverName}. It was changed by hand, so Posteryard leaves it alone.`
          : `On ${serverName} now.`;
    }
    return h('section', { class: ['card', 'image-card', wide && 'wide'] },
      h('div', { class: 'card-head' }, h('h3', {}, TARGET_LABEL[target] || target), statusBadge(rec?.status ?? null)),
      h('div', { class: 'card-body' }, body, h('p', { class: 'small muted caption' }, caption),
        rec?.status === 'failed' && rec.last_error ? h('p', { class: 'small error-text' }, icon('alert'), rec.last_error) : null));
  });
  if (!cards.length) return emptyState({ icon: 'image', title: 'No images', message: 'Posteryard makes no images for this title.' });
  return h('div', { class: 'image-cards' }, cards);
}

// ---- Art candidates ----

function verdictText(c) {
  if (c.used) return 'In use';
  if (c.skipped) return 'Skipped by Next art';
  const text = c.verdict || (c.clean ? 'clean' : 'not used');
  return text.charAt(0).toUpperCase() + text.slice(1);
}

const VERDICT_TONE = (c) => (c.used ? 'green' : c.skipped ? 'purple' : c.clean ? 'blue' : 'red');

/**
 * state: {result, stale, error, running} kept by the page across tab switches.
 * run(): starts the Why job. use(candidate): applies one.
 */
export function candidatesTab(item, state, { run, use }) {
  const intro = h('div', { class: 'why-intro' },
    h('div', { class: 'grow' },
      h('h3', {}, 'Art candidates'),
      h('p', { class: 'muted small' }, 'Every picture Posteryard considers for this title, and why it was used or not. Each one is checked for printed text, so this can take a minute. Nothing changes.')),
    state.running ? h('div', { class: 'row muted small' }, h('span', { class: 'spinner' }), 'Checking…')
      : button({ label: state.result ? 'Check again' : 'Check art candidates', icon: state.result ? 'refresh' : 'search', kind: state.result ? 'secondary' : 'primary', onClick: run }));
  const parts = [intro];
  if (state.running && !state.result) {
    parts.push(h('div', { class: 'why-loading' }, h('span', { class: 'spinner l' }),
      h('div', {}, h('strong', {}, 'Checking art candidates'), h('div', { class: 'small muted' }, 'Looking at every image for printed text. You can switch tabs; this keeps going.'))));
  }
  if (state.error) parts.push(h('div', { class: 'banner danger' }, icon('alert'), h('span', { class: 'grow' }, state.error)));
  const why = state.result;
  if (!why) return h('div', { class: 'why' }, parts);
  if (state.stale) {
    parts.push(h('div', { class: 'banner warn' }, icon('info'), h('span', { class: 'grow' }, 'The art changed since this check. Check again to see the current choice.')));
  }

  parts.push(h('div', { class: 'why-summary card' },
    h('div', { class: 'why-used' },
      lazyImage(why.art_url, { alt: 'The art in use', placeholder: why.art_url ? 'No preview' : 'None' }),
      h('div', { class: 'grow' },
        h('div', { class: 'small muted' }, 'Art in use'),
        h('div', { class: 'why-note' }, why.note || (why.art ? 'Chosen automatically' : 'No clean art was found')),
        why.art ? h('div', { class: 'mono small faint truncate', title: why.art }, why.art) : null)),
    why.logo_url || why.logo ? h('div', { class: 'why-logo' },
      why.logo_url ? h('img', { src: why.logo_url, alt: 'Title logo', loading: 'lazy' }) : null,
      h('div', {}, h('div', { class: 'small muted' }, 'Title logo'), h('div', { class: 'mono small faint' }, why.logo || '')))
      : h('div', { class: 'why-logo' }, h('div', {}, h('div', { class: 'small muted' }, 'Title logo'), h('div', { class: 'small' }, 'None: the name is set in white.')))));

  for (const notice of why.notices || []) parts.push(h('div', { class: 'banner info' }, icon('info'), h('span', { class: 'grow' }, notice)));
  if ((why.unchecked || []).length) {
    parts.push(h('div', { class: 'unchecked small muted' }, icon('clock'), h('span', {}, 'Not checked: ', why.unchecked.join(', '),
      '. Posteryard stopped at the first clean picture.')));
  }

  for (const group of why.groups || []) {
    const candidates = group.candidates || [];
    parts.push(h('section', { class: 'why-group' },
      h('div', { class: 'row' }, h('h3', { class: 'grow' }, group.name), h('span', { class: 'small muted' }, `${candidates.length} ${candidates.length === 1 ? 'image' : 'images'}`)),
      candidates.length
        ? h('div', { class: ['candidates', /backdrop|background|fanart.*back/i.test(group.name) && 'wide'] }, candidates.map((c) => {
          const usable = c.clean && !c.used && !c.skipped;
          const open = () => lightbox({ candidate: c, group: group.name, onUse: usable ? () => use(c) : null });
          return h('div', { class: ['candidate', c.used && 'used', !c.clean && !c.used && 'rejected'] },
            h('button', { type: 'button', class: 'candidate-art', onClick: open, 'aria-label': `Enlarge ${c.path}` },
              lazyImage(c.thumb_url, { alt: '', placeholder: 'No preview' }),
              c.used ? h('span', { class: 'used-mark' }, icon('check'), 'In use') : null),
            h('div', { class: 'candidate-meta' },
              h('span', { class: ['badge', VERDICT_TONE(c), 'verdict'], title: c.verdict }, verdictText(c)),
              h('div', { class: 'small muted' }, c.width ? `${c.width} × ${c.height}` : '', c.votes ? h('span', {}, c.width ? ' · ' : '', c.votes) : null),
              usable ? button({ label: 'Use this', icon: 'check', small: true, onClick: () => use(c) }) : null));
        }))
        : h('p', { class: 'small muted' }, 'None found.')));
  }
  if (!(why.groups || []).length) parts.push(emptyState({ icon: 'image', title: 'No candidates', message: 'No art sources had pictures for this title.' }));
  return h('div', { class: 'why' }, parts);
}

// ---- Seasons and episodes ----

export async function childrenTab(item, host, { isCurrent, bust }) {
  mount(host, h('div', { class: 'loading' }, h('span', { class: 'spinner' }), 'Loading'));
  let items;
  try {
    items = (await api.get(`/api/items/${encodeURIComponent(item.key)}/children`)).items || [];
  } catch (err) {
    if (isCurrent()) mount(host, errorState(err, () => childrenTab(item, host, { isCurrent, bust })));
    return;
  }
  if (!isCurrent()) return;
  if (!items.length) {
    mount(host, emptyState({ icon: 'layers', title: item.type === 'show' ? 'No seasons' : 'No episodes', message: 'The server lists none.' }));
    return;
  }
  const episodes = items.every((child) => child.type === 'episode');
  mount(host, h('div', { class: ['posters', 'children', episodes && 'wide'] }, items.map((child) => posterTile({
    href: `/item/${encodeURIComponent(child.key)}`,
    src: imageUrl(child.key, { target: episodes ? 'thumb' : 'poster', w: episodes ? 480 : 342, bust: bustFor(child.key) }),
    title: child.title,
    subtitle: episodes ? (child.index != null ? `Episode ${child.index}` : 'Episode') : child.year || '',
    status: child.status,
    wide: episodes,
  }))));
}

// ---- Details ----

export function detailsTab(item) {
  const copyRow = (label, value) => [h('dt', {}, label), h('dd', {}, value != null && value !== ''
    ? h('span', { class: 'row gap-s' }, h('span', { class: 'mono' }, String(value)),
      button({ icon: 'copy', kind: 'ghost', small: true, title: `Copy ${label.toLowerCase()}`, onClick: () => copyText(String(value)) }))
    : h('span', { class: 'muted' }, '—'))];

  const records = item.records || [];
  const recordsCard = h('section', { class: 'card' },
    h('div', { class: 'card-head' }, h('h3', {}, 'Image records')),
    h('div', { class: 'card-body flush table-scroll' }, records.length || item.item_error
      ? h('table', { class: 'table' },
        h('thead', {}, h('tr', {}, ['Image', 'Status', 'Updated', 'Failures', 'Last error', 'Fingerprint', 'Image key'].map((t) => h('th', {}, t)))),
        h('tbody', {},
          records.map((r) => h('tr', {},
            h('td', {}, TARGET_LABEL[r.target] || r.target), h('td', {}, statusBadge(r.status)),
            h('td', { class: 'nowrap small', title: formatDate(r.updated_at) }, timeAgo(r.updated_at)),
            h('td', {}, String(r.failures ?? 0)),
            h('td', { class: 'small' }, r.last_error || h('span', { class: 'muted' }, '—')),
            h('td', { class: 'mono small break' }, r.fingerprint || '—'),
            h('td', { class: 'mono small break' }, r.image_key || '—'))),
          item.item_error ? h('tr', {},
            h('td', {}, TARGET_LABEL.item), h('td', {}, statusBadge('failed')),
            h('td', { class: 'nowrap small', title: formatDate(item.item_error.updated_at) }, timeAgo(item.item_error.updated_at)),
            h('td', {}, String(item.item_error.failures ?? 0)),
            h('td', { class: 'small' }, item.item_error.last_error || '—'), h('td', {}, '—'), h('td', {}, '—')) : null))
      : h('p', { class: 'muted', style: { padding: '18px' } }, 'Posteryard has no record of this title yet. It is rendered at the next sweep or full pass.')));

  const override = item.override;
  const overrideCard = h('section', { class: 'card' },
    h('div', { class: 'card-head' }, h('h3', {}, 'Art choice')),
    h('div', { class: 'card-body' }, override
      ? h('dl', { class: 'props' },
        h('dt', {}, 'Custom art'), h('dd', {}, override.custom ? `Yes, ${sourceText(override.source)}` : 'No'),
        h('dt', {}, 'Skipped with Next art'), h('dd', {}, (override.skip || []).length
          ? h('div', { class: 'col gap-s' }, override.skip.map((path) => h('span', { class: 'mono small' }, path)))
          : 'None'))
      : h('p', { class: 'muted' }, 'Automatic. Posteryard picks the best clean art.')));

  const labels = item.all_labels || [];
  const labelsCard = h('section', { class: 'card' },
    h('div', { class: 'card-head' }, h('h3', {}, 'Labels')),
    h('div', { class: 'card-body' }, labels.length
      ? h('ul', { class: 'label-list' }, labels.map((label) => h('li', {},
        h('span', { class: ['badge', label.startsWith('posteryard-') && 'accent', 'mono-badge'] }, label),
        LABEL_HELP[label] ? h('span', { class: 'small muted' }, LABEL_HELP[label]) : null)))
      : h('p', { class: 'muted' }, 'No labels.')));

  const idsCard = h('section', { class: 'card' },
    h('div', { class: 'card-head' }, h('h3', {}, 'Identifiers')),
    h('div', { class: 'card-body' }, h('dl', { class: 'props' },
      copyRow('Rating key', item.key), copyRow('TMDB id', item.tmdb_id),
      h('dt', {}, 'Library'), h('dd', {}, item.library || '—'),
      h('dt', {}, 'Added'), h('dd', {}, item.added_at ? formatDate(item.added_at) : '—'),
      h('dt', {}, 'Images'), h('dd', {}, (item.targets || []).map((t) => TARGET_LABEL[t] || t).join(', ') || '—'))));

  return h('div', { class: 'details' }, recordsCard, h('div', { class: 'grid cols-3 details-cards' }, overrideCard, labelsCard, idsCard));
}
