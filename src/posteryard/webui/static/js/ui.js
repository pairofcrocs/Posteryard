// Shared building blocks for the pages. Use these before writing new ones.

import { api, ApiError } from './api.js';

/** Hyperscript: h('div', {class: 'row', onClick: fn, style: {gap: '4px'}}, child, [children], null). */
export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (name === 'class') el.className = Array.isArray(value) ? value.filter(Boolean).join(' ') : value;
    else if (name === 'style' && typeof value === 'object') Object.assign(el.style, value);
    else if (name === 'dataset') Object.assign(el.dataset, value);
    else if (name.startsWith('on') && typeof value === 'function') el.addEventListener(name.slice(2).toLowerCase(), value);
    else if (name === 'html') el.innerHTML = value; // Only for trusted, static markup such as icons.
    else if (name in el && typeof value !== 'string') el[name] = value;
    else el.setAttribute(name, value === true ? '' : String(value));
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

/** Replace an element's children. */
export function mount(el, ...children) {
  el.replaceChildren();
  append(el, children);
  return el;
}

const ICONS = {
  home: '<path d="M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"/>',
  grid: '<rect x="3" y="3" width="7" height="9" rx="1"/><rect x="14" y="3" width="7" height="9" rx="1"/><rect x="3" y="15" width="7" height="6" rx="1"/><rect x="14" y="15" width="7" height="6" rx="1"/>',
  activity: '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
  alert: '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/>',
  image: '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="m21 15-5-5L5 21"/>',
  tv: '<rect x="2" y="7" width="20" height="15" rx="2"/><path d="m17 2-5 5-5-5"/>',
  tool: '<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.8-3.8a6 6 0 0 1-7.9 7.9l-6.9 6.9a2.1 2.1 0 0 1-3-3l6.9-6.9a6 6 0 0 1 7.9-7.9z"/>',
  gear: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/>',
  refresh: '<path d="M21 12a9 9 0 1 1-2.6-6.4L21 8"/><path d="M21 3v5h-5"/>',
  play: '<path d="m6 4 14 8-14 8z"/>',
  pause: '<rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  x: '<path d="M18 6 6 18M6 6l12 12"/>',
  chevronRight: '<path d="m9 18 6-6-6-6"/>',
  chevronLeft: '<path d="m15 18-6-6 6-6"/>',
  chevronDown: '<path d="m6 9 6 6 6-6"/>',
  external: '<path d="M15 3h6v6M10 14 21 3M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
  upload: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12"/>',
  download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/>',
  trash: '<path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2m3 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/>',
  eye: '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
  sparkles: '<path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/><path d="M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z"/>',
  logout: '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9"/>',
  menu: '<path d="M3 6h18M3 12h18M3 18h18"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  moon: '<path d="M21 12.8A9 9 0 1 1 11.2 3 7 7 0 0 0 21 12.8z"/>',
  info: '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/>',
  clock: '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
  layers: '<path d="m12 2 10 5-10 5L2 7z"/><path d="m2 17 10 5 10-5M2 12l10 5 10-5"/>',
  wand: '<path d="m15 4 5 5L9 20l-5-5zM14 5l5 5"/><path d="M4 4l1 2 2 1-2 1-1 2-1-2-2-1 2-1z"/>',
  skip: '<path d="m5 4 10 8-10 8zM19 5v14"/>',
  undo: '<path d="M3 7v6h6"/><path d="M21 17a9 9 0 0 0-15-6.7L3 13"/>',
  link: '<path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7"/><path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7"/>',
  film: '<rect x="2" y="2" width="20" height="20" rx="2"/><path d="M7 2v20M17 2v20M2 12h20M2 7h5M2 17h5M17 17h5M17 7h5"/>',
  server: '<rect x="2" y="3" width="20" height="8" rx="2"/><rect x="2" y="13" width="20" height="8" rx="2"/><path d="M6 7h.01M6 17h.01"/>',
  bell: '<path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9M13.7 21a2 2 0 0 1-3.4 0"/>',
  copy: '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
  filter: '<path d="M22 3H2l8 9.5V19l4 2v-8.5z"/>',
  lock: '<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
};

/** An inline SVG icon from the set above. size: '' | 'l' | 'xl'. */
export function icon(name, size = '') {
  const span = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  span.setAttribute('viewBox', '0 0 24 24');
  span.setAttribute('class', `icon ${size}`.trim());
  span.setAttribute('aria-hidden', 'true');
  span.innerHTML = ICONS[name] || ICONS.info;
  return span;
}

/** A button. kind: 'primary' | 'secondary' | 'danger' | 'ghost'. An async onClick shows a busy state. */
export function button({ label = '', icon: iconName = null, kind = 'secondary', small = false, title = null, disabled = false, onClick = null, type = 'button' } = {}) {
  const el = h('button', {
    type,
    class: ['btn', kind !== 'secondary' && kind, small && 'small', !label && 'icon-only'],
    title: title || (label ? null : iconName),
    'aria-label': label ? null : title || iconName,
    disabled,
  }, iconName && icon(iconName), label);
  if (onClick) {
    el.addEventListener('click', async (event) => {
      if (el.classList.contains('busy')) return;
      el.classList.add('busy');
      try {
        await onClick(event);
      } catch (err) {
        toast(err.message || String(err), 'error');
      } finally {
        el.classList.remove('busy');
      }
    });
  }
  return el;
}

const STATUS_TEXT = {
  uploaded: 'Uploaded', uploading: 'Uploading', preview: 'Preview', manual: 'By hand', failed: 'Failed',
};

/** The badge for an image record's status; null shows "Not yet". */
export function statusBadge(status) {
  return h('span', { class: ['badge', status || ''] }, status ? STATUS_TEXT[status] || status : 'Not yet');
}

export function badge(text, tone = '') {
  return h('span', { class: ['badge', tone] }, text);
}

let toastHost = null;

/** A short message bottom right. kind: 'success' | 'error' | 'info'. */
export function toast(message, kind = 'info', timeout = kind === 'error' ? 8000 : 4000) {
  if (!toastHost) {
    toastHost = h('div', { class: 'toasts', role: 'status', 'aria-live': 'polite' });
    document.body.append(toastHost);
  }
  const iconName = kind === 'success' ? 'check' : kind === 'error' ? 'alert' : 'info';
  const close = () => el.remove();
  const el = h('div', { class: ['toast', kind] }, icon(iconName), h('div', { class: 'grow' }, message),
    h('button', { class: 'btn ghost small icon-only', 'aria-label': 'Close', onClick: close }, icon('x')));
  toastHost.append(el);
  if (timeout) setTimeout(close, timeout);
  return { close, el };
}

/** A dialog. actions: [{label, kind, onClick}] where onClick may return false to keep it open. */
export function modal({ title, content, actions = [], wide = false, onClose = null }) {
  const previous = document.activeElement;
  const close = () => {
    backdrop.remove();
    document.removeEventListener('keydown', onKey);
    if (onClose) onClose();
    if (previous && previous.focus) previous.focus();
  };
  const onKey = (event) => { if (event.key === 'Escape') close(); };
  const foot = actions.length ? h('div', { class: 'modal-foot' }, actions.map((action) => button({
    ...action,
    onClick: async (event) => {
      const keep = action.onClick ? await action.onClick(event) : undefined;
      if (keep !== false) close();
    },
  }))) : null;
  const dialog = h('div', { class: ['modal', wide && 'wide'], role: 'dialog', 'aria-modal': 'true', 'aria-label': title },
    h('div', { class: 'modal-head' }, h('h2', {}, title),
      h('button', { class: 'btn ghost small icon-only', 'aria-label': 'Close', onClick: close }, icon('x'))),
    h('div', { class: 'modal-body' }, content),
    foot);
  const backdrop = h('div', { class: 'modal-backdrop', onMousedown: (event) => { if (event.target === backdrop) close(); } }, dialog);
  document.body.append(backdrop);
  document.addEventListener('keydown', onKey);
  const focusable = dialog.querySelector('input, select, textarea, .modal-foot .btn.primary, .modal-foot .btn');
  if (focusable) focusable.focus();
  return { close, el: dialog };
}

/** Ask before something that changes the server. Resolves true when confirmed. */
export function confirmDialog({ title, message, confirmLabel = 'Continue', danger = false }) {
  return new Promise((resolve) => {
    let answered = false;
    modal({
      title,
      content: typeof message === 'string' ? h('p', {}, message) : message,
      onClose: () => { if (!answered) resolve(false); },
      actions: [
        { label: 'Cancel', kind: 'ghost' },
        { label: confirmLabel, kind: danger ? 'danger' : 'primary', onClick: () => { answered = true; resolve(true); } },
      ],
    });
  });
}

/**
 * Run a job route with a toast that follows it. Resolves with the job's result, or null when it failed
 * (the error is shown). Example: await runJob(`/api/items/${key}/art/next`, {}, 'Next art for Dune').
 */
export async function runJob(path, body, label, { blob = null, quiet = false } = {}) {
  const note = toast(h('span', { class: 'row' }, h('span', { class: 'spinner' }), `${label}…`), 'info', 0);
  try {
    const result = await api.job(path, body, { blob });
    note.close();
    if (!quiet) toast(`${label}: ${describeOutcome(result)}`, 'success');
    return result;
  } catch (err) {
    note.close();
    toast(`${label}: ${err.message}`, 'error');
    return null;
  }
}

const OUTCOMES = {
  uploaded: 'uploaded to the server', preview: 'preview saved (DRY_RUN is on)', unchanged: 'nothing changed',
  manual: 'left alone, it was changed by hand', skipped: 'skipped', failed: 'failed, see Problems', gone: 'the server no longer has it',
};

export function describeOutcome(result) {
  if (result && typeof result === 'object' && result.outcome) return OUTCOMES[result.outcome] || result.outcome;
  return 'done';
}

export function spinner(text = 'Loading') {
  return h('div', { class: 'loading' }, h('span', { class: 'spinner' }), text);
}

export function emptyState({ icon: iconName = 'info', title, message = '', action = null }) {
  return h('div', { class: 'empty' }, icon(iconName, 'xl'), h('h3', {}, title), message && h('p', {}, message), action);
}

/** A failed load, with a retry button. */
export function errorState(err, retry) {
  const message = err instanceof ApiError || err instanceof Error ? err.message : String(err);
  return emptyState({ icon: 'alert', title: 'Could not load this', message,
    action: retry ? button({ label: 'Try again', icon: 'refresh', onClick: retry }) : null });
}

export function card({ title = null, actions = null, body, flush = false, className = '' }) {
  return h('section', { class: ['card', className] },
    title && h('div', { class: 'card-head' }, h('h2', {}, title), actions),
    h('div', { class: ['card-body', flush && 'flush'] }, body));
}

export function stat({ label, value, hint = null, tone = '', iconName = null }) {
  return h('div', { class: ['card', 'stat', tone] },
    h('div', { class: 'label' }, iconName && icon(iconName), label),
    h('div', { class: 'value' }, value),
    hint && h('div', { class: 'hint' }, hint));
}

/** Tabs: items [{id, label}]. Returns the element; onChange(id). */
export function tabs(items, active, onChange) {
  const el = h('div', { class: 'tabs', role: 'tablist' });
  for (const item of items) {
    el.append(h('button', {
      class: item.id === active ? 'active' : '', role: 'tab', 'aria-selected': String(item.id === active),
      onClick: (event) => {
        el.querySelectorAll('button').forEach((b) => {
          b.classList.toggle('active', b === event.currentTarget);
          b.setAttribute('aria-selected', String(b === event.currentTarget));
        });
        onChange(item.id);
      },
    }, item.label));
  }
  return el;
}

/** Segmented control: options [[value, label]]. */
export function segmented(options, value, onChange) {
  const el = h('div', { class: 'segmented', role: 'radiogroup' });
  for (const [optionValue, label] of options) {
    el.append(h('button', {
      type: 'button', class: optionValue === value ? 'active' : '', role: 'radio', 'aria-checked': String(optionValue === value),
      onClick: (event) => {
        el.querySelectorAll('button').forEach((b) => b.classList.toggle('active', b === event.currentTarget));
        onChange(optionValue);
      },
    }, label));
  }
  return el;
}

export function toggleSwitch(checked, onChange, label = '') {
  const input = h('input', { type: 'checkbox', role: 'switch', checked, 'aria-label': label || null, onChange: () => onChange(input.checked) });
  return h('label', { class: 'switch' }, input, h('span'));
}

/** An image that shows a placeholder until it loads, and the placeholder text when it fails. */
export function lazyImage(src, { alt = '', placeholder = '', className = '' } = {}) {
  const box = h('div', { class: ['placeholder-art', className] }, placeholder);
  if (!src) return box;
  const img = h('img', { src, alt, loading: 'lazy', decoding: 'async', class: className });
  img.addEventListener('error', () => img.replaceWith(box));
  return img;
}

/** A poster tile linking somewhere. wide for backgrounds and episode thumbnails. */
export function posterTile({ href, src, title, subtitle = '', status, wide = false, corner = null, showStatus = true }) {
  return h(href ? 'a' : 'div', { class: ['tile', wide && 'wide'], href: href || null, 'data-link': href ? '' : null },
    h('div', { class: 'art' }, lazyImage(src, { alt: title, placeholder: title }),
      showStatus && status !== undefined && statusBadge(status), corner && h('div', { class: 'corner' }, corner)),
    h('div', { class: 'meta' }, h('div', { class: 'title', title }, title), subtitle && h('div', { class: 'sub' }, subtitle)));
}

export function timeAgo(value) {
  if (value === null || value === undefined || value === '') return 'never';
  const then = typeof value === 'number' ? value * 1000 : Date.parse(value);
  if (Number.isNaN(then)) return String(value);
  const seconds = Math.round((Date.now() - then) / 1000);
  const future = seconds < 0;
  const s = Math.abs(seconds);
  const text = s < 45 ? 'a few seconds' : s < 90 ? 'a minute' : s < 3600 ? `${Math.round(s / 60)} minutes`
    : s < 5400 ? 'an hour' : s < 86400 ? `${Math.round(s / 3600)} hours` : s < 172800 ? 'a day' : `${Math.round(s / 86400)} days`;
  return future ? `in ${text}` : `${text} ago`;
}

export function formatDate(value) {
  if (value === null || value === undefined || value === '') return '—';
  const date = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

export function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return '—';
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`;
  return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}m`;
}

export function formatBytes(bytes) {
  if (!bytes) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  const i = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
  return `${(bytes / 1024 ** i).toFixed(i ? 1 : 0)} ${units[i]}`;
}

export function formatNumber(value) {
  return typeof value === 'number' ? value.toLocaleString() : value ?? '—';
}

export function debounce(fn, ms = 250) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

/** Copy to the clipboard. Plain HTTP on a LAN address has no Clipboard API, so fall back to a selected text box. */
export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast('Copied', 'success', 1500);
    return;
  } catch { /* no Clipboard API here */ }
  const area = h('textarea', { class: 'input mono', readonly: true, rows: 8 }, text);
  document.body.append(area);
  area.select();
  let copied = false;
  try { copied = document.execCommand('copy'); } catch { /* not allowed */ }
  area.remove();
  if (copied) {
    toast('Copied', 'success', 1500);
    return;
  }
  const shown = h('textarea', { class: 'input mono', readonly: true, rows: 10 }, text);
  modal({ title: 'Copy this text', content: shown, actions: [{ label: 'Done', kind: 'primary' }] });
  shown.select();
}

/** Calls fn every ms while the page is visible; returns a stop function for the page's cleanup. */
export function every(ms, fn) {
  let stopped = false;
  let timer = null;
  const tick = async () => {
    if (stopped) return;
    if (!document.hidden) {
      try { await fn(); } catch { /* the next tick tries again */ }
    }
    if (!stopped) timer = setTimeout(tick, ms);
  };
  timer = setTimeout(tick, ms);
  return () => { stopped = true; clearTimeout(timer); };
}

export const KIND_LABEL = { movie: 'Movie', show: 'Show', season: 'Season', episode: 'Episode', collection: 'Collection' };
export const TARGET_LABEL = { poster: 'Poster', art: 'Background', thumb: 'Thumbnail', item: 'Item' };
