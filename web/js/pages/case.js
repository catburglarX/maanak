// Case detail: notice prepare/issue/delivery/response/withdraw, transitions, outcome, events.
import { requireAuth, can } from '../workspace.js';
import { mountAppChrome } from '../layout.js';
import { api, ApiError } from '../api.js';
import { $, el, clear, tag, fmtDate, fmtDay, labelize, setStatus, showError, fillSelect } from '../util.js';
import { getVocabulary, labelFor } from '../chrome.js';
import { openDialog, reasonDialog } from '../dialog.js';

const id = new URLSearchParams(location.search).get('id');
let vocab = null;
let kase = null;

async function main() {
  mountAppChrome();
  const me = await requireAuth();
  if (!me) return;
  if (!id) { setStatus($('#page-status'), 'No case specified.', 'error'); return; }
  vocab = await getVocabulary();
  await load();
}

async function load() {
  try { kase = await api.get(`/cases/${id}`); }
  catch (err) { showError($('#detail'), err); return; }
  render();
}
function vlabel(list, v) { return labelFor(vocab[list], v); }

function render() {
  const box = $('#detail');
  box.removeAttribute('aria-busy');
  clear(box);
  box.appendChild(el('div', { class: 'between' }, [
    el('div', {}, [
      el('h1', { text: `Case ${kase.reference}` }),
      el('div', { class: 'btn-row' }, [tag(kase.state, vlabel('case_states', kase.state))]),
    ]),
    el('a', { class: 'btn btn-sm', href: '/app/cases.html', text: 'Back to cases' }),
  ]));

  const dl = el('dl', { class: 'defs' });
  const add = (k, v) => { if (v === null || v === undefined || v === '') return; dl.append(el('dt', { text: k }), el('dd', {}, [typeof v === 'object' ? v : document.createTextNode(String(v))])); };
  add('Subject', kase.subject);
  add('Respondent', kase.respondent_name);
  add('Respondent role', kase.respondent_role ? labelize(kase.respondent_role) : null);
  add('Respondent address', kase.respondent_address);
  add('Respondent email', kase.respondent_email);
  add('Respondent phone', kase.respondent_phone);
  add('Inspection', kase.inspection_id ? el('a', { href: `/app/inspection.html?id=${encodeURIComponent(kase.inspection_id)}`, text: 'View inspection' }) : null);
  add('Jurisdiction', kase.jurisdiction_name);
  add('Outcome', kase.outcome ? labelize(kase.outcome) : null);
  add('Outcome note', kase.outcome_note);
  add('Opened', fmtDate(kase.created_at));
  add('Version', kase.version);
  box.appendChild(dl);

  renderNotices(box);
  renderTransitions(box);
  renderActions(box);
}

function renderNotices(box) {
  const sec = el('section', {}, [el('h2', { text: `Notices (${(kase.notices || []).length})` })]);
  if (can('case.manage_notices')) {
    const prep = el('button', { class: 'btn', type: 'button', text: 'Prepare notice' });
    prep.addEventListener('click', prepareNotice);
    sec.appendChild(el('div', { class: 'btn-row mb-075' }, [prep]));
  }
  const notices = kase.notices || [];
  if (!notices.length) { sec.appendChild(el('p', { class: 'muted', text: 'No notices prepared.' })); box.appendChild(sec); return; }
  for (const n of notices) sec.appendChild(renderNotice(n));
  box.appendChild(sec);
}

function renderNotice(n) {
  const wrap = el('div', { class: 'panel' });
  wrap.appendChild(el('div', { class: 'between' }, [
    el('h3', { text: `${vlabel('notice_types', n.notice_type)} — ${n.reference}` }),
    n.is_frozen ? tag('active', 'Frozen') : tag('draft', 'Draft'),
  ]));
  const dl = el('dl', { class: 'defs' });
  const add = (k, v) => { if (v === null || v === undefined || v === '') return; dl.append(el('dt', { text: k }), el('dd', { text: String(v) })); };
  add('Template', n.template);
  add('Issued', n.issued_at ? fmtDate(n.issued_at) : 'Not issued');
  add('Delivery', n.delivery_method ? `${vlabel('delivery_methods', n.delivery_method)} on ${fmtDate(n.delivered_at)}` : null);
  add('Response due', n.response_due_on ? fmtDay(n.response_due_on) : null);
  add('Response received', n.response_received_at ? fmtDate(n.response_received_at) : null);
  add('Withdrawn', n.withdrawn_at ? `${fmtDate(n.withdrawn_at)} — ${n.withdrawn_reason || ''}` : null);
  wrap.appendChild(dl);

  if (n.response_summary) wrap.appendChild(el('div', { class: 'notice notice-info' }, [el('p', {}, [el('strong', { text: 'Response: ' }), document.createTextNode(n.response_summary)])]));

  // Frozen notice text shown verbatim.
  if (n.rendered_body) {
    wrap.appendChild(el('details', {}, [
      el('summary', { text: n.is_frozen ? 'View frozen notice text' : 'View draft notice text' }),
      el('div', { class: 'ocr-raw mt-half', text: n.rendered_body }),
      n.body_sha256 ? el('p', { class: 'small muted', text: `Body hash: ${n.body_sha256}` }) : null,
    ]));
  }

  if (can('case.manage_notices')) {
    const row = el('div', { class: 'btn-row mt-half' });
    if (!n.issued_at) { const b = el('button', { class: 'btn btn-sm', type: 'button', text: 'Issue' }); b.addEventListener('click', () => issueNotice(n)); row.appendChild(b); }
    if (n.issued_at && !n.delivered_at) { const b = el('button', { class: 'btn btn-sm', type: 'button', text: 'Record delivery' }); b.addEventListener('click', () => deliverNotice(n)); row.appendChild(b); }
    if (n.issued_at && !n.response_received_at && !n.withdrawn_at) { const b = el('button', { class: 'btn btn-sm', type: 'button', text: 'Record response' }); b.addEventListener('click', () => respondNotice(n)); row.appendChild(b); }
    if (!n.withdrawn_at) { const b = el('button', { class: 'btn btn-sm btn-danger', type: 'button', text: 'Withdraw' }); b.addEventListener('click', () => withdrawNotice(n)); row.appendChild(b); }
    wrap.appendChild(row);
  }
  return wrap;
}

function renderTransitions(box) {
  const transitions = kase.available_transitions || [];
  if (!transitions.length) return;
  const sec = el('section', { class: 'panel' }, [el('h2', { text: 'State' })]);
  const row = el('div', { class: 'transition-list' });
  for (const t of transitions) {
    const blocked = Array.isArray(t.blocked_by) && t.blocked_by.length > 0;
    const btn = el('button', { class: `btn btn-sm ${blocked ? 'blocked' : ''}`, type: 'button', disabled: blocked, 'aria-disabled': String(blocked), text: t.label || labelize(t.target_state) });
    if (!blocked) btn.addEventListener('click', () => doTransition(t));
    const w = el('div', {}, [btn]);
    if (blocked) w.appendChild(el('span', { class: 'small muted', text: `Blocked: ${t.blocked_by.join('; ')}` }));
    row.appendChild(w);
  }
  sec.appendChild(row);
  box.appendChild(sec);
}

function renderActions(box) {
  const sec = el('section', { class: 'panel' }, [el('h2', { text: 'Actions' })]);
  const row = el('div', { class: 'btn-row' });
  let any = false;
  if (can('case.record_outcome') || can('case.manage')) { any = true; const b = el('button', { class: 'btn', type: 'button', text: 'Record outcome' }); b.addEventListener('click', recordOutcome); row.appendChild(b); }
  if (can('case.manage')) { any = true; const b = el('button', { class: 'btn', type: 'button', text: 'Add event' }); b.addEventListener('click', addEvent); row.appendChild(b); }
  if (!any) sec.appendChild(el('p', { class: 'muted', text: 'You do not have permission to act on this case.' }));
  else sec.appendChild(row);
  box.appendChild(sec);
}

function fieldEl(labelText, control) { return el('div', { class: 'field' }, [el('label', { for: control.id, text: labelText }), control]); }

function submitDialog(title, fields, onSubmit, submitLabel = 'Save') {
  const form = el('form', {});
  const status = el('div', { class: 'status-region', 'aria-live': 'polite' });
  form.appendChild(status);
  fields.forEach((f) => form.appendChild(f));
  const dlg = openDialog({
    title, body: form,
    actions: [
      { label: 'Cancel', value: null },
      { label: submitLabel, class: 'btn-primary', close: false, onClick: async () => {
        setStatus(status, '');
        try { await onSubmit(); dlg.close(); setStatus($('#page-status'), 'Saved.', 'ok'); await load(); }
        catch (err) {
          if (err instanceof ApiError && err.status === 409 && err.code === 'stale_record') setStatus(status, 'This record changed after you loaded it. Close and reload.', 'error');
          else setStatus(status, err instanceof ApiError ? err.message : 'Action failed.', 'error');
        }
        return false;
      } },
    ],
  });
}

function prepareNotice() {
  const typeSel = el('select', { id: 'n-type' }); fillSelect(typeSel, vocab.notice_types, {});
  const days = el('input', { type: 'number', id: 'n-days', min: '1', max: '180', value: '15' });
  const sig = el('textarea', { id: 'n-sig', minlength: '5', maxlength: '500' });
  submitDialog('Prepare notice', [
    fieldEl('Notice type', typeSel),
    fieldEl('Response days', days),
    fieldEl('Signature block (name and designation of issuing officer)', sig),
  ], async () => {
    if (sig.value.trim().length < 5) throw new ApiError(422, { error: { message: 'Signature block is required.' } });
    await api.post(`/cases/${id}/notices`, { notice_type: typeSel.value, response_days: Number(days.value), signature_block: sig.value.trim() });
  }, 'Prepare');
}

function issueNotice(n) {
  const due = el('input', { type: 'date', id: 'i-due', value: n.response_due_on || '' });
  submitDialog(`Issue ${n.reference}`, [fieldEl('Response due on', due)], async () => {
    if (!due.value) throw new ApiError(422, { error: { message: 'Set the response due date.' } });
    await api.post(`/cases/${id}/notices/${n.id}/issue`, { response_due_on: due.value, expected_version: kase.version });
  }, 'Issue');
}

function deliverNotice(n) {
  const method = el('select', { id: 'd-method' }); fillSelect(method, vocab.delivery_methods, {});
  const at = el('input', { type: 'datetime-local', id: 'd-at' });
  const proof = el('input', { type: 'text', id: 'd-proof', maxlength: '160' });
  const note = el('textarea', { id: 'd-note', maxlength: '2000' });
  submitDialog(`Record delivery of ${n.reference}`, [
    fieldEl('Delivery method', method), fieldEl('Delivered at', at), fieldEl('Proof reference', proof), fieldEl('Note', note),
  ], async () => {
    if (!at.value) throw new ApiError(422, { error: { message: 'Set the delivery time.' } });
    await api.post(`/cases/${id}/notices/${n.id}/delivery`, {
      method: method.value, delivered_at: new Date(at.value).toISOString(),
      proof_reference: proof.value.trim() || undefined, note: note.value.trim() || undefined,
      expected_version: kase.version,
    });
  }, 'Record delivery');
}

function respondNotice(n) {
  const summary = el('textarea', { id: 'r-summary', minlength: '10', maxlength: '5000' });
  const at = el('input', { type: 'datetime-local', id: 'r-at' });
  submitDialog(`Record response to ${n.reference}`, [fieldEl('Response summary', summary), fieldEl('Received at', at)], async () => {
    if (summary.value.trim().length < 10 || !at.value) throw new ApiError(422, { error: { message: 'Summary (min 10 chars) and received time are required.' } });
    await api.post(`/cases/${id}/notices/${n.id}/response`, { summary: summary.value.trim(), received_at: new Date(at.value).toISOString(), expected_version: kase.version });
  }, 'Record response');
}

async function withdrawNotice(n) {
  const reason = await reasonDialog(`Withdraw ${n.reference}`, { label: 'Reason', minLength: 1, confirmLabel: 'Withdraw', danger: true });
  if (!reason) return;
  try { await api.post(`/cases/${id}/notices/${n.id}/withdraw?reason=${encodeURIComponent(reason)}`); setStatus($('#page-status'), 'Notice withdrawn.', 'ok'); await load(); }
  catch (err) { setStatus($('#page-status'), err instanceof ApiError ? err.message : 'Could not withdraw the notice.', 'error'); }
}

async function doTransition(t) {
  let reason;
  if (t.reason_required) { reason = await reasonDialog(`Transition to ${t.label}`, { label: 'Reason', minLength: 1, confirmLabel: 'Confirm' }); if (reason === null) return; }
  try { await api.post(`/cases/${id}/transitions`, { target_state: t.target_state, reason: reason || undefined, expected_version: kase.version }); setStatus($('#page-status'), `Moved to ${t.label}.`, 'ok'); await load(); }
  catch (err) {
    if (err instanceof ApiError && err.status === 409 && err.code === 'stale_record') setStatus($('#page-status'), 'This record changed after you loaded it. Reload the page.', 'error');
    else setStatus($('#page-status'), err instanceof ApiError ? err.message : 'Transition failed.', 'error');
  }
}

function recordOutcome() {
  const outcome = el('input', { type: 'text', id: 'o-outcome', minlength: '3', maxlength: '60' });
  const note = el('textarea', { id: 'o-note', minlength: '10', maxlength: '2000' });
  submitDialog('Record case outcome', [fieldEl('Outcome', outcome), fieldEl('Outcome note', note)], async () => {
    if (outcome.value.trim().length < 3 || note.value.trim().length < 10) throw new ApiError(422, { error: { message: 'Outcome (min 3) and note (min 10) are required.' } });
    await api.post(`/cases/${id}/outcome`, { outcome: outcome.value.trim(), outcome_note: note.value.trim(), expected_version: kase.version });
  }, 'Record outcome');
}

function addEvent() {
  const type = el('input', { type: 'text', id: 'e-type', minlength: '3', maxlength: '40' });
  const summary = el('textarea', { id: 'e-summary', minlength: '5', maxlength: '2000' });
  const when = el('input', { type: 'datetime-local', id: 'e-when' });
  submitDialog('Add case event', [fieldEl('Event type', type), fieldEl('Summary', summary), fieldEl('Scheduled for (optional)', when)], async () => {
    if (type.value.trim().length < 3 || summary.value.trim().length < 5) throw new ApiError(422, { error: { message: 'Event type (min 3) and summary (min 5) are required.' } });
    const payload = { event_type: type.value.trim(), summary: summary.value.trim() };
    if (when.value) payload.scheduled_for = new Date(when.value).toISOString();
    await api.post(`/cases/${id}/events`, payload);
  }, 'Add event');
}

main();
