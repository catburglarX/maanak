// Workspace bootstrap: verifies session, renders the signed-in user,
// wires sign-out, exposes permission checks, highlights current nav.
import { api, auth, ApiError } from './api.js';
import { el, $, clear, labelize } from './util.js';
import { initChrome } from './chrome.js';

let profile = null;

export function currentUser() { return profile; }
export function can(permission) {
  return !!(profile && Array.isArray(profile.permissions) && profile.permissions.includes(permission));
}

function renderUserBar(bar) {
  clear(bar);
  if (!profile) return;
  bar.appendChild(el('div', { class: 'who' }, [
    el('span', { class: 'name', text: profile.name }),
    el('span', { class: 'role', text: [labelize(profile.role), profile.jurisdiction_name].filter(Boolean).join(' · ') }),
  ]));
  const settings = el('a', { href: '/app/account.html', class: 'btn btn-sm', text: 'Account' });
  const out = el('button', { class: 'btn btn-sm', type: 'button', text: 'Sign out' });
  out.addEventListener('click', async () => {
    out.disabled = true;
    try { await auth.signOut(); } catch { /* proceed to login regardless */ }
    location.href = '/login.html';
  });
  bar.appendChild(settings);
  bar.appendChild(out);
}

function highlightNav() {
  const path = location.pathname.split('/').pop();
  document.querySelectorAll('.app-nav a').forEach((a) => {
    const href = (a.getAttribute('href') || '').split('/').pop();
    if (href === path) a.setAttribute('aria-current', 'page');
  });
}

// Show or hide every element that declares a required permission.
//
// This used to hide only. Elements are written into the markup with `hidden` so a
// forbidden control never flashes on screen before the profile arrives, which meant a
// permitted control was never revealed either: the evidence upload panel on the
// inspection screen declared data-permission="evidence.upload" and stayed hidden for
// everyone, including an inspector who holds it. Pages that happened to un-hide their
// own button in JavaScript worked; the one section that relied on this did not.
//
// Both directions are now driven from the profile, so `hidden` in the markup means
// "hidden until the permission is confirmed" rather than "hidden forever".
export function applyPermissionVisibility(root = document) {
  root.querySelectorAll('[data-permission]').forEach((node) => {
    const perm = node.getAttribute('data-permission');
    if (!perm) return;
    node.hidden = !can(perm);
  });
}

// Call at the top of every workspace page. Returns the profile.
export async function requireAuth() {
  initChrome();
  try {
    profile = await api.get('/auth/me', undefined);
  } catch (err) {
    if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
      const next = encodeURIComponent(location.pathname + location.search);
      location.href = `/login.html?next=${next}`;
      return null;
    }
    throw err;
  }
  const bar = $('#user-bar');
  if (bar) renderUserBar(bar);
  highlightNav();
  applyPermissionVisibility();
  return profile;
}
