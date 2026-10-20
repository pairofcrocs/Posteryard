// Page: previews. The images saved while DRY_RUN is on, newest first. See docs/webui-api.md "Status and actions".
import { api, imageUrl } from '../api.js';
import {
  h, mount, icon, button, toast, modal, confirmDialog, segmented, spinner, emptyState, errorState, lazyImage, timeAgo, formatDate,
  formatBytes, formatNumber, TARGET_LABEL,
} from '../ui.js';

const PAGE = 60;
const FILTERS = [['poster', 'Posters'], ['art', 'Backgrounds'], ['thumb', 'Thumbnails'], ['', 'All']];

export default async function render(view, ctx) {
  const root = h('div', { class: 'page-previews' });
  mount(view, root);
  let target = FILTERS.some(([v]) => v === ctx.query.get('target')) ? ctx.query.get('target') : 'poster';
  let items = [];
  let ordered = []; // items in the order they are shown, for the lightbox
  let total = 0;
  let loadId = 0;
  let lightbox = null;

  const intro = h('div', { class: 'previews-intro' });
  const count = h('span', { class: 'small muted' });
  const body = h('div', { class: 'previews-body' });
  const more = h('div', { class: 'load-more' });
  const clear = button({ label: 'Clear previews', icon: 'trash', kind: 'danger', small: true, onClick: clearAll });

  const paintIntro = (status) => {
    const live = status && !status.dry_run;
    mount(intro, h('div', { class: ['banner', live ? 'accent' : 'info'] }, icon(live ? 'upload' : 'eye'),
      h('span', { class: 'grow' }, live
        ? 'Posteryard is live, so new images go to your server. These previews are left from preview mode or a preview you asked for.'
        : 'Preview mode (DRY_RUN) is on: Posteryard saves each image here instead of uploading it. Nothing on your server changes.'),
      !live && h('a', { class: 'btn small', href: '/settings', 'data-link': '' }, 'Go live')));
  };

  mount(root,
    intro,
    h('div', { class: 'previews-toolbar' },
      segmented(FILTERS, target, (value) => {
        target = value;
        const query = new URLSearchParams(ctx.query);
        if (value === 'poster') query.delete('target'); else query.set('target', value);
        ctx.setQuery(query);
        load();
      }),
      count, h('span', { class: 'grow' }), clear),
    body, more);
  root.querySelector('.segmented').setAttribute('aria-label', 'Image kind');
  paintIntro(ctx.status);
  const unsubscribe = ctx.onStatus(paintIntro);

  async function fetchPage(offset) {
    const params = new URLSearchParams({ offset: String(offset), limit: String(PAGE) });
    if (target) params.set('target', target);
    return api.get(`/api/previews?${params}`);
  }

  async function load() {
    const id = ++loadId;
    mount(body, spinner('Loading previews'));
    mount(more);
    try {
      const data = await fetchPage(0);
      if (id !== loadId) return;
      items = data.items || [];
      total = data.total || 0;
      paint();
    } catch (err) {
      if (id !== loadId) return;
      mount(body, errorState(err, load));
    }
  }

  async function loadMore() {
    const id = loadId;
    const data = await fetchPage(items.length);
    if (id !== loadId) return false;
    items = items.concat(data.items || []);
    total = data.total || total;
    paint();
    return true;
  }

  function paint() {
    count.textContent = total ? `${formatNumber(total)} ${total === 1 ? 'image' : 'images'}` : '';
    clear.disabled = !total && !target;
    if (!items.length) {
      const what = target ? FILTERS.find(([v]) => v === target)[1].toLowerCase() : 'previews';
      mount(body, emptyState({ icon: 'image', title: `No ${what} yet`,
        message: 'Previews appear here while preview mode is on, and when you preview a title or a TMDB title under Tools.',
        action: h('a', { class: 'btn', href: '/tools', 'data-link': '' }, icon('tool'), 'Preview a TMDB title') }));
      mount(more);
      return;
    }
    const posters = items.filter((item) => item.target === 'poster');
    const wide = items.filter((item) => item.target !== 'poster');
    ordered = [...posters, ...wide];
    const sections = [];
    if (posters.length) sections.push(section(target ? null : 'Posters', 'posters', posters));
    if (wide.length) sections.push(section(target ? null : 'Backgrounds and thumbnails', 'posters wide', wide));
    mount(body, sections);
    mount(more, items.length < total
      ? button({ label: `Load more (${formatNumber(total - items.length)} left)`, onClick: loadMore })
      : null);
  }

  function section(title, cls, list) {
    return h('section', { class: 'previews-section' }, title && h('h2', {}, title),
      h('div', { class: cls }, list.map((item) => tile(item))));
  }

  function tile(item) {
    const wide = item.target !== 'poster';
    return h('button', { type: 'button', class: ['tile', 'preview-tile', wide && 'wide'], onClick: () => openLightbox(item),
      'aria-label': `${item.title || item.name}, ${TARGET_LABEL[item.target] || item.target}` },
    h('div', { class: 'art' }, thumb(item, wide)),
    h('div', { class: 'meta' }, h('div', { class: 'title', title: item.title || item.name }, item.title || item.name),
      h('div', { class: 'sub' }, `${TARGET_LABEL[item.target] || item.target} · ${timeAgo(item.mtime)}`)));
  }

  // A resized copy through the item image route, else the full preview file.
  function thumb(item, wide) {
    if (!item.key) return lazyImage(item.url, { alt: '', placeholder: item.title || item.name });
    const img = h('img', { src: imageUrl(item.key, { target: item.target, source: 'preview', w: wide ? 520 : 320, bust: Math.round(item.mtime) }),
      alt: '', loading: 'lazy', decoding: 'async' });
    img.addEventListener('error', () => {
      if (img.dataset.full) img.replaceWith(lazyImage(null, { placeholder: item.title || item.name }));
      else { img.dataset.full = '1'; img.src = item.url; }
    });
    return img;
  }

  function openLightbox(start) {
    let current = ordered.indexOf(start);
    const stage = h('div', { class: 'lb-stage' });
    const info = h('div', { class: 'lb-info' });
    const prev = button({ icon: 'chevronLeft', title: 'Previous (←)', kind: 'ghost', onClick: () => go(-1) });
    const next = button({ icon: 'chevronRight', title: 'Next (→)', kind: 'ghost', onClick: () => go(1) });
    const show = () => {
      const item = ordered[current];
      if (!item) return;
      const head = lightbox?.el.querySelector('.modal-head h2');
      if (head) mount(head, item.title || item.name, h('span', { class: 'lb-count' }, `${current + 1} of ${formatNumber(total)}`));
      mount(stage, prev, h('img', { class: ['lb-image', item.target !== 'poster' && 'wide'], src: item.url, alt: `${TARGET_LABEL[item.target] || item.target} for ${item.title || item.name}` }), next);
      prev.disabled = current === 0;
      next.disabled = current >= ordered.length - 1 && items.length >= total;
      mount(info,
        h('dl', { class: 'props small' },
          h('dt', {}, 'Image'), h('dd', {}, TARGET_LABEL[item.target] || item.target),
          h('dt', {}, 'Saved'), h('dd', {}, `${formatDate(item.mtime)} (${timeAgo(item.mtime)})`),
          h('dt', {}, 'Size'), h('dd', {}, formatBytes(item.size)),
          h('dt', {}, 'File'), h('dd', {}, h('code', {}, item.name))),
        h('div', { class: 'row wrap' },
          item.key && h('a', { class: 'btn primary small', href: `/item/${encodeURIComponent(item.key)}`, 'data-link': '', onClick: () => lightbox?.close() }, 'Open the title'),
          h('a', { class: 'btn small', href: item.url, target: '_blank', rel: 'noopener' }, icon('external'), 'Open the image')));
    };
    const go = async (step) => {
      const item = ordered[current];
      if (current + step >= ordered.length && items.length < total) {
        await loadMore();
        current = ordered.indexOf(item);
      }
      const next = current + step;
      if (next < 0 || next >= ordered.length) return;
      current = next;
      show();
    };
    const onKey = (event) => {
      if (event.key === 'ArrowLeft') { event.preventDefault(); go(-1); }
      if (event.key === 'ArrowRight') { event.preventDefault(); go(1); }
    };
    document.addEventListener('keydown', onKey);
    lightbox = modal({
      title: 'Preview', wide: true,
      content: h('div', { class: 'page-previews lightbox' }, stage, info),
      onClose: () => { document.removeEventListener('keydown', onKey); lightbox = null; },
    });
    lightbox.el.classList.add('page-previews', 'preview-lightbox');
    show();
    next.focus();
  }

  async function clearAll() {
    const ok = await confirmDialog({
      title: 'Clear previews?',
      message: 'Deletes every saved preview image. Nothing on your server changes. While preview mode is on, new previews come back at the next pass.',
      confirmLabel: 'Clear previews', danger: true,
    });
    if (!ok) return;
    const result = await api.del('/api/previews');
    toast(`Deleted ${formatNumber(result?.deleted ?? 0)} previews`, 'success');
    load();
  }

  await load();
  return () => {
    unsubscribe();
    lightbox?.close();
  };
}
