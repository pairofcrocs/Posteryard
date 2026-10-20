// Page: Live TV. The guide posters Posteryard keeps, or what the feature does when LIVE_TV is off.
import { api } from '../api.js';
import { h, mount, icon, spinner, emptyState, errorState, lazyImage, timeAgo, formatDate, formatNumber, badge } from '../ui.js';

export default async function render(view, ctx) {
  const root = h('div', { class: 'page-livetv' });
  mount(view, root);

  async function load() {
    mount(root, spinner('Loading the guide'));
    let data;
    try {
      data = await api.get('/api/guide');
    } catch (err) {
      mount(root, errorState(err, load));
      return;
    }
    if (!data.enabled) mount(root, explainer(ctx.status));
    else mount(root, guide(data.items || [], ctx.status));
  }

  await load();
  return undefined;
}

function explainer(status) {
  const emby = (status?.server?.name || '').toLowerCase() === 'emby';
  return h('section', { class: 'livetv-intro card' },
    h('div', { class: 'livetv-hero' }, icon('tv', 'xl'),
      h('div', { class: 'grow' }, h('h2', {}, 'Posters for the Live TV guide'),
        h('p', { class: 'muted' }, 'Live TV is off. Turn it on to give every show and movie in the guide a poster.'))),
    h('ul', { class: 'livetv-points' },
      h('li', {}, h('b', {}, 'Found on TMDB: '), 'a poster in the library style, without a label.'),
      h('li', {}, h('b', {}, 'With its own guide picture: '), 'that picture with the title.'),
      h('li', {}, h('b', {}, 'Anything else: '), 'the channel\'s logo, large, over its own colours, with the title below.'),
      h('li', {}, 'Works with Plex and Jellyfin, not with Emby. Posteryard checks the guide at every sweep and puts its posters back after a guide refresh.')),
    emby
      ? h('div', { class: 'banner warn' }, icon('alert'), h('span', { class: 'grow' }, 'Your server is Emby. Emby Live TV is not supported.'))
      : h('div', { class: 'row wrap' },
        h('a', { class: 'btn primary', href: '/settings?q=LIVE_TV', 'data-link': '' }, icon('gear'), 'Turn on LIVE_TV in Settings'),
        h('a', { class: 'btn ghost', href: 'https://github.com/SandObserver/Posteryard/blob/main/docs/how-it-works.md#live-tv', target: '_blank', rel: 'noopener noreferrer' },
          icon('external'), 'How it works')));
}

function guide(items, status) {
  if (!items.length) {
    return emptyState({ icon: 'tv', title: 'No guide posters yet',
      message: 'Live TV is on. Posteryard makes guide posters at the next sweep. Check the guide in your server has titles.' });
  }
  const filter = h('input', { class: 'input', type: 'search', placeholder: 'Filter titles', 'aria-label': 'Filter guide titles' });
  const sorted = [...items].sort((a, b) => (b.seen_at || 0) - (a.seen_at || 0));
  const rows = sorted.map((item) => {
    const uploaded = Boolean(item.image_key);
    const picture = item.picture || '';
    const tr = h('tr', { 'data-search': `${item.title} ${item.key}`.toLowerCase() },
      h('td', { class: 'title-cell' }, h('div', { class: 'truncate', title: item.title }, item.title),
        h('div', { class: 'small faint truncate', title: item.key }, item.key)),
      h('td', { 'data-label': 'Picture used' }, pictureCell(picture)),
      h('td', { 'data-label': 'Last seen', title: formatDate(item.seen_at) }, timeAgo(item.seen_at)),
      h('td', { 'data-label': 'Poster' }, uploaded ? badge('Uploaded', 'uploaded') : badge(status && !status.dry_run ? 'Not uploaded' : 'Preview', 'preview')));
    return tr;
  });
  const count = h('span', { class: 'small muted' }, `${formatNumber(items.length)} ${items.length === 1 ? 'title' : 'titles'}`);
  const none = h('p', { class: 'muted hidden', style: { padding: '18px' } }, 'No titles match.');
  filter.addEventListener('input', () => {
    const q = filter.value.trim().toLowerCase();
    let shown = 0;
    for (const tr of rows) {
      const match = !q || tr.dataset.search.includes(q);
      tr.classList.toggle('hidden', !match);
      if (match) shown += 1;
    }
    none.classList.toggle('hidden', Boolean(shown));
  });
  return [
    h('div', { class: 'livetv-toolbar' }, h('p', { class: 'muted grow' }, 'Shows and movies in the guide that have a Posteryard poster. Titles leave the list when the guide drops them.'),
      count),
    h('div', { class: 'livetv-filter' }, filter),
    h('div', { class: 'card' },
      h('table', { class: 'table guide-table' },
        h('thead', {}, h('tr', {}, h('th', {}, 'Title'), h('th', {}, 'Picture used'), h('th', {}, 'Last seen'), h('th', {}, 'Poster'))),
        h('tbody', {}, rows)),
      none),
  ];
}

function pictureCell(picture) {
  if (!picture) return h('span', { class: 'muted' }, 'TMDB art or the channel logo');
  if (/^https?:\/\//i.test(picture)) {
    // The page's CSP allows https images only; an http picture is a plain link.
    return h('a', { class: 'guide-picture', href: picture, target: '_blank', rel: 'noopener noreferrer', title: picture },
      /^https:/i.test(picture) && lazyImage(picture, { alt: 'Guide picture', placeholder: '' }), h('span', {}, 'Guide picture'));
  }
  return h('span', { class: 'truncate', title: picture }, 'Guide picture');
}
