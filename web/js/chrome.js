// Global page chrome: offline/online banner, vocabulary cache, year stamp.
import { api } from './api.js';
import { el, $ } from './util.js';

let vocabCache = null;

export async function getVocabulary() {
  if (vocabCache) return vocabCache;
  vocabCache = await api.get('/reference/vocabulary');
  return vocabCache;
}

// Map a vocabulary list to a label for a value.
export function labelFor(list, value) {
  if (!Array.isArray(list)) return value;
  const found = list.find((o) => o.value === value);
  return found ? found.label : value;
}

export function initOfflineBanner() {
  let banner = $('#offline-banner');
  if (!banner) {
    banner = el('div', {
      id: 'offline-banner',
      class: 'global-banner',
      role: 'status',
      'aria-live': 'polite',
      text: 'You are offline. Changes cannot be saved until the connection returns.',
    });
    banner.hidden = navigator.onLine;
    document.body.insertBefore(banner, document.body.firstChild);
  }
  const update = () => { banner.hidden = navigator.onLine; };
  window.addEventListener('online', update);
  window.addEventListener('offline', update);
  update();
}

export function stampYear() {
  const y = new Date().getFullYear();
  document.querySelectorAll('[data-year]').forEach((n) => { n.textContent = y; });
}

// Initialise common page behaviour.
export function initChrome() {
  initOfflineBanner();
  stampYear();
}
