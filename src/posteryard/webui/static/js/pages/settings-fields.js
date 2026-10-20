// Settings page helpers: one control per field type, help text, source badges and the restart flow.
import { api } from '../api.js';
import { h, icon, button, toast, copyText } from '../ui.js';

const DOCS = 'https://github.com/SandObserver/Posteryard/blob/main/';

export const SOURCE = {
  ui: ['Web UI', 'accent', 'Saved in the web UI. It wins over the environment.'],
  env: ['Environment', 'blue', 'Set in the container\'s environment.'],
  default: ['Default', '', 'Not set, so the default applies.'],
};

export const SERVERS = [
  { id: 'plex', label: 'Plex', keys: ['PLEX_URL', 'PLEX_TOKEN'] },
  { id: 'jellyfin', label: 'Jellyfin', keys: ['JELLYFIN_URL', 'JELLYFIN_API_KEY'] },
  { id: 'emby', label: 'Emby', keys: ['EMBY_URL', 'EMBY_API_KEY'] },
];
export const SERVER_KEYS = SERVERS.flatMap((s) => s.keys);

// Connection tests: shown on every group that holds one of the keys.
export const TESTS = [
  { what: 'server', keys: SERVER_KEYS, label: 'Test connection' },
  { what: 'tmdb', keys: ['TMDB_API_KEY'], label: 'Test TMDB' },
  { what: 'fanart', keys: ['FANART_API_KEY'], label: 'Test fanart.tv' },
  { what: 'maintainerr', keys: ['MAINTAINERR_URL'], label: 'Test Maintainerr' },
  { what: 'notify', keys: ['NOTIFY_URLS'], label: 'Test alerts' },
];

/** Help text with `code` and [links](url); docs/ links go to GitHub. */
export function richText(text) {
  const out = [];
  const pattern = /\[([^\]]+)\]\(([^)\s]+)\)|`([^`]+)`/g;
  let last = 0;
  for (const match of String(text || '').matchAll(pattern)) {
    if (match.index > last) out.push(text.slice(last, match.index));
    if (match[3] !== undefined) out.push(h('code', {}, match[3]));
    else {
      let href = match[2];
      if (!/^[a-z]+:/i.test(href)) href = DOCS + href.replace(/^(\.\.\/|\.\/)+/, '');
      out.push(h('a', { class: 'link', href, target: '_blank', rel: 'noopener noreferrer' }, match[1]));
    }
    last = match.index + match[0].length;
  }
  if (last < String(text || '').length) out.push(text.slice(last));
  return out;
}

const TRUE = /^(true|1|yes|on)$/i;

export function splitList(value) {
  return String(value ?? '').split(',').map((part) => part.trim()).filter(Boolean);
}

/** A field's saved value, with the default filled in. */
export function savedValue(field) {
  if (field.type === 'secret') return '';
  const value = field.value ?? '';
  if (value === '' && field.source === 'default' && field.default !== null && field.default !== undefined) return String(field.default);
  return String(value);
}

/** Compare two values the way the setting reads them. */
export function sameValue(field, a, b) {
  if (a === null || b === null) return a === b;
  if (field.type === 'switch') return TRUE.test(a) === TRUE.test(b);
  if (field.type === 'multi' || field.type === 'list') return splitList(a).join(',') === splitList(b).join(',');
  return String(a).trim() === String(b).trim();
}

export function sourceBadge(source) {
  const [text, tone, title] = SOURCE[source] || SOURCE.default;
  return h('span', { class: ['badge', 'source', tone], title }, text);
}

let uid = 0;
export const nextId = (key) => `set-${key.toLowerCase()}-${(uid += 1)}`;

/** Random hex text for WEBHOOK_SECRET. */
export function randomSecret(bytes = 16) {
  const values = crypto.getRandomValues(new Uint8Array(bytes));
  return [...values].map((b) => b.toString(16).padStart(2, '0')).join('');
}

/**
 * The control for a field. value: the value to show; onChange(text). Returns {el, focusId}.
 * options: {labelId, generate} where generate adds a Generate button to a secret.
 */
export function control(field, value, onChange, { id, labelId, generate = false } = {}) {
  if (field.readonly) return readonlyControl(field, value, id);
  switch (field.type) {
    case 'switch': return switchControl(value, onChange, id);
    case 'menu': return menuControl(field, value, onChange, id, labelId);
    case 'multi': return multiControl(field, value, onChange, labelId);
    case 'list': return listControl(field, value, onChange, id);
    case 'secret': return secretControl(field, value, onChange, id, generate);
    case 'number': return inputControl(field, value, onChange, id, { type: 'number', inputmode: 'numeric', class: 'input narrow', min: field.min ?? null, max: field.max ?? null });
    case 'time': return inputControl(field, value, onChange, id, { type: 'time', step: 60, class: 'input narrow' });
    default: return inputControl(field, value, onChange, id, { type: 'text' });
  }
}

function readonlyControl(field, value, id) {
  const text = field.type === 'switch' ? (TRUE.test(value) ? 'On' : 'Off') : value || '—';
  return h('output', { class: 'readonly-value', id, title: 'Set in the compose file' }, icon('lock'), text);
}

function switchControl(value, onChange, id) {
  const input = h('input', { type: 'checkbox', role: 'switch', id, checked: TRUE.test(value) });
  input.addEventListener('change', () => onChange(input.checked ? 'true' : 'false'));
  return h('label', { class: 'switch' }, input, h('span'));
}

function menuControl(field, value, onChange, id, labelId) {
  const options = (field.options || []).map(([v, label, hint]) => [String(v), String(label ?? v), hint || '']);
  const short = options.length <= 4 && options.reduce((n, [, label]) => n + label.length, 0) <= 30;
  if (short && options.some(([v]) => v === value)) {
    const group = h('div', { class: 'segmented', role: 'radiogroup', 'aria-labelledby': labelId });
    const buttons = options.map(([v, label, hint]) => h('button', {
      type: 'button', role: 'radio', 'aria-checked': String(v === value), class: v === value ? 'active' : '', title: hint || null,
      tabindex: v === value ? '0' : '-1',
      onClick: () => pick(v),
    }, label));
    const pick = (v) => {
      buttons.forEach((b, i) => {
        const on = options[i][0] === v;
        b.classList.toggle('active', on);
        b.setAttribute('aria-checked', String(on));
        b.tabIndex = on ? 0 : -1;
      });
      onChange(v);
    };
    group.addEventListener('keydown', (event) => {
      if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) return;
      event.preventDefault();
      const current = buttons.findIndex((b) => b.classList.contains('active'));
      const step = event.key === 'ArrowLeft' || event.key === 'ArrowUp' ? -1 : 1;
      const next = (current + step + buttons.length) % buttons.length;
      pick(options[next][0]);
      buttons[next].focus();
    });
    group.append(...buttons);
    return group;
  }
  const select = h('select', { class: 'input', id },
    options.map(([v, label, hint]) => h('option', { value: v, selected: v === value }, hint ? `${label} (${hint})` : label)));
  if (!options.some(([v]) => v === value)) select.prepend(h('option', { value, selected: true }, value || '(empty)'));
  select.addEventListener('change', () => onChange(select.value));
  return select;
}

function multiControl(field, value, onChange, labelId) {
  const chosen = new Set(splitList(value));
  const known = (field.options || []).map(([v]) => String(v));
  const emit = () => onChange([...known.filter((v) => chosen.has(v)), ...[...chosen].filter((v) => !known.includes(v))].join(','));
  return h('div', { class: 'chips', role: 'group', 'aria-labelledby': labelId },
    (field.options || []).map(([v, label, hint]) => {
      const key = String(v);
      const chip = h('button', {
        type: 'button', class: ['chip', chosen.has(key) && 'active'], 'aria-pressed': String(chosen.has(key)), title: hint || null,
        onClick: () => {
          if (chosen.has(key) && chosen.size <= (field.min || 0)) {
            toast(`Choose at least ${field.min}.`, 'info', 2500);
            return;
          }
          if (chosen.has(key)) chosen.delete(key); else chosen.add(key);
          chip.classList.toggle('active', chosen.has(key));
          chip.setAttribute('aria-pressed', String(chosen.has(key)));
          chip.querySelector('.icon')?.remove();
          if (chosen.has(key)) chip.prepend(icon('check'));
          emit();
        },
      }, chosen.has(key) ? icon('check') : null, label ?? key);
      return chip;
    }));
}

function listControl(field, value, onChange, id) {
  let items = splitList(value);
  const box = h('div', { class: 'list-edit' });
  const input = h('input', { class: 'list-input', id, type: 'text', placeholder: field.placeholder || 'Add and press Enter', autocomplete: 'off' });
  const paint = () => {
    input.placeholder = items.length ? '' : field.placeholder || 'Add and press Enter';
    box.querySelectorAll('.chip').forEach((c) => c.remove());
    items.forEach((item, i) => input.before(h('span', { class: 'chip active' }, item,
      h('button', { type: 'button', class: 'chip-x', 'aria-label': `Remove ${item}`, onClick: () => { items.splice(i, 1); paint(); emit(); input.focus(); } }, icon('x')))));
  };
  const emit = () => onChange(items.join(','));
  const add = () => {
    const parts = splitList(input.value);
    if (!parts.length) return false;
    items = [...items, ...parts.filter((p) => !items.includes(p))];
    input.value = '';
    paint();
    emit();
    return true;
  };
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ',') {
      if (input.value.trim()) event.preventDefault();
      add();
    } else if (event.key === 'Backspace' && !input.value && items.length) {
      items.pop();
      paint();
      emit();
    }
  });
  input.addEventListener('blur', add);
  box.addEventListener('click', (event) => { if (event.target === box) input.focus(); });
  box.append(input);
  paint();
  return box;
}

function inputControl(field, value, onChange, id, attrs) {
  const input = h('input', { class: 'input', id, value: value ?? '', placeholder: field.placeholder || null, autocomplete: 'off', spellcheck: 'false', ...attrs });
  input.addEventListener('input', () => onChange(input.value));
  return input;
}

const generated = new Set(); // generated secrets stay visible when the form is drawn again

function secretControl(field, value, onChange, id, generate) {
  const status = h('span', { class: ['badge', field.is_set ? 'green' : ''] }, field.is_set ? 'Set' : 'Not set');
  const input = h('input', { class: 'input', id, type: 'password', autocomplete: 'new-password', spellcheck: 'false',
    placeholder: field.is_set ? 'New value' : field.placeholder || 'Value' });
  input.addEventListener('input', () => onChange(input.value));
  const copy = button({ icon: 'copy', kind: 'ghost', small: true, title: 'Copy', onClick: () => copyText(input.value) });
  copy.classList.add('hidden');
  const editor = h('div', { class: 'secret-edit hidden' }, input,
    generate && button({ label: 'Generate', icon: 'sparkles', small: true, onClick: () => {
      input.value = randomSecret();
      generated.add(input.value);
      input.type = 'text';
      copy.classList.remove('hidden');
      onChange(input.value);
    } }),
    copy,
    button({ label: 'Cancel', kind: 'ghost', small: true, onClick: () => {
      input.value = '';
      input.type = 'password';
      copy.classList.add('hidden');
      onChange('');
      editor.classList.add('hidden');
      summary.classList.remove('hidden');
      change.focus();
    } }));
  const change = button({ label: field.is_set ? 'Change' : 'Set', small: true, onClick: () => {
    summary.classList.add('hidden');
    editor.classList.remove('hidden');
    input.focus();
  } });
  const summary = h('div', { class: 'row' }, status, change);
  if (value) {
    input.value = value;
    if (generated.has(value)) {
      input.type = 'text';
      copy.classList.remove('hidden');
    }
    summary.classList.add('hidden');
    editor.classList.remove('hidden');
  }
  return h('div', { class: 'secret' }, summary, editor);
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** Restart the service, wait for it to answer again, then reload. host: where the overlay goes. */
export async function restartAndReload(host, text = 'Restarting Posteryard') {
  const overlay = h('div', { class: 'restart-overlay', role: 'alertdialog', 'aria-modal': 'true', 'aria-label': text },
    h('div', { class: 'restart-box' }, h('span', { class: 'spinner l' }), h('h2', {}, `${text}…`),
      h('p', { class: 'muted' }, 'This page reloads when it is back.')));
  host.append(overlay);
  try {
    await api.post('/api/actions/restart');
  } catch (err) {
    overlay.remove();
    throw err;
  }
  let down = false;
  const started = Date.now();
  while (Date.now() - started < 120000) {
    await sleep(1200);
    try {
      await api.get('/api/session');
      if (down || Date.now() - started > 8000) {
        location.reload();
        return;
      }
    } catch {
      down = true;
    }
  }
  overlay.remove();
  throw new Error('Posteryard did not come back. Check its log.');
}
