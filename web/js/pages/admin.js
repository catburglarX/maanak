// Administration: accounts, roles, jurisdictions, sessions, password reset.
import { requireAuth, can } from '../workspace.js';
import { mountAppChrome, createRegister } from '../layout.js';
import { api, ApiError } from '../api.js';
import { $, el, clear, tag, fmtDate, labelize, fillSelect, setStatus, applyFieldErrors, clearFieldErrors } from '../util.js';
import { getVocabulary, labelFor } from '../chrome.js';
import { openDialog, reasonDialog, confirmDialog } from '../dialog.js';

let vocab = null;
let register = null;

async function main() {
  mountAppChrome();
  const me = await requireAuth();
  if (!me) return;
  vocab = await getVocabulary();
  fillSelect($('#f-role'), vocab.roles, { blank: 'Any role' });

  register = createRegister($('#register'), {
    endpoint: '/users',
    caption: 'Accounts',
    emptyTitle: 'No accounts',
    emptyDetail: 'Create the first officer account with the New account button.',
    params: () => ({ search: $('#f-search').value.trim(), role: $('#f-role').value, active: $('#f-active').value }),
    columns: [
      { label: 'Name', render: (u) => u.name },
      { label: 'Email', render: (u) => u.email },
      { label: 'Role', render: (u) => tag(u.is_active ? 'active' : 'draft', labelFor(vocab.roles, u.role)) },
      { label: 'Jurisdiction', render: (u) => u.jurisdiction_name || u.jurisdiction_code || '—' },
      { label: 'Status', render: (u) => u.locked ? tag('rejected', 'Locked') : (u.is_active ? tag('confirmed', 'Active') : tag('grey', 'Inactive')) },
      { label: 'Last login', render: (u) => u.last_login_at ? fmtDate(u.last_login_at) : 'Never' },
      { label: 'Manage', render: (u) => renderRowActions(u) },
    ],
  });
  $('#filters').addEventListener('submit', (e) => { e.preventDefault(); register.reload(); });
  register.reload();

  const nb = $('#new-user');
  if (can('user.create')) { nb.hidden = false; nb.addEventListener('click', createUser); }
}

function renderRowActions(u) {
  const row = el('div', { class: 'btn-row' });
  if (can('user.update')) { const b = el('button', { class: 'btn btn-sm', type: 'button', text: 'Edit' }); b.addEventListener('click', () => editUser(u)); row.appendChild(b); }
  const s = el('button', { class: 'btn btn-sm', type: 'button', text: 'Sessions' }); s.addEventListener('click', () => viewSessions(u)); row.appendChild(s);
  if (can('user.reset_password')) { const p = el('button', { class: 'btn btn-sm', type: 'button', text: 'Reset password' }); p.addEventListener('click', () => resetPassword(u)); row.appendChild(p); }
  return row;
}

function fieldEl(name, label, control, required) {
  control.id = `u-${name}`; control.name = name;
  return el('div', { class: 'field' }, [
    el('label', { for: control.id }, [label, required ? el('span', { class: 'req', 'aria-hidden': 'true', text: ' *' }) : null]),
    control,
  ]);
}

async function createUser() {
  const roleSel = el('select', {}); fillSelect(roleSel, vocab.roles, { blank: 'Select role…' });
  const mustChange = el('input', { type: 'checkbox', checked: true }); mustChange.id = 'u-must'; mustChange.name = 'must_change_password';
  const form = el('form', { novalidate: true });
  const status = el('div', { class: 'status-region', 'aria-live': 'polite' });
  let policyHint = 'Choose a strong password.';
  try { const pol = await api.get('/auth/password-policy'); policyHint = (pol.requirements || []).join(' ') || policyHint; } catch { /* keep default */ }
  form.append(
    status,
    fieldEl('name', 'Name', el('input', { type: 'text', maxlength: '160' }), true),
    fieldEl('email', 'Email', el('input', { type: 'email' }), true),
    (() => { const f = fieldEl('password', 'Temporary password', el('input', { type: 'password', autocomplete: 'new-password' }), true); f.querySelector('input').setAttribute('aria-describedby', 'u-pw-hint'); f.appendChild(el('p', { class: 'hint', id: 'u-pw-hint', text: policyHint })); return f; })(),
    fieldEl('role', 'Role', roleSel, true),
    fieldEl('jurisdiction_code', 'Jurisdiction code', el('input', { type: 'text' }), true),
    fieldEl('jurisdiction_name', 'Jurisdiction name', el('input', { type: 'text' }), true),
    fieldEl('designation', 'Designation', el('input', { type: 'text' })),
    el('div', { class: 'field checkbox-row' }, [mustChange, el('label', { for: 'u-must', text: 'Require password change at next sign-in' })]),
  );
  const dlg = openDialog({
    title: 'New account', body: form,
    actions: [
      { label: 'Cancel', value: null },
      { label: 'Create', class: 'btn-primary', close: false, onClick: async () => {
        clearFieldErrors(form); setStatus(status, '');
        const g = (n) => { const e = form.elements[n]; return e && e.value.trim() ? e.value.trim() : undefined; };
        const payload = { name: g('name'), email: g('email'), password: form.elements['password'].value, role: g('role'), jurisdiction_code: g('jurisdiction_code'), jurisdiction_name: g('jurisdiction_name'), designation: g('designation'), must_change_password: mustChange.checked };
        Object.keys(payload).forEach((k) => payload[k] === undefined && delete payload[k]);
        try { await api.post('/users', payload); dlg.close(); setStatus($('#page-status'), 'Account created.', 'ok'); register.reload(); }
        catch (err) { if (err instanceof ApiError && err.status === 422) applyFieldErrors(form, err); setStatus(status, err instanceof ApiError ? err.message : 'Could not create the account.', 'error'); }
        return false;
      } },
    ],
  });
}

async function editUser(u) {
  let full = u;
  try { full = await api.get(`/users/${u.id}`); } catch { /* use summary */ }
  const roleSel = el('select', {}); fillSelect(roleSel, vocab.roles, { selected: full.role });
  const active = el('input', { type: 'checkbox', checked: full.is_active !== false }); active.id = 'e-active';
  const form = el('form', { novalidate: true });
  const status = el('div', { class: 'status-region', 'aria-live': 'polite' });
  form.append(
    status,
    fieldEl('name', 'Name', el('input', { type: 'text', value: full.name || '' })),
    fieldEl('role', 'Role', roleSel),
    fieldEl('jurisdiction_code', 'Jurisdiction code', el('input', { type: 'text', value: full.jurisdiction_code || '' })),
    fieldEl('jurisdiction_name', 'Jurisdiction name', el('input', { type: 'text', value: full.jurisdiction_name || '' })),
    fieldEl('designation', 'Designation', el('input', { type: 'text', value: full.designation || '' })),
    el('div', { class: 'field checkbox-row' }, [active, el('label', { for: 'e-active', text: 'Account is active' })]),
  );
  const dlg = openDialog({
    title: `Edit ${full.name || full.email}`, body: form,
    actions: [
      { label: 'Cancel', value: null },
      { label: 'Save', class: 'btn-primary', close: false, onClick: async () => {
        clearFieldErrors(form); setStatus(status, '');
        const g = (n) => form.elements[n].value.trim();
        const payload = { name: g('name'), role: g('role'), jurisdiction_code: g('jurisdiction_code'), jurisdiction_name: g('jurisdiction_name'), designation: g('designation') || null, is_active: active.checked };
        try { await api.patch(`/users/${u.id}`, payload); dlg.close(); setStatus($('#page-status'), 'Account updated.', 'ok'); register.reload(); }
        catch (err) { if (err instanceof ApiError && err.status === 422) applyFieldErrors(form, err); setStatus(status, err instanceof ApiError ? err.message : 'Could not update the account.', 'error'); }
        return false;
      } },
    ],
  });
}

async function viewSessions(u) {
  const body = el('div', {}, [el('p', { text: 'Loading sessions…' })]);
  const dlg = openDialog({ title: `Sessions — ${u.name}`, body, actions: [{ label: 'Close', value: null }] });
  async function refresh() {
    clear(body);
    try {
      const res = await api.get(`/users/${u.id}/sessions`);
      const items = res.items || res || [];
      if (!items.length) { body.appendChild(el('p', { text: 'No active sessions.' })); return; }
      const revokeAll = el('button', { class: 'btn btn-sm btn-danger', type: 'button', text: 'Revoke all sessions' });
      revokeAll.addEventListener('click', async () => {
        if (!(await confirmDialog('Revoke all sessions', 'This signs the user out of all devices. Continue?', { danger: true, confirmLabel: 'Revoke all' }))) return;
        try { await api.del(`/users/${u.id}/sessions`); await refresh(); } catch (err) { body.appendChild(el('p', { class: 'field-msg', role: 'alert', text: err instanceof ApiError ? err.message : 'Failed.' })); }
      });
      body.appendChild(el('div', { class: 'btn-row mb-half' }, [revokeAll]));
      const ul = el('ul', {});
      for (const s of items) {
        ul.appendChild(el('li', {}, [
          document.createTextNode(`${s.ip_address || 'unknown IP'} — ${s.user_agent || 'unknown agent'} `),
          el('span', { class: 'small muted', text: `last seen ${s.last_seen_at ? fmtDate(s.last_seen_at) : '—'}` }),
        ]));
      }
      body.appendChild(ul);
    } catch (err) { body.appendChild(el('p', { class: 'field-msg', role: 'alert', text: err instanceof ApiError ? err.message : 'Could not load sessions.' })); }
  }
  refresh();
}

async function resetPassword(u) {
  const reason = await reasonDialog(`Reset password for ${u.name}`, { label: 'Reason for reset (recorded in the audit trail)', minLength: 1, confirmLabel: 'Reset' });
  if (!reason) return;
  try {
    const res = await api.post(`/users/${u.id}/password-reset`, { reason });
    const temp = res && (res.temporary_password || res.password);
    openDialog({
      title: 'Password reset',
      body: el('div', {}, [
        el('p', { text: 'The password has been reset. Communicate the temporary password securely; it will not be shown again.' }),
        temp ? el('p', { class: 'mono panel', text: temp }) : el('p', { class: 'muted', text: res && res.message ? res.message : 'A reset was recorded.' }),
      ]),
      actions: [{ label: 'Done', class: 'btn-primary', value: null }],
    });
  } catch (err) { setStatus($('#page-status'), err instanceof ApiError ? err.message : 'Could not reset the password.', 'error'); }
}

main();
