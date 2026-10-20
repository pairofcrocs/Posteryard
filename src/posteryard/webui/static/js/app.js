// The app shell: sign-in, navigation, the status bar and the router. Pages live in ./pages/*.js and export
// `default async function render(view, ctx)`, which may return a cleanup function. ctx is described in ROUTES below.

import { api, imageUrl } from './api.js';
import { h, mount, icon, button, toast, spinner, errorState, debounce, KIND_LABEL } from './ui.js';

const NAV = [
  { path: '/', label: 'Dashboard', icon: 'home' },
  { path: '/library', label: 'Library', icon: 'grid' },
  { path: '/previews', label: 'Previews', icon: 'image' },
  { path: '/activity', label: 'Activity', icon: 'activity' },
  { path: '/problems', label: 'Problems', icon: 'alert', badge: 'problems' },
  { path: '/missing', label: 'Missing', icon: 'layers' },
  { section: 'More' },
  { path: '/live-tv', label: 'Live TV', icon: 'tv' },
  { path: '/tools', label: 'Tools', icon: 'tool' },
  { path: '/settings', label: 'Settings', icon: 'gear' },
];

// Path pattern -> page module. `:name` becomes ctx.params.name.
const ROUTES = [
  ['/', 'dashboard', 'Dashboard'],
  ['/library', 'library', 'Library'],
  ['/item/:key', 'item', 'Title'],
  ['/previews', 'previews', 'Previews'],
  ['/activity', 'activity', 'Activity'],
  ['/problems', 'problems', 'Problems'],
  ['/missing', 'missing', 'Missing'],
  ['/live-tv', 'livetv', 'Live TV'],
  ['/tools', 'tools', 'Tools'],
  ['/settings', 'settings', 'Settings'],
];

const state = {
  session: null,
  status: null,
  statusListeners: new Set(),
  cleanup: null,
  renderId: 0,
};

function compile(pattern) {
  const names = [];
  const regex = new RegExp(`^${pattern.replace(/:(\w+)/g, (_, name) => { names.push(name); return '([^/]+)'; })}/?$`);
  return { regex, names };
}

const COMPILED = ROUTES.map(([pattern, page, title]) => ({ ...compile(pattern), page, title }));

export function navigate(path, { replace = false } = {}) {
  if (path === location.pathname + location.search) return;
  history[replace ? 'replaceState' : 'pushState']({}, '', path);
  render();
}

function onStatus(listener) {
  state.statusListeners.add(listener);
  if (state.status) listener(state.status);
  return () => state.statusListeners.delete(listener);
}

async function refreshStatus() {
  if (state.session?.setup || !state.session?.authenticated) return;
  try {
    state.status = await api.get('/api/status');
  } catch {
    return;
  }
  for (const listener of state.statusListeners) listener(state.status);
  paintChrome();
}

// ---- Shell ----

let shell = null;

function buildShell() {
  const nav = h('nav', { class: 'nav', 'aria-label': 'Main' });
  for (const entry of NAV) {
    if (entry.section) {
      nav.append(h('div', { class: 'nav-section' }, entry.section));
      continue;
    }
    if (state.session.setup && entry.path !== '/settings') continue;
    nav.append(h('a', { href: entry.path, 'data-link': '', 'data-path': entry.path },
      icon(entry.icon), entry.label, entry.badge && h('span', { class: 'count hidden', 'data-badge': entry.badge })));
  }
  const theme = button({ icon: themeIcon(), kind: 'ghost', small: true, title: 'Light or dark', onClick: toggleTheme });
  const foot = h('div', { class: 'sidebar-foot' },
    h('div', { class: 'row between' },
      h('span', { class: 'small muted', 'data-slot': 'server' }, ''),
      h('div', { class: 'row gap-s' }, theme,
        state.session.authenticated ? button({ icon: 'logout', kind: 'ghost', small: true, title: 'Sign out', onClick: signOut }) : null)),
  );
  const sidebar = h('aside', { class: 'sidebar' },
    h('a', { class: 'brand', href: '/', 'data-link': '' }, h('img', { src: '/static/img/logo.svg', alt: '' }), 'Posteryard',
      h('span', { class: 'version' }, state.session.version ? `v${state.session.version}` : '')),
    nav, foot);
  const title = h('h1', { 'data-slot': 'title' }, '');
  const topbar = h('header', { class: 'topbar' },
    button({ icon: 'menu', kind: 'ghost', title: 'Menu', onClick: () => shell.classList.toggle('nav-open') }),
    title,
    state.session.setup ? null : searchBox(),
    h('span', { class: 'pill mode-pill', 'data-slot': 'mode' }, ''));
  topbar.firstChild.classList.add('menu-button');
  const view = h('div', { class: 'view', id: 'view' });
  shell = h('div', { class: 'shell' }, sidebar,
    h('main', { class: 'main' }, topbar, h('div', { class: 'banners', 'data-slot': 'banners' }), view));
  document.body.replaceChildren(shell);
  shell.addEventListener('click', (event) => { if (event.target.closest('.nav a')) shell.classList.remove('nav-open'); });
}

function paintChrome() {
  if (!shell) return;
  const s = state.status;
  for (const link of shell.querySelectorAll('.nav a')) {
    const path = link.dataset.path;
    link.classList.toggle('active', path === '/' ? location.pathname === '/' : location.pathname.startsWith(path)
      || (path === '/library' && location.pathname.startsWith('/item/')));
  }
  const mode = shell.querySelector('[data-slot="mode"]');
  const banners = [];
  if (state.session.setup) {
    mode.className = 'pill mode-pill bad';
    mount(mode, h('span', { class: 'dot' }), 'Setup');
  } else if (s) {
    const working = s.current ? h('span', { class: 'dot pulse' }) : h('span', { class: 'dot' });
    const [cls, text] = !s.ok ? ['bad', 'Unhealthy'] : s.paused ? ['paused', 'Paused'] : s.dry_run ? ['dry', 'Dry run'] : ['live', 'Live'];
    mode.className = `pill mode-pill ${cls}`;
    mode.title = s.current ? `Working on ${s.current.title || s.current.label || s.current.key}` : 'Idle';
    mount(mode, working, text);
    mount(shell.querySelector('[data-slot="server"]'), s.server ? `${s.server.name}` : '');
    const problems = (s.images?.failed || 0);
    const badge = shell.querySelector('[data-badge="problems"]');
    if (badge) {
      badge.textContent = String(problems);
      badge.classList.toggle('hidden', !problems);
    }
    if (s.restart_required) {
      banners.push(h('div', { class: 'banner accent' }, icon('refresh'), h('span', { class: 'grow' }, 'Settings were saved. Restart Posteryard to use them.'),
        button({ label: 'Restart now', kind: 'primary', small: true, onClick: restart })));
    }
    if (s.paused) {
      banners.push(h('div', { class: 'banner warn' }, icon('pause'), h('span', { class: 'grow' }, 'Paused: no sweeps, full passes or library items run until you resume.'),
        button({ label: 'Resume', icon: 'play', small: true, onClick: async () => { await api.post('/api/actions/resume'); await refreshStatus(); } })));
    }
  }
  mount(shell.querySelector('[data-slot="banners"]'), banners);
}

async function restart() {
  await api.post('/api/actions/restart');
  toast('Restarting…', 'info');
  for (let i = 0; i < 60; i += 1) {
    await new Promise((r) => setTimeout(r, 1500));
    try {
      await api.get('/api/session');
      location.reload();
      return;
    } catch { /* still restarting */ }
  }
  toast('Posteryard did not come back. Check its log.', 'error');
}

function searchBox() {
  const results = h('div', { class: 'search-results hidden', role: 'listbox' });
  const input = h('input', { class: 'input', type: 'search', placeholder: 'Search titles  ( / )', 'aria-label': 'Search titles', autocomplete: 'off' });
  let active = -1;
  const close = () => { results.classList.add('hidden'); active = -1; };
  const run = debounce(async () => {
    const q = input.value.trim();
    if (!q) return close();
    try {
      const data = await api.get(`/api/search?q=${encodeURIComponent(q)}`);
      const items = data.items || [];
      mount(results, items.length ? items.map((item) => h('a', { href: `/item/${item.key}`, 'data-link': '', role: 'option' },
        h('img', { src: imageUrl(item.key, { w: 80 }), alt: '', loading: 'lazy', onError: (e) => e.target.replaceWith(h('div', { class: 'ph' })) }),
        h('div', { class: 'grow' }, h('div', { class: 'truncate' }, item.title),
          h('div', { class: 'small muted' }, [KIND_LABEL[item.type] || item.type, item.year].filter(Boolean).join(' · ')))))
        : h('div', { class: 'small muted', style: { padding: '8px' } }, 'No titles match.'));
      results.classList.remove('hidden');
      active = -1;
    } catch (err) {
      mount(results, h('div', { class: 'small muted', style: { padding: '8px' } }, err.message));
      results.classList.remove('hidden');
    }
  }, 200);
  input.addEventListener('input', run);
  input.addEventListener('keydown', (event) => {
    const links = [...results.querySelectorAll('a')];
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      active = Math.max(0, Math.min(links.length - 1, active + (event.key === 'ArrowDown' ? 1 : -1)));
      links.forEach((link, i) => link.classList.toggle('active', i === active));
    } else if (event.key === 'Enter' && links.length) {
      navigate(links[Math.max(0, active)].getAttribute('href'));
      input.value = '';
      close();
      input.blur();
    } else if (event.key === 'Escape') {
      close();
      input.blur();
    }
  });
  input.addEventListener('blur', () => setTimeout(close, 150));
  results.addEventListener('click', () => { input.value = ''; close(); });
  document.addEventListener('keydown', (event) => {
    if (event.key === '/' && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName)) {
      event.preventDefault();
      input.focus();
    }
  });
  return h('div', { class: 'search' }, icon('search'), input, results);
}

function themeIcon() {
  return document.documentElement.dataset.theme === 'light' ? 'moon' : 'sun';
}

function applyTheme() {
  let theme = null;
  try { theme = localStorage.getItem('posteryard-theme'); } catch { /* storage off */ }
  if (theme) document.documentElement.dataset.theme = theme;
}

function toggleTheme() {
  const next = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem('posteryard-theme', next); } catch { /* storage off */ }
  buildShell();
  render();
}

async function signOut() {
  await api.post('/api/logout');
  state.session = await api.get('/api/session');
  shell = null;
  navigate('/login', { replace: true });
}

// ---- Sign in ----

function renderLogin() {
  shell = null;
  const password = h('input', { class: 'input', type: 'password', autocomplete: 'current-password', placeholder: 'Password', required: true });
  const message = h('div', { class: 'small', style: { color: 'var(--red)', minHeight: '18px' } });
  const submit = button({ label: 'Sign in', kind: 'primary', type: 'submit' });
  const form = h('form', { class: 'card auth-card' },
    h('div', { class: 'brand' }, h('img', { src: '/static/img/logo.svg', alt: '' }), 'Posteryard'),
    state.session.password_set
      ? h('p', { class: 'muted small', style: { textAlign: 'center' } }, 'Sign in with UI_PASSWORD, or WEBHOOK_SECRET when UI_PASSWORD is not set.')
      : h('p', { class: 'muted small', style: { textAlign: 'center' } }, 'No password is set. Set UI_PASSWORD or WEBHOOK_SECRET and restart Posteryard.'),
    password, message, submit);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    submit.classList.add('busy');
    try {
      await api.post('/api/login', { password: password.value });
      state.session = await api.get('/api/session');
      const next = new URLSearchParams(location.search).get('next') || '/';
      history.replaceState({}, '', next.startsWith('/') && !next.startsWith('//') ? next : '/');
      start();
    } catch (err) {
      message.textContent = err.message;
      password.select();
    } finally {
      submit.classList.remove('busy');
    }
  });
  document.body.replaceChildren(h('div', { class: 'auth-page' }, form));
  password.focus();
}

// ---- Router ----

async function render() {
  const renderId = ++state.renderId;
  if (state.cleanup) {
    try { state.cleanup(); } catch { /* a page's cleanup must not stop navigation */ }
    state.cleanup = null;
  }
  const needsLogin = !state.session.authenticated && !(state.session.setup && !state.session.password_set);
  if (needsLogin) {
    if (location.pathname !== '/login') history.replaceState({}, '', `/login?next=${encodeURIComponent(location.pathname + location.search)}`);
    renderLogin();
    return;
  }
  if (location.pathname === '/login') history.replaceState({}, '', '/');
  if (state.session.setup && location.pathname !== '/settings') history.replaceState({}, '', '/settings');
  if (!shell) buildShell();
  const view = shell.querySelector('#view');
  const route = COMPILED.find((r) => r.regex.test(location.pathname));
  const titleSlot = shell.querySelector('[data-slot="title"]');
  const setTitle = (text) => {
    titleSlot.textContent = text;
    document.title = text ? `${text} · Posteryard` : 'Posteryard';
  };
  paintChrome();
  window.scrollTo(0, 0);
  if (!route) {
    setTitle('Not found');
    mount(view, errorState(new Error('There is no page here.'), null));
    return;
  }
  const match = route.regex.exec(location.pathname);
  const params = Object.fromEntries(route.names.map((name, i) => [name, decodeURIComponent(match[i + 1])]));
  setTitle(route.title);
  mount(view, spinner());
  try {
    const module = await import(`./pages/${route.page}.js`);
    if (renderId !== state.renderId) return;
    const ctx = {
      params,
      query: new URLSearchParams(location.search),
      navigate,
      setTitle,
      /** Replace the query string without reloading the page, e.g. for filters. */
      setQuery: (query) => history.replaceState({}, '', `${location.pathname}${query.toString() ? `?${query}` : ''}`),
      status: state.status,
      session: state.session,
      onStatus,
      refreshStatus,
      isCurrent: () => renderId === state.renderId,
    };
    mount(view);
    const cleanup = await module.default(view, ctx);
    if (renderId === state.renderId && typeof cleanup === 'function') state.cleanup = cleanup;
    else if (typeof cleanup === 'function') cleanup();
  } catch (err) {
    if (renderId !== state.renderId) return;
    console.error(err);
    mount(view, errorState(err, () => render()));
  }
}

document.addEventListener('click', (event) => {
  const link = event.target.closest('a[data-link]');
  if (!link || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || link.target) return;
  const href = link.getAttribute('href');
  if (!href || !href.startsWith('/')) return;
  event.preventDefault();
  navigate(href);
});
window.addEventListener('popstate', () => render());
window.addEventListener('posteryard:unauthorized', () => {
  if (!state.session) return;
  state.session = { ...state.session, authenticated: false };
  render();
});

let statusTimer = null;

async function start() {
  if (!statusTimer) {
    statusTimer = setInterval(() => { if (!document.hidden) refreshStatus(); }, 8000);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshStatus(); });
  }
  shell = null;
  await refreshStatus();
  render();
}

applyTheme();
api.get('/api/session').then((session) => {
  state.session = session;
  start();
}).catch((err) => {
  document.body.replaceChildren(h('div', { class: 'auth-page' }, errorState(err, () => location.reload())));
});
