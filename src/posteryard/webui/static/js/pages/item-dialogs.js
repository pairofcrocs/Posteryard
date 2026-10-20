// Dialogs of the title page: use your own image, the preview of a render, and the candidate lightbox.
import { h, icon, modal, tabs, badge, lazyImage, formatBytes, TARGET_LABEL } from '../ui.js';

const MAX_BYTES = 40 * 1024 * 1024;

/**
 * Ask for an image from a URL or a file. Resolves {url} or {blob}, or null when cancelled.
 * live: the server changes right away, so the dialog says so.
 */
export function ownImageDialog({ title, live, serverName }) {
  return new Promise((resolve) => {
    let mode = 'url';
    let file = null;
    let objectUrl = null;
    let answered = false;

    const urlInput = h('input', {
      class: 'input', type: 'url', id: 'own-image-url', placeholder: 'https://example.org/poster.jpg', autocomplete: 'off', spellcheck: 'false',
    });
    const urlError = h('div', { class: 'error', role: 'alert' });
    const urlPane = h('div', { class: 'field' },
      h('label', { for: 'own-image-url' }, 'Image URL'), urlInput,
      h('div', { class: 'help' }, 'A direct link to a JPEG, PNG or WebP image. Posteryard downloads it.'), urlError);

    const fileInput = h('input', { type: 'file', accept: 'image/jpeg,image/png,image/webp,image/*', class: 'sr-only', id: 'own-image-file' });
    const fileError = h('div', { class: 'error', role: 'alert' });
    const preview = h('div', { class: 'drop-preview hidden' });
    const dropText = h('div', { class: 'drop-text' }, icon('upload', 'xl'),
      h('div', {}, h('strong', {}, 'Drop an image here'), ' or ', h('span', { class: 'link' }, 'choose a file')),
      h('div', { class: 'small muted' }, 'JPEG, PNG or WebP, up to 40 MB.'));
    const drop = h('label', { class: 'drop-zone', for: 'own-image-file', tabindex: '0' }, dropText, preview);
    drop.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); fileInput.click(); }
    });
    const filePane = h('div', { class: 'field hidden' }, fileInput, drop, fileError);

    const pick = (picked) => {
      fileError.textContent = '';
      if (!picked) return;
      if (!picked.type.startsWith('image/')) {
        fileError.textContent = 'That file is not an image.';
        return;
      }
      if (picked.size > MAX_BYTES) {
        fileError.textContent = `That image is ${formatBytes(picked.size)}. The limit is 40 MB.`;
        return;
      }
      file = picked;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      objectUrl = URL.createObjectURL(picked);
      preview.replaceChildren(h('img', { src: objectUrl, alt: 'The chosen image' }),
        h('div', { class: 'small' }, h('div', { class: 'truncate' }, picked.name || 'Image'), h('div', { class: 'muted' }, formatBytes(picked.size))));
      preview.classList.remove('hidden');
      dropText.classList.add('hidden');
    };
    fileInput.addEventListener('change', () => pick(fileInput.files[0]));
    for (const name of ['dragenter', 'dragover']) {
      drop.addEventListener(name, (event) => { event.preventDefault(); drop.classList.add('over'); });
    }
    for (const name of ['dragleave', 'drop']) {
      drop.addEventListener(name, (event) => { event.preventDefault(); drop.classList.remove('over'); });
    }
    drop.addEventListener('drop', (event) => pick(event.dataTransfer?.files?.[0]));

    const switcher = tabs([{ id: 'url', label: 'From a URL' }, { id: 'file', label: 'Upload a file' }], mode, (id) => {
      mode = id;
      urlPane.classList.toggle('hidden', id !== 'url');
      filePane.classList.toggle('hidden', id !== 'file');
      if (id === 'url') urlInput.focus();
      else drop.focus();
    });

    const content = h('div', { class: 'page-item-dialog own-image' },
      h('p', { class: 'muted' }, `Posteryard uses your image as the art of ${title} and draws the title logo and badges on top.`),
      switcher, urlPane, filePane,
      h('p', { class: ['small', live ? 'warn-text' : 'muted'] }, live
        ? `This uploads the new poster to ${serverName} right away. Reset to automatic art undoes it.`
        : 'DRY_RUN is on: Posteryard saves a preview and changes nothing on the server.'));

    const dialog = modal({
      title: 'Use my own image',
      content,
      onClose: () => {
        if (objectUrl) URL.revokeObjectURL(objectUrl);
        if (!answered) resolve(null);
      },
      actions: [
        { label: 'Cancel', kind: 'ghost' },
        {
          label: live ? 'Use this image and upload' : 'Use this image', kind: 'primary', onClick: () => {
            if (mode === 'url') {
              const value = urlInput.value.trim();
              if (!/^https?:\/\/\S+$/i.test(value)) {
                urlError.textContent = value ? 'Enter a full link that starts with https://.' : 'Enter the link to an image.';
                urlInput.classList.add('invalid');
                urlInput.focus();
                return false;
              }
              answered = true;
              resolve({ url: value });
              return undefined;
            }
            if (!file) {
              fileError.textContent = 'Choose an image first.';
              return false;
            }
            answered = true;
            resolve({ blob: file });
            return undefined;
          },
        },
      ],
    });
    urlInput.addEventListener('input', () => { urlError.textContent = ''; urlInput.classList.remove('invalid'); });
    urlInput.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') dialog.el.querySelector('.modal-foot .btn.primary')?.click();
    });
    urlInput.focus();
  });
}

/** Show every image a render job made, with its notes and whether it would change. */
export function previewDialog({ title, renders }) {
  const content = renders.length
    ? h('div', { class: 'render-grid' }, renders.map((render) => {
      const wide = render.target !== 'poster';
      return h('figure', { class: ['render-card', wide && 'wide'] },
        h('a', { href: render.url, target: '_blank', rel: 'noopener', class: 'render-art', title: 'Open full size' },
          h('img', { src: render.url, alt: `${TARGET_LABEL[render.target] || render.target} for ${title}`, loading: 'lazy' })),
        h('figcaption', {},
          h('div', { class: 'row wrap' }, h('strong', {}, TARGET_LABEL[render.target] || render.target),
            render.changed ? badge('Would change', 'orange') : badge('Same as now')),
          render.notes && h('div', { class: 'small muted' }, render.notes),
          render.name && h('div', { class: 'mono small faint truncate', title: render.name }, render.name)));
    }))
    : h('p', { class: 'muted' }, 'Posteryard made no images for this title.');
  modal({
    title: `Preview of ${title}`,
    wide: true,
    content: h('div', { class: 'page-item-dialog' },
      h('p', { class: 'small muted', style: { marginBottom: '16px' } }, 'Rendered just now. Nothing was saved or uploaded.'), content),
    actions: [{ label: 'Close', kind: 'primary' }],
  });
}

/** A candidate at full size. onUse is set for a clean candidate that is not in use. */
export function lightbox({ candidate, group, onUse }) {
  const actions = [{ label: 'Close', kind: onUse ? 'ghost' : 'primary' }];
  if (onUse) actions.push({ label: 'Use this', kind: 'primary', icon: 'check', onClick: onUse });
  modal({
    title: group,
    wide: true,
    content: h('div', { class: 'page-item-dialog lightbox' },
      lazyImage(candidate.full_url || candidate.thumb_url, { alt: `${group} candidate ${candidate.path}`, placeholder: 'This image could not be loaded.' }),
      h('div', { class: 'row wrap small muted' },
        h('span', { class: 'mono' }, candidate.path),
        candidate.width ? h('span', {}, `${candidate.width} × ${candidate.height}`) : null,
        candidate.votes ? h('span', {}, candidate.votes) : null)),
    actions,
  });
}
