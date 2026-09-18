// Account: profile, password change, own sessions.
import { requireAuth, currentUser } from '../workspace.js';
import { mountAppChrome } from '../layout.js';
import { api, ApiError } from '../api.js';
import { $, el, clear, fmtDate, labelize, setStatus, applyFieldErrors, clearFieldErrors } from '../util.js';
import { confirmDialog } from '../dialog.js';

async function main() {
  mountAppChrome();
  const me = await requireAuth();
  if (!me) return;
  renderProfile(me);
  loadPolicy();
  loadSessions();
  wirePassword();
}

function renderProfile(me) {
  const box = $('#profile');
  const dl = el('dl', { class: 'defs' });
  const add = (k, v) => { dl.append(el('dt', { text: k }), el('dd', { text: v || '—' })); };
  add('Name', me.name);
  add('Email', me.email);
  add('Role', labelize(me.role));
  add('Designation', me.designation);
  add('Jurisdiction', me.jurisdiction_name || me.jurisdiction_code);
  box.appendChild(el('div', { class: 'panel' }, [dl]));
  if (me.must_change_password) {
    box.appendChild(el('div', { class: 'notice notice-warn' }, [el('p', { text: 'You are required to change your password.' })]));
  }
}

async function loadPolicy() {
  try { const pol = await api.get('/auth/password-policy'); $('#pw-policy').textContent = (pol.requirements || []).join(' ') || `Minimum ${pol.min_length} characters.`; }
  catch { $('#pw-policy').textContent = 'Choose a strong password.'; }
}

function wirePassword() {
  const form = $('#pw-form');
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    clearFieldErrors(form); setStatus($('#pw-status'), '');
    try {
      await api.post('/auth/me/password', {
        current_password: form.elements['current_password'].value,
        new_password: form.elements['new_password'].value,
      });
      setStatus($('#pw-status'), 'Password changed.', 'ok');
      form.reset();
    } catch (err) {
      if (err instanceof ApiError && err.status === 422) applyFieldErrors(form, err);
      setStatus($('#pw-status'), err instanceof ApiError ? err.message : 'Could not change the password.', 'error');
    }
  });
}

async function loadSessions() {
  const box = $('#sessions');
  clear(box);
  try {
    const res = await api.get('/auth/me/sessions');
    const items = res.items || res || [];
    if (!items.length) { box.appendChild(el('p', { class: 'muted', text: 'No active sessions.' })); return; }
    const table = el('table', { class: 'register' });
    table.appendChild(el('caption', { text: 'Active sessions' }));
    table.appendChild(el('thead', {}, [el('tr', {}, [
      el('th', { scope: 'col', text: 'Device / agent' }),
      el('th', { scope: 'col', text: 'IP' }),
      el('th', { scope: 'col', text: 'Last seen' }),
      el('th', { scope: 'col', text: '' }),
    ])]));
    const tbody = el('tbody');
    for (const s of items) {
      const revoke = el('button', { class: 'btn btn-sm btn-danger', type: 'button', text: s.is_current ? 'This device' : 'Revoke', disabled: !!s.is_current });
      if (!s.is_current) revoke.addEventListener('click', async () => {
        if (!(await confirmDialog('Revoke session', 'Sign out this session?', { danger: true, confirmLabel: 'Revoke' }))) return;
        try { await api.del(`/auth/me/sessions/${s.id}`); loadSessions(); } catch (err) { setStatus($('#page-status'), err instanceof ApiError ? err.message : 'Could not revoke.', 'error'); }
      });
      tbody.appendChild(el('tr', {}, [
        el('th', { scope: 'row', text: s.user_agent || 'unknown' }),
        el('td', { text: s.ip_address || '—' }),
        el('td', { text: s.last_seen_at ? fmtDate(s.last_seen_at) : '—' }),
        el('td', {}, [revoke]),
      ]));
    }
    table.appendChild(tbody);
    box.appendChild(el('div', { class: 'table-wrap' }, [table]));
  } catch (err) {
    box.appendChild(el('div', { class: 'notice notice-error', role: 'alert' }, [el('p', { text: err instanceof ApiError ? err.message : 'Could not load sessions.' })]));
  }
}

main();
