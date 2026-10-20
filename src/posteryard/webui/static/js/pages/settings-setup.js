// Setup mode pieces of the settings page: the first password and the welcome hero.
import { api } from '../api.js';
import { h, mount, icon, button } from '../ui.js';
import { SERVER_KEYS } from './settings-fields.js';

// Shown first in setup mode; everything else is under "More settings".
export const ESSENTIAL_KEYS = [...SERVER_KEYS, 'LIBRARIES', 'TMDB_API_KEY', 'WEBHOOK_SECRET', 'DRY_RUN'];

const MIN_PASSWORD = 8;

export function setupHero(session, data, text = null) {
  // After a save, the settings' own error is newer than the one that stopped the service.
  const saved = Boolean(data?.restart_required);
  const error = data ? data.error : session.setup_error;
  return h('section', { class: 'setup-hero' },
    h('img', { src: '/static/img/logo.svg', alt: '' }),
    h('div', { class: 'grow' },
      h('h1', {}, 'Let\'s set up Posteryard'),
      h('p', {}, text || 'Fill in the essentials below and save. Posteryard starts in preview mode, so nothing on your server changes until you go live.'),
      error && h('div', { class: 'setup-error', role: 'alert' }, icon('alert'),
        h('div', {}, h('b', {}, saved ? 'Still needed: ' : 'Posteryard could not start: '), error))));
}

/** Setup without any password yet: the first visitor chooses UI_PASSWORD. */
export function passwordStep(root, ctx) {
  const password = h('input', { class: 'input', id: 'setup-password', type: 'password', autocomplete: 'new-password' });
  const again = h('input', { class: 'input', id: 'setup-password-again', type: 'password', autocomplete: 'new-password' });
  const message = h('div', { class: 'field-error', role: 'alert' });
  const submit = button({ label: 'Save password', kind: 'primary', type: 'submit' });
  const form = h('form', { class: 'password-step settings-card' },
    h('div', { class: 'field' }, h('label', { for: 'setup-password' }, 'Password'), password,
      h('div', { class: 'help' }, `At least ${MIN_PASSWORD} characters. You sign in to the web UI with it.`)),
    h('div', { class: 'field' }, h('label', { for: 'setup-password-again' }, 'Password again'), again),
    message,
    h('div', { class: 'row' }, h('span', { class: 'grow small muted' }, 'Saved as UI_PASSWORD in the data folder.'), submit));
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    message.textContent = '';
    if (password.value.length < MIN_PASSWORD) {
      message.textContent = `The password needs at least ${MIN_PASSWORD} characters.`;
      password.focus();
      return;
    }
    if (password.value !== again.value) {
      message.textContent = 'The two passwords are not the same.';
      again.select();
      return;
    }
    submit.classList.add('busy');
    try {
      await api.put('/api/settings', { values: { UI_PASSWORD: password.value } });
      location.reload();
    } catch (err) {
      message.textContent = err.message;
    } finally {
      submit.classList.remove('busy');
    }
  });
  mount(root, setupHero(ctx.session, null, 'First choose a password for this page. Then fill in a few settings, and Posteryard starts.'),
    h('section', { class: 'settings-group' },
      h('div', { class: 'group-head' }, h('h2', {}, h('span', { class: 'step-title' }, icon('lock'), 'Choose a password'))),
      h('p', { class: 'group-intro muted' }, 'No password is set yet, so anyone who opens this page can change the settings. Choose one first.'),
      form));
  password.focus();
}
