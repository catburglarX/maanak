// Audit trail with filters and honest chain verification.
import { requireAuth } from '../workspace.js';
import { mountAppChrome, createRegister } from '../layout.js';
import { api, ApiError } from '../api.js';
import { $, el, clear, fmtDate, labelize, counted, NOT_RECORDED } from '../util.js';

async function main() {
  mountAppChrome();
  const me = await requireAuth();
  if (!me) return;

  // Populate actions filter honestly from the API.
  try {
    const actions = await api.get('/audit/actions');
    const list = Array.isArray(actions) ? actions : (actions.items || actions.actions || []);
    const sel = $('#f-action');
    for (const a of list) {
      const value = typeof a === 'string' ? a : (a.value || a.action);
      sel.appendChild(el('option', { value, text: labelize(value) }));
    }
  } catch { /* filter still usable as free selection of Any */ }

  const register = createRegister($('#register'), {
    endpoint: '/audit',
    caption: 'Audit events',
    emptyTitle: 'No audit events',
    emptyDetail: 'Events matching your filters will appear here.',
    params: () => ({
      action: $('#f-action').value,
      entity_type: $('#f-entity').value.trim(),
      since: $('#f-since').value ? new Date($('#f-since').value).toISOString() : '',
      until: $('#f-until').value ? new Date($('#f-until').value).toISOString() : '',
    }),
    columns: [
      { label: 'Seq', render: (e) => String(e.sequence) },
      { label: 'Action', render: (e) => labelize(e.action) },
      { label: 'Entity', render: (e) => `${labelize(e.entity_type)} ${e.entity_id ? e.entity_id.slice(0, 8) : ''}`.trim() },
      { label: 'Actor', render: (e) => e.actor_is_public ? 'Public' : (e.actor_email || (e.actor_role ? labelize(e.actor_role) : 'System')) },
      { label: 'Reason', render: (e) => e.reason || NOT_RECORDED },
      { label: 'Recorded', render: (e) => fmtDate(e.recorded_at) },
    ],
  });
  $('#filters').addEventListener('submit', (e) => { e.preventDefault(); register.reload(); });
  register.reload();

  $('#verify-chain').addEventListener('click', verifyChain);
}

async function verifyChain() {
  const out = $('#verify-result');
  clear(out);
  const btn = $('#verify-chain');
  btn.disabled = true;
  try {
    const res = await api.get('/audit/verify');
    const intact = res.intact === true;
    const box = el('div', { class: intact ? 'notice notice-ok' : 'notice notice-error', role: intact ? 'status' : 'alert' });
    box.appendChild(el('p', {}, [
      el('strong', { text: intact ? 'Audit chain intact. ' : 'Audit chain is NOT intact. ' }),
      document.createTextNode(`${counted(res.events_checked ?? 0, 'event')} checked.`),
    ]));
    if (!intact) {
      if (res.first_broken_sequence != null) box.appendChild(el('p', { text: `First broken sequence: ${res.first_broken_sequence}.` }));
      if (res.problem) box.appendChild(el('p', { text: `Problem: ${res.problem}` }));
    }
    if (res.head_sequence != null) box.appendChild(el('p', { class: 'small muted', text: `Head sequence ${res.head_sequence}.` }));
    if (res.note) box.appendChild(el('p', { class: 'small muted', text: res.note }));
    out.appendChild(box);
  } catch (err) {
    out.appendChild(el('div', { class: 'notice notice-error', role: 'alert' }, [el('p', { text: err instanceof ApiError ? err.message : 'Could not verify the chain.' })]));
  } finally { btn.disabled = false; }
}

main();
