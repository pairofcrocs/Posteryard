// Page: settings, and setup mode when the settings do not load. See docs/webui-api.md "Settings" and "Setup mode".
import { api } from '../api.js';
import { h, mount, icon, button, toast, spinner, errorState, confirmDialog } from '../ui.js';
import {
  SERVERS, SERVER_KEYS, TESTS, richText, savedValue, sameValue, sourceBadge, nextId, control, restartAndReload,
} from './settings-fields.js';
import { passwordStep, setupHero, ESSENTIAL_KEYS } from './settings-setup.js';

export default async function render(view, ctx) {
  const root = h('div', { class: 'page-settings' });
  mount(view, root);
  if (ctx.session.setup && !ctx.session.password_set && !ctx.session.authenticated) {
    passwordStep(root, ctx);
    return undefined;
  }
  const page = new SettingsPage(root, ctx);
  await page.load();
  return () => page.destroy();
}

const slug = (name) => `group-${String(name).toLowerCase().replace(/[^a-z0-9]+/g, '-')}`;

class SettingsPage {
  constructor(root, ctx) {
    this.root = root;
    this.ctx = ctx;
    this.setup = Boolean(ctx.session.setup);
    this.data = null;
    this.fields = new Map();
    this.changes = new Map();
    this.rows = new Map();
    this.server = 'plex';
    this.query = (ctx.query.get('q') || '').trim();
    this.showMore = !this.setup;
    this.saveError = '';
    this.running = ctx.status;
    this.observer = null;
    this.onBeforeUnload = (event) => {
      if (!this.changes.size) return;
      event.preventDefault();
      event.returnValue = '';
    };
    this.onLinkClick = (event) => this.guardLink(event);
    window.addEventListener('beforeunload', this.onBeforeUnload);
    document.addEventListener('click', this.onLinkClick, true);
    this.unsubscribe = ctx.onStatus((status) => {
      this.running = status;
      this.paintRestart();
      this.paintMode();
    });
  }

  destroy() {
    window.removeEventListener('beforeunload', this.onBeforeUnload);
    document.removeEventListener('click', this.onLinkClick, true);
    this.unsubscribe();
    if (this.observer) this.observer.disconnect();
  }

  // Ask before an in-app link leaves the page with unsaved changes.
  async guardLink(event) {
    const link = event.target.closest('a[data-link]');
    if (!link || !this.changes.size || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey) return;
    const href = link.getAttribute('href');
    if (!href || href.startsWith('/settings')) return;
    event.preventDefault();
    event.stopPropagation();
    const leave = await confirmDialog({
      title: 'Discard changes?',
      message: `You have ${this.countText()} that are not saved.`,
      confirmLabel: 'Discard', danger: true,
    });
    if (leave) {
      this.changes.clear();
      this.ctx.navigate(href);
    }
  }

  async load() {
    mount(this.root, spinner('Loading settings'));
    try {
      this.data = await api.get('/api/settings');
    } catch (err) {
      mount(this.root, errorState(err, () => this.load()));
      return;
    }
    this.index();
    this.server = this.detectServer();
    this.build();
  }

  index() {
    this.fields.clear();
    for (const group of this.data.groups || []) for (const field of group.fields || []) this.fields.set(field.key, field);
  }

  detectServer() {
    const set = SERVERS.find((s) => s.keys.some((key) => {
      const field = this.fields.get(key);
      return field && (field.type === 'secret' ? field.is_set : savedValue(field));
    }));
    return set ? set.id : 'plex';
  }

  // ---- Values ----

  current(key) {
    const field = this.fields.get(key);
    if (this.changes.has(key)) return this.changes.get(key);
    return field ? savedValue(field) : '';
  }

  setChange(key, value) {
    const field = this.fields.get(key);
    if (!field) return;
    const unchanged = field.type === 'secret' ? value === '' : value !== null && sameValue(field, value, savedValue(field));
    if (unchanged) this.changes.delete(key); else this.changes.set(key, value);
    this.saveError = '';
    this.paintRow(key);
    this.paintBar();
  }

  countText() {
    const n = this.changes.size;
    return `${n} change${n === 1 ? '' : 's'}`;
  }

  // ---- Layout ----

  build() {
    const scroll = window.scrollY;
    if (this.observer) this.observer.disconnect();
    this.rows.clear();
    this.restartSlot = h('div', { class: 'restart-slot' });
    this.modeSlot = h('div');
    this.bar = h('div', { class: 'save-bar hidden', role: 'region', 'aria-label': 'Unsaved changes' });
    const parts = [];
    if (this.setup) parts.push(setupHero(this.ctx.session, this.data));
    parts.push(this.restartSlot);
    if (this.setup) {
      parts.push(this.essentials());
      parts.push(h('div', { class: 'more-toggle' }, button({
        label: this.showMore ? 'Fewer settings' : 'More settings', icon: this.showMore ? 'chevronDown' : 'chevronRight', kind: 'ghost',
        onClick: () => { this.showMore = !this.showMore; this.build(); },
      }), h('span', { class: 'small muted' }, 'Badges, labels, art, alerts and more. Every one has a default.')));
    } else {
      parts.push(this.modeSlot);
    }
    if (this.showMore) parts.push(this.groupsLayout());
    parts.push(this.bar);
    mount(this.root, parts);
    this.paintRestart();
    this.paintMode();
    this.paintBar();
    for (const key of this.rows.keys()) this.paintRow(key);
    this.applyServer();
    if (this.query) this.filter(this.query);
    this.watchGroups();
    window.scrollTo(0, scroll);
  }

  groupsLayout() {
    const skip = new Set(this.setup ? ESSENTIAL_KEYS : ['DRY_RUN']);
    const groups = (this.data.groups || []).map((group) => ({ ...group, fields: (group.fields || []).filter((f) => !skip.has(f.key)) }))
      .filter((group) => group.fields.length);
    this.indexLinks = new Map();
    const search = h('input', { class: 'input', type: 'search', placeholder: 'Search settings', 'aria-label': 'Search settings', value: this.query });
    search.addEventListener('input', () => this.filter(search.value));
    const nav = h('nav', { class: 'group-index', 'aria-label': 'Setting groups' }, groups.map((group) => {
      const link = h('a', { href: `#${slug(group.name)}`, onClick: (event) => {
        event.preventDefault();
        document.getElementById(slug(group.name))?.scrollIntoView({ behavior: 'smooth', block: 'start' });
      } }, group.name);
      this.indexLinks.set(slug(group.name), link);
      return link;
    }));
    this.noMatch = h('div', { class: 'empty hidden' }, icon('search', 'xl'), h('h3', {}, 'No settings match'),
      h('p', {}, 'Try another word, or a setting name such as DRY_RUN.'));
    this.groupEls = groups.map((group) => this.groupSection(group));
    return h('div', { class: 'settings-layout' },
      h('aside', { class: 'settings-aside' }, h('div', { class: 'search-wrap' }, icon('search'), search), nav),
      h('div', { class: 'settings-groups' }, this.groupEls, this.noMatch));
  }

  groupSection(group, { title = group.name, intro = null, id = slug(group.name) } = {}) {
    const keys = group.fields.map((f) => f.key);
    const tests = TESTS.filter((t) => t.keys.some((key) => keys.includes(key)));
    const result = h('div', { class: 'test-result hidden', role: 'status' });
    const rows = [];
    if (keys.some((key) => SERVER_KEYS.includes(key))) rows.push(this.serverChooser());
    for (const field of group.fields) rows.push(this.row(field));
    return h('section', { class: 'settings-group', id, 'aria-labelledby': `${id}-title` },
      h('div', { class: 'group-head' }, h('h2', { id: `${id}-title` }, title),
        tests.map((test) => button({ label: test.label, icon: 'check', small: true, onClick: () => this.runTest(test, result) }))),
      intro && h('p', { class: 'group-intro muted' }, intro),
      result,
      h('div', { class: 'settings-card' }, rows));
  }

  essentials() {
    const pick = (keys) => keys.map((key) => this.fields.get(key)).filter(Boolean);
    const sections = [
      { name: 'Media server', id: 'setup-server', intro: 'The server whose posters Posteryard looks after, and the libraries to manage.',
        fields: pick([...SERVER_KEYS, 'LIBRARIES']) },
      { name: 'TMDB', id: 'setup-tmdb', intro: 'Posteryard finds clean art on TMDB. A free key is under Settings, API on themoviedb.org.',
        fields: pick(['TMDB_API_KEY']) },
      { name: 'Webhook secret', id: 'setup-webhook', intro: 'Any long random text. It is part of the webhook address your server calls for new titles.',
        fields: pick(['WEBHOOK_SECRET']) },
      { name: 'Preview first', id: 'setup-mode', intro: 'In preview mode Posteryard saves each image under Previews and changes nothing on your server.',
        fields: pick(['DRY_RUN']) },
    ].filter((s) => s.fields.length);
    return h('div', { class: 'essentials' }, sections.map((s, i) => this.groupSection(s, {
      title: h('span', { class: 'step-title' }, h('span', { class: 'step' }, String(i + 1)), s.name), intro: s.intro, id: s.id,
    })));
  }

  serverChooser() {
    const labelId = nextId('server');
    const group = h('div', { class: 'segmented', role: 'radiogroup', 'aria-labelledby': labelId });
    for (const server of SERVERS) {
      group.append(h('button', {
        type: 'button', role: 'radio', 'data-server': server.id,
        onClick: () => this.chooseServer(server.id),
      }, server.label));
    }
    this.chooser = group;
    return h('div', { class: 'setting-row', 'data-search': 'media server plex jellyfin emby', 'data-key': '' },
      h('div', { class: 'setting-text' },
        h('div', { class: 'setting-label', id: labelId }, 'Media server'),
        h('div', { class: 'setting-help' }, 'Posteryard works with one server. Jellyfin and Emby use tags for labels.')),
      h('div', { class: 'setting-control' }, group));
  }

  chooseServer(id) {
    this.server = id;
    // Clear the other servers' values, so only one is set.
    for (const server of SERVERS) {
      for (const key of server.keys) {
        const field = this.fields.get(key);
        if (!field) continue;
        if (server.id === id) {
          const pending = this.changes.get(key);
          if (pending === null || pending === '') {
            this.setChange(key, savedValue(field));
            this.rows.get(key)?.renderControl();
          }
        } else if (field.type === 'secret') {
          this.setChange(key, field.source === 'ui' && field.is_set ? null : '');
          this.rows.get(key)?.renderControl();
        } else {
          this.setChange(key, savedValue(field) ? '' : savedValue(field));
          this.rows.get(key)?.renderControl();
        }
      }
    }
    this.applyServer();
    this.rows.get(SERVERS.find((s) => s.id === id).keys[0])?.querySelector('input')?.focus();
  }

  applyServer() {
    if (this.chooser) {
      for (const b of this.chooser.querySelectorAll('button')) {
        const on = b.dataset.server === this.server;
        b.classList.toggle('active', on);
        b.setAttribute('aria-checked', String(on));
      }
    }
    for (const server of SERVERS) {
      for (const key of server.keys) this.rows.get(key)?.classList.toggle('off-server', server.id !== this.server);
    }
  }

  row(field) {
    const id = nextId(field.key);
    const labelId = `${id}-label`;
    const slot = h('div', { class: 'setting-control' });
    const note = h('div', { class: 'setting-note hidden' });
    const row = h('div', {
      class: 'setting-row', 'data-key': field.key,
      'data-search': `${field.key} ${field.label} ${field.help || ''}`.toLowerCase(),
    },
    h('div', { class: 'setting-text' },
      h('div', { class: 'setting-label', id: labelId },
        h('label', { for: id }, field.label || field.key), h('span', { class: 'dirty-dot', title: 'Changed, not saved' })),
      field.help && h('div', { class: 'setting-help' }, richText(field.help)),
      h('div', { class: 'setting-meta' }, h('code', {}, field.key), field.readonly ? h('span', { class: 'badge source' }, 'Compose file') : sourceBadge(field.source),
        field.source === 'ui' && h('button', { type: 'button', class: 'linklike reset', onClick: () => this.setChange(field.key, null) }, 'Use environment value'))),
    slot, note);
    row.renderControl = () => mount(slot, control(field, this.current(field.key) ?? '', (value) => this.setChange(field.key, value),
      { id, labelId, generate: field.key === 'WEBHOOK_SECRET' }));
    row.note = note;
    row.renderControl();
    this.rows.set(field.key, row);
    return row;
  }

  paintRow(key) {
    const row = this.rows.get(key);
    if (!row) return;
    const dirty = this.changes.has(key);
    const dropping = dirty && this.changes.get(key) === null;
    row.classList.toggle('dirty', dirty);
    row.classList.toggle('dropping', dropping);
    row.querySelector('.reset')?.classList.toggle('hidden', dropping);
    if (dropping) {
      mount(row.note, icon('undo'), h('span', { class: 'grow' }, 'Saving drops the web UI value, so the environment\'s value applies again.'),
        h('button', { type: 'button', class: 'linklike', onClick: () => { this.changes.delete(key); this.paintRow(key); this.paintBar(); row.renderControl(); } }, 'Undo'));
    }
    row.note.classList.toggle('hidden', !dropping);
  }

  filter(text) {
    this.query = text.trim();
    const q = this.query.toLowerCase();
    let any = false;
    for (const section of this.groupEls || []) {
      let visible = 0;
      for (const row of section.querySelectorAll('.setting-row')) {
        const match = !q || row.dataset.search.includes(q);
        row.classList.toggle('filtered', !match);
        if (match && !row.classList.contains('off-server')) visible += 1;
      }
      section.classList.toggle('hidden', !visible);
      this.indexLinks.get(section.id)?.classList.toggle('hidden', !visible);
      any = any || Boolean(visible);
    }
    this.noMatch?.classList.toggle('hidden', any);
  }

  watchGroups() {
    if (!this.groupEls?.length || !('IntersectionObserver' in window)) return;
    this.observer = new IntersectionObserver((entries) => {
      const top = entries.filter((e) => e.isIntersecting).sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)[0];
      if (!top) return;
      for (const [id, link] of this.indexLinks) {
        link.classList.toggle('active', id === top.target.id);
        const nav = link.parentElement;
        if (id === top.target.id && nav.scrollWidth > nav.clientWidth) nav.scrollLeft = link.offsetLeft - nav.clientWidth / 2;
      }
    }, { rootMargin: '-170px 0px -55% 0px' });
    for (const el of this.groupEls) this.observer.observe(el);
  }

  // ---- Preview mode / live ----

  paintMode() {
    if (!this.modeSlot || this.setup) return;
    const field = this.fields.get('DRY_RUN');
    if (!field) return mount(this.modeSlot);
    const preview = !sameValue(field, savedValue(field), 'false');
    const running = this.running ? this.running.dry_run : preview;
    const pending = running !== preview;
    mount(this.modeSlot, h('section', { class: ['mode-card', preview ? 'preview' : 'live'] },
      h('div', { class: 'mode-icon' }, icon(preview ? 'eye' : 'upload', 'l')),
      h('div', { class: 'grow' },
        h('div', { class: 'row wrap' }, h('h2', {}, preview ? 'Preview mode' : 'Live'),
          pending && h('span', { class: 'badge orange' }, 'Restart to apply'), sourceBadge(field.source), h('code', { class: 'faint' }, 'DRY_RUN')),
        h('p', {}, preview
          ? 'Posteryard saves each image under Previews and changes nothing on your server. Check a few, then go live.'
          : 'Posteryard uploads each image to your server and locks it, so a server refresh keeps it.')),
      h('div', { class: 'mode-actions' }, preview
        ? [h('a', { class: 'btn', href: '/previews', 'data-link': '' }, 'Open previews'),
          button({ label: 'Go live', icon: 'play', kind: 'primary', onClick: () => this.goLive(true) })]
        : button({ label: 'Back to preview mode', icon: 'eye', onClick: () => this.goLive(false) }))));
    return undefined;
  }

  async goLive(live) {
    const ok = await confirmDialog(live ? {
      title: 'Go live?',
      message: h('div', { class: 'col' },
        h('p', {}, 'Posteryard will upload its images to your server and lock them, so a server refresh keeps them. Your current posters are replaced.'),
        h('p', { class: 'muted' }, 'Images you changed by hand stay as they are. To undo later, use Restore every image under Tools.')),
      confirmLabel: 'Go live',
    } : {
      title: 'Back to preview mode?',
      message: 'Posteryard stops uploading and saves new images under Previews. Images already uploaded stay on your server.',
      confirmLabel: 'Back to preview mode',
    });
    if (!ok) return;
    await this.save({ DRY_RUN: live ? 'false' : 'true' });
  }

  // ---- Tests, save, restart ----

  async runTest(test, slot) {
    const values = Object.fromEntries([...this.changes].filter(([, v]) => v !== null));
    mount(slot, h('span', { class: 'spinner' }), 'Testing…');
    slot.className = 'test-result';
    try {
      const result = await api.post('/api/settings/test', { what: test.what, values });
      slot.className = `test-result ${result.ok ? 'ok' : 'fail'}`;
      mount(slot, icon(result.ok ? 'check' : 'alert'), h('span', { class: 'grow' }, result.message || (result.ok ? 'It works.' : 'It did not work.')));
    } catch (err) {
      slot.className = 'test-result fail';
      mount(slot, icon('alert'), h('span', { class: 'grow' }, err.message));
    }
  }

  paintBar() {
    if (!this.bar) return;
    const n = this.changes.size;
    this.bar.classList.toggle('hidden', !n && !this.saveError);
    this.root.classList.toggle('has-bar', Boolean(n || this.saveError));
    mount(this.bar, h('div', { class: 'save-inner' },
      h('div', { class: 'grow' },
        h('div', { class: 'save-count' }, n ? this.countText() : 'Not saved'),
        this.saveError && h('div', { class: 'save-error', role: 'alert' }, icon('alert'), this.saveError)),
      button({ label: 'Discard', kind: 'ghost', onClick: () => { this.changes.clear(); this.saveError = ''; this.server = this.detectServer(); this.build(); } }),
      button({ label: 'Save', kind: 'primary', disabled: !n, onClick: () => this.save() })));
  }

  async save(values = null) {
    const body = values || Object.fromEntries(this.changes);
    try {
      this.data = await api.put('/api/settings', { values: body });
    } catch (err) {
      this.saveError = err.message;
      if (values) toast(err.message, 'error'); else this.paintBar();
      return;
    }
    this.saveError = '';
    this.index();
    // Keep unsaved changes that still differ from the new saved values.
    for (const key of [...this.changes.keys()]) {
      const field = this.fields.get(key);
      if (key in body || !field) this.changes.delete(key);
    }
    toast('Settings saved', 'success');
    this.build();
    this.ctx.refreshStatus();
    if (this.setup && !this.data.error) this.restartSlot.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  paintRestart() {
    if (!this.restartSlot || !this.data) return;
    if (this.setup) {
      // While the settings still have a problem, the hero says what is missing.
      if (this.data.error) return mount(this.restartSlot);
      mount(this.restartSlot, h('div', { class: 'restart-card ready' }, icon('check', 'l'),
        h('div', { class: 'grow' }, h('h3', {}, 'Ready to start'), h('p', {}, 'The settings are complete. Restart to start Posteryard with them.')),
        button({ label: 'Restart and start Posteryard', icon: 'play', kind: 'primary', onClick: () => restartAndReload(this.root, 'Starting Posteryard') })));
      return undefined;
    }
    if (!this.data.restart_required || this.running?.restart_required) return mount(this.restartSlot);
    mount(this.restartSlot, h('div', { class: 'banner accent' }, icon('refresh'),
      h('span', { class: 'grow' }, 'Settings were saved. Restart Posteryard to use them.'),
      button({ label: 'Restart now', kind: 'primary', small: true, onClick: () => restartAndReload(this.root) })));
    return undefined;
  }
}
