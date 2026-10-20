// A before/after comparison: drag the divider (pointer or touch), or focus it and use the arrow keys.
import { h, icon } from '../ui.js';

/** before/after: {src, label}. wide for 16:9 images. */
export function compareSlider({ before, after, wide = false, alt = '' }) {
  let position = 50;
  const beforeImg = h('img', { src: before.src, alt: `${alt} ${before.label}`.trim(), draggable: false, decoding: 'async' });
  const afterImg = h('img', { src: after.src, alt: `${alt} ${after.label}`.trim(), draggable: false, decoding: 'async' });
  const beforeLayer = h('div', { class: 'compare-before' }, beforeImg);
  const handle = h('div', {
    class: 'compare-handle', role: 'slider', tabindex: '0', 'aria-label': `Compare ${before.label} and ${after.label}`,
    'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-orientation': 'horizontal',
  }, h('span', { class: 'compare-knob' }, icon('chevronLeft'), icon('chevronRight')));
  const el = h('div', { class: ['compare', wide && 'wide'] },
    afterImg, beforeLayer, handle,
    h('span', { class: 'compare-tag left' }, before.label), h('span', { class: 'compare-tag right' }, after.label));

  for (const img of [beforeImg, afterImg]) {
    img.addEventListener('error', () => img.replaceWith(h('div', { class: 'placeholder-art' }, 'No image')));
  }

  const set = (value) => {
    position = Math.max(0, Math.min(100, value));
    beforeLayer.style.clipPath = `inset(0 ${100 - position}% 0 0)`;
    handle.style.left = `${position}%`;
    handle.setAttribute('aria-valuenow', String(Math.round(position)));
    handle.setAttribute('aria-valuetext', `${Math.round(position)}% ${before.label}`);
  };
  set(position);

  const fromEvent = (event) => {
    const rect = el.getBoundingClientRect();
    set(((event.clientX - rect.left) / rect.width) * 100);
  };
  let dragging = null;
  el.addEventListener('pointerdown', (event) => {
    if (event.button !== 0) return;
    dragging = event.pointerId;
    el.setPointerCapture(event.pointerId);
    el.classList.add('dragging');
    fromEvent(event);
    handle.focus({ preventScroll: true });
  });
  el.addEventListener('pointermove', (event) => { if (dragging === event.pointerId) fromEvent(event); });
  const stop = (event) => {
    if (dragging !== event.pointerId) return;
    dragging = null;
    el.classList.remove('dragging');
  };
  el.addEventListener('pointerup', stop);
  el.addEventListener('pointercancel', stop);
  handle.addEventListener('keydown', (event) => {
    const step = event.shiftKey ? 10 : 2;
    const moves = { ArrowLeft: -step, ArrowDown: -step, ArrowRight: step, ArrowUp: step, PageDown: -25, PageUp: 25 };
    if (event.key in moves) set(position + moves[event.key]);
    else if (event.key === 'Home') set(0);
    else if (event.key === 'End') set(100);
    else return;
    event.preventDefault();
  });
  return el;
}
