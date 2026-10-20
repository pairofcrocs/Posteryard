// Page: tools. TMDB previews, run now, pause, test alert, webhook help and the danger zone. See docs/webui-api.md.
import { api } from '../api.js';
import {
  h, mount, icon, button, toast, confirmDialog, modal, runJob, segmented, tabs, formatDuration, copyText, TARGET_LABEL,
} from '../ui.js';

export default async function render(view, ctx) {
  const root = h('div', { class: 'page-tools' });
  const status = { value: ctx.status };
  const painters = [];
  const onStatus = (fn) => { painters.push(fn); if (status.value) fn(status.value); };
  mount(view, root);
  mount(root,
    h('div', { class: 'page-head' }, h('div', { class: 'grow' }, h('p', {}, 'Try a title, run work now and fix the odd problem.'))),
    tmdbCard(),
    h('div', { class: 'tools-grid' }, runCard(ctx, onStatus), pauseCard(ctx, onStatus), alertCard(ctx, onStatus)),
    webhookCard(onStatus),
    dangerCard(ctx, onStatus));
  const unsubscribe = ctx.onStatus((s) => {
    status.value = s;
    for (const paint of painters) paint(s);
  });
  return unsubscribe;
}

function toolCard({ iconName, title, intro, body, className = '' }) {
  return h('section', { class: ['card', 'tool-card', className] },
    h('div', { class: 'tool-head' }, h('div', { class: 'tool-icon' }, icon(iconName, 'l')),
      h('div', { class: 'grow' }, h('h2', {}, title), intro && h('p', { class: 'muted' }, intro))),
    h('div', { class: 'tool-body' }, body));
}

// ---- Preview any TMDB title ----

/** "603", "https://www.themoviedb.org/movie/603-the-matrix" or "tv/1399" -> {kind, id}. */
export function parseTmdb(text) {
  const value = text.trim();
  const url = /(?:^|\/)(movie|tv)\/(\d+)/i.exec(value);
  if (url) return { kind: url[1].toLowerCase(), id: Number(url[2]) };
  const prefixed = /^(movie|tv):(\d+)$/i.exec(value);
  if (prefixed) return { kind: prefixed[1].toLowerCase(), id: Number(prefixed[2]) };
  if (/^\d+$/.test(value)) return { kind: null, id: Number(value) };
  return null;
}

function tmdbCard() {
  let kind = 'movie';
  const kindControl = h('div', { class: 'kind-slot' });
  const paintKind = () => mount(kindControl, segmented([['movie', 'Movie'], ['tv', 'Show']], kind, (value) => { kind = value; seasonsField.classList.toggle('hidden', kind !== 'tv'); }));
  const idInput = h('input', { class: 'input', id: 'tmdb-id', placeholder: '603 or a themoviedb.org link', autocomplete: 'off', spellcheck: 'false' });
  const seasonsInput = h('input', { class: 'input', id: 'tmdb-seasons', placeholder: 'Such as 1, 2', inputmode: 'numeric', autocomplete: 'off' });
  const seasonsField = h('div', { class: 'field hidden' }, h('label', { for: 'tmdb-seasons' }, 'Seasons ', h('span', { class: 'muted' }, '(optional)')), seasonsInput);
  const hint = h('div', { class: 'help', id: 'tmdb-id-help' }, 'A TMDB id, or paste the title\'s address from themoviedb.org.');
  idInput.setAttribute('aria-describedby', 'tmdb-id-help');
  const output = h('div', { class: 'tmdb-output', 'aria-live': 'polite' });
  idInput.addEventListener('input', () => {
    const parsed = parseTmdb(idInput.value);
    idInput.classList.toggle('invalid', Boolean(idInput.value.trim()) && !parsed);
    if (parsed?.kind && parsed.kind !== kind) {
      kind = parsed.kind;
      paintKind();
      seasonsField.classList.toggle('hidden', kind !== 'tv');
    }
    hint.textContent = parsed?.kind ? `${parsed.kind === 'tv' ? 'Show' : 'Movie'} ${parsed.id}` : 'A TMDB id, or paste the title\'s address from themoviedb.org.';
  });
  const submit = button({ label: 'Render preview', icon: 'eye', kind: 'primary', type: 'submit' });
  const form = h('form', { class: 'tmdb-form' },
    h('div', { class: 'field' }, h('span', { class: 'field-label', id: 'tmdb-kind-label' }, 'Kind'), kindControl),
    h('div', { class: 'field grow' }, h('label', { for: 'tmdb-id' }, 'TMDB title'), idInput, hint),
    seasonsField,
    h('div', { class: 'field submit' }, submit));
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const parsed = parseTmdb(idInput.value);
    if (!parsed) {
      idInput.classList.add('invalid');
      idInput.focus();
      toast('Enter a TMDB id or a themoviedb.org link.', 'error');
      return;
    }
    const seasons = kind === 'tv' ? seasonsInput.value.split(/[\s,]+/).filter(Boolean).map(Number).filter((n) => Number.isInteger(n) && n >= 0) : [];
    submit.classList.add('busy');
    mount(output, h('div', { class: 'loading' }, h('span', { class: 'spinner' }), 'Rendering. Fetching art can take a few seconds…'));
    try {
      const result = await runJob('/api/tools/tmdb-preview', { kind, id: parsed.id, seasons }, `Preview ${kind === 'tv' ? 'show' : 'movie'} ${parsed.id}`, { quiet: true });
      if (!result) {
        mount(output, h('div', { class: 'test-result fail' }, icon('alert'), 'The preview failed. The message is in the corner.'));
        return;
      }
      showRenders(output, result.renders || []);
    } finally {
      submit.classList.remove('busy');
    }
  });
  paintKind();
  kindControl.firstChild.setAttribute('aria-labelledby', 'tmdb-kind-label');
  return toolCard({
    iconName: 'film', title: 'Preview any TMDB title', className: 'wide-card',
    intro: 'Render the images for a movie or show, even one not on your server. Nothing is saved or uploaded.',
    body: [form, output],
  });
}

function showRenders(output, renders) {
  if (!renders.length) {
    mount(output, h('p', { class: 'muted' }, 'Nothing to render for this title.'));
    return;
  }
  const open = (r) => modal({
    title: `${TARGET_LABEL[r.target] || r.target}: ${r.name || ''}`, wide: true,
    content: h('div', { class: 'page-tools render-full' }, h('img', { src: r.url, alt: r.name || r.target }),
      notesList(r.notes)),
  });
  const figure = (r) => h('figure', { class: 'render' },
    h('button', { type: 'button', class: 'render-art', 'aria-label': `Open ${r.name || r.target} large`, onClick: () => open(r) },
      h('img', { src: r.url, alt: `${TARGET_LABEL[r.target] || r.target} for ${r.name || 'the title'}`, loading: 'lazy' })),
    h('figcaption', {}, h('div', { class: 'row' },
      h('div', { class: 'grow' }, h('b', { class: 'truncate', title: r.name }, r.name || r.target),
        h('div', { class: 'small muted' }, TARGET_LABEL[r.target] || r.target)),
      h('a', { class: 'btn ghost small icon-only', href: r.url, target: '_blank', rel: 'noopener', 'aria-label': 'Open the image in a new tab', title: 'Open the image' }, icon('external'))),
    notesList(r.notes)));
  const posters = renders.filter((r) => r.target === 'poster');
  const wide = renders.filter((r) => r.target !== 'poster');
  mount(output,
    posters.length ? h('div', { class: 'renders' }, posters.map(figure)) : null,
    wide.length ? h('div', { class: 'renders wide' }, wide.map(figure)) : null);
}

function notesList(notes) {
  const list = Array.isArray(notes) ? notes : notes ? [notes] : [];
  return list.length ? h('ul', { class: 'notes' }, list.map((n) => h('li', {}, n))) : null;
}

// ---- Run now ----

function runCard(ctx, onStatus) {
  const facts = h('dl', { class: 'props small' });
  onStatus((s) => mount(facts,
    h('dt', {}, 'Last sweep'), h('dd', {}, s.last_sweep_seconds_ago == null ? 'not yet' : `${formatDuration(s.last_sweep_seconds_ago)} ago`),
    h('dt', {}, 'Next sweep'), h('dd', {}, s.paused ? 'paused' : s.next_sweep_seconds == null ? '—' : `in ${formatDuration(s.next_sweep_seconds)}`),
    h('dt', {}, 'Full pass'), h('dd', {}, s.full_pass_running ? h('span', { class: 'badge blue' }, 'Running')
      : `daily at ${s.daily_at || '—'}${s.last_full_pass ? `, last ${s.last_full_pass}` : ''}`)));
  const action = (path, done) => async () => {
    await api.post(path);
    toast(done, 'success');
    ctx.refreshStatus();
  };
  return toolCard({
    iconName: 'refresh', title: 'Run now',
    intro: 'A sweep checks for new and changed titles. A full pass checks every title, as the daily pass does.',
    body: [facts, h('div', { class: 'row wrap' },
      button({ label: 'Run a sweep now', icon: 'refresh', onClick: action('/api/actions/sweep', 'Sweep started') }),
      button({ label: 'Run a full pass now', icon: 'layers', onClick: action('/api/actions/full', 'Full pass started') }))],
  });
}

// ---- Pause / resume ----

function pauseCard(ctx, onStatus) {
  const slot = h('div', { class: 'col' });
  const toggle = async (pause) => {
    await api.post(`/api/actions/${pause ? 'pause' : 'resume'}`);
    toast(pause ? 'Paused' : 'Resumed', 'success');
    await ctx.refreshStatus();
  };
  onStatus((s) => mount(slot,
    h('div', { class: 'row' }, h('span', { class: ['pill', s.paused ? 'paused' : 'live'] }, h('span', { class: 'dot' }), s.paused ? 'Paused' : 'Running')),
    s.paused
      ? button({ label: 'Resume', icon: 'play', kind: 'primary', onClick: () => toggle(false) })
      : button({ label: 'Pause', icon: 'pause', onClick: () => toggle(true) })));
  if (!ctx.status) mount(slot, h('span', { class: 'spinner' }));
  return toolCard({
    iconName: 'pause', title: 'Pause',
    intro: 'Stops sweeps, full passes and library items until you resume. Titles from webhooks wait and run when you resume. What you start in the web UI, such as a preview, still runs. It stays paused after a restart.',
    body: slot,
  });
}

// ---- Test alert ----

function alertCard(ctx, onStatus) {
  const slot = h('div', { class: 'col' });
  const result = h('div', { class: 'test-result hidden', role: 'status' });
  const send = async () => {
    const res = await runJob('/api/actions/test-alert', {}, 'Test alert', { quiet: true });
    if (!res) return;
    result.className = `test-result ${res.sent ? 'ok' : 'fail'}`;
    mount(result, icon(res.sent ? 'check' : 'alert'), h('span', { class: 'grow' }, res.message || (res.sent ? 'Sent.' : 'Not sent.')));
  };
  onStatus((s) => mount(slot, s.features?.notify
    ? button({ label: 'Send a test alert', icon: 'bell', onClick: send })
    : h('p', { class: 'small muted' }, 'No alert addresses are set. Add them under ',
      h('a', { class: 'link', href: '/settings?q=NOTIFY_URLS', 'data-link': '' }, 'Settings, Alerts'), '.'),
  result));
  return toolCard({
    iconName: 'bell', title: 'Test alert',
    intro: 'Sends a test message to every address in NOTIFY_URLS.',
    body: slot,
  });
}

// ---- Webhook help ----

const WEBHOOK_STEPS = {
  plex: [
    'Needs Plex Pass.',
    'In Plex, open Settings, then Webhooks.',
    'Choose Add Webhook and paste the address below, with your own WEBHOOK_SECRET.',
    'Choose Save Changes.',
  ],
  jellyfin: [
    'In the Jellyfin dashboard, open Plugins, Catalog and install Webhook.',
    'In the plugin\'s settings, add a Generic destination with the address below.',
    'Choose the notification type Item Added, the item types you want, and turn on Send All Properties.',
    'Save, then restart Jellyfin once. Until then the plugin sends nothing. It sends new items about every 30 seconds.',
  ],
  emby: [
    'In the Emby dashboard, open Notifications.',
    'Add a Webhooks notification with the address below.',
    'Choose the event New Media Added. Both request types work.',
  ],
};

function webhookCard(onStatus) {
  const url = `${location.protocol}//${location.host}/webhook/YOUR-WEBHOOK-SECRET`;
  const steps = h('div');
  let current = 'plex';
  const paint = (id) => {
    current = id;
    mount(steps, h('ol', { class: 'steps' }, WEBHOOK_STEPS[id].map((step) => h('li', {}, step))));
  };
  const tabSlot = h('div');
  const paintTabs = () => mount(tabSlot, tabs([{ id: 'plex', label: 'Plex' }, { id: 'jellyfin', label: 'Jellyfin' }, { id: 'emby', label: 'Emby' }], current, paint));
  let picked = false;
  onStatus((s) => {
    const name = (s.server?.name || '').toLowerCase();
    if (!picked && WEBHOOK_STEPS[name]) {
      picked = true;
      paint(name);
      paintTabs();
    }
  });
  paint(current);
  paintTabs();
  return toolCard({
    iconName: 'link', title: 'Webhook for new titles', className: 'wide-card',
    intro: 'With a webhook, your server tells Posteryard about a new title right away. Without one, new titles get posters at the next sweep.',
    body: [tabSlot, steps,
      h('div', { class: 'webhook-url' }, h('code', { class: 'grow' }, url),
        button({ label: 'Copy', icon: 'copy', small: true, onClick: () => copyText(url) })),
      h('p', { class: 'small muted' }, 'Replace YOUR-WEBHOOK-SECRET with your WEBHOOK_SECRET. Your server must reach this address: use the machine\'s network address, not localhost, and the port your server can reach.')],
  });
}

// ---- Danger zone ----

function dangerCard(ctx, onStatus) {
  const slot = h('div', { class: 'danger-row' });
  const result = h('div', { class: 'restore-result' });
  let paused = false;
  const restore = async () => {
    if (!paused) {
      const pause = await confirmDialog({
        title: 'Pause first',
        message: 'Restoring needs Posteryard paused, so it does not upload the images again right away. Pause now?',
        confirmLabel: 'Pause',
      });
      if (!pause) return;
      await api.post('/api/actions/pause');
      await ctx.refreshStatus();
      paused = true;
    }
    const typed = await typeToConfirm();
    if (!typed) return;
    const res = await runJob('/api/actions/restore-all', { confirm: 'RESTORE' }, 'Restore every image', { quiet: true });
    if (!res) return;
    toast('Restore finished', 'success');
    mount(result, h('div', { class: 'grid cols-3 restore-counts' },
      count('Restored', res.restored, 'green'), count('Kept, changed by hand', res.kept, ''), count('Failed', res.failed, res.failed ? 'red' : '')),
    h('p', { class: 'small muted' }, 'Posteryard is still paused, so it uploads nothing new. Resume only if you want its images back. To uninstall, stop the container now.'));
  };
  onStatus((s) => {
    paused = Boolean(s.paused);
    mount(slot, h('div', { class: 'grow' }, h('h3', {}, 'Restore every image'),
      h('p', { class: 'muted' }, 'Gives every image Posteryard uploaded back to the server\'s own and unlocks it. Images you changed by hand stay as they are. Use it before you uninstall.'),
      !paused && h('p', { class: 'small warn-text' }, icon('pause'), 'Needs Posteryard paused. You are asked to pause first.')),
    button({ label: 'Restore every image', icon: 'undo', kind: 'danger', onClick: restore }));
  });
  if (!ctx.status) mount(slot, h('span', { class: 'spinner' }));
  return h('section', { class: 'card danger-zone' },
    h('div', { class: 'tool-head' }, h('div', { class: 'tool-icon' }, icon('alert', 'l')),
      h('div', { class: 'grow' }, h('h2', {}, 'Danger zone'), h('p', { class: 'muted' }, 'These change every title on your server.'))),
    h('div', { class: 'tool-body' }, slot, result));
}

function count(label, value, tone) {
  return h('div', { class: ['card', 'stat', tone] }, h('div', { class: 'label' }, label), h('div', { class: 'value' }, String(value ?? 0)));
}

function typeToConfirm() {
  return new Promise((resolve) => {
    let answered = false;
    const input = h('input', { class: 'input', id: 'restore-confirm', autocomplete: 'off', spellcheck: 'false', placeholder: 'RESTORE' });
    const dialog = modal({
      title: 'Restore every image?',
      content: h('div', { class: 'col' },
        h('p', {}, 'Every image Posteryard uploaded goes back to the server\'s own. Posteryard stays paused. When you resume, it uploads its images again.'),
        h('label', { for: 'restore-confirm' }, 'Type ', h('b', {}, 'RESTORE'), ' to confirm.'), input),
      onClose: () => { if (!answered) resolve(false); },
      actions: [
        { label: 'Cancel', kind: 'ghost' },
        { label: 'Restore every image', kind: 'danger', onClick: () => {
          if (input.value.trim() !== 'RESTORE') { input.classList.add('invalid'); input.focus(); return false; }
          answered = true;
          resolve(true);
          return true;
        } },
      ],
    });
    const confirmButton = dialog.el.querySelector('.modal-foot .btn.danger');
    confirmButton.disabled = true;
    input.addEventListener('input', () => { confirmButton.disabled = input.value.trim() !== 'RESTORE'; input.classList.remove('invalid'); });
    input.addEventListener('keydown', (event) => { if (event.key === 'Enter' && !confirmButton.disabled) confirmButton.click(); });
    input.focus();
  });
}

