// Complaint detail: view, triage, assign, transition, convert to inspection.
import { requireAuth, can } from '../workspace.js';
import { mountAppChrome } from '../layout.js';
import { api, ApiError } from '../api.js';
import { $, el, clear, tag, fmtDate, fmtDay, labelize, setStatus, showError, fillSelect } from '../util.js';
import { getVocabulary, labelFor } from '../chrome.js';
import { openDialog } from '../dialog.js';

const id = new URLSearchParams(location.search).get('id');
let vocab = null;
let complaint = null;

async function main() {
  mountAppChrome();
  const me = await requireAuth();
  if (!me) return;
  if (!id) { setStatus($('#page-status'), 'No complaint specified.', 'error'); return; }
  vocab = await getVocabulary();
  await load();
}

async function load() {
  try { complaint = await api.get(`/complaints/${id}`); }
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
      el('h1', { text: `Complaint ${complaint.reference}` }),
      el('div', { class: 'btn-row' }, [
        tag(complaint.state, vlabel('complaint_states', complaint.state)),
        el('span', { class: 'small' }, [el('strong', { text: `Priority ${complaint.priority ?? 0}` })]),
      ]),
    ]),
    el('a', { class: 'btn btn-sm', href: '/app/complaints.html', text: 'Back to queue' }),
  ]));
  if (complaint.state_description) box.appendChild(el('p', { class: 'muted', text: complaint.state_description }));
  if (complaint.priority_reason) box.appendChild(el('div', { class: 'notice notice-info' }, [el('p', {}, [el('strong', { text: 'Priority reason: ' }), document.createTextNode(complaint.priority_reason)])]));

  const dl = el('dl', { class: 'defs' });
  const add = (k, v) => { if (v === null || v === undefined || v === '') return; dl.appendChild(el('dt', { text: k })); dl.appendChild(el('dd', {}, [typeof v === 'object' ? v : document.createTextNode(String(v))])); };
  add('Category', vlabel('complaint_categories', complaint.category));
  add('Product', complaint.product_name);
  add('Brand', complaint.brand);
  add('Barcode', complaint.barcode_value);
  add('Description', complaint.description);
  add('Stated MRP', complaint.stated_mrp);
  add('Price paid', complaint.stated_price_paid);
  add('Seller', complaint.seller_name);
  add('Marketplace', complaint.marketplace_name);
  add('Listing', complaint.listing_url ? el('a', { href: complaint.listing_url, target: '_blank', rel: 'noopener noreferrer', text: complaint.listing_url }) : null);
  add('Location', complaint.location_text);
  add('Purchase date', complaint.purchase_date ? fmtDay(complaint.purchase_date) : null);
  add('Received', fmtDate(complaint.created_at));
  add('Jurisdiction', complaint.jurisdiction_name);
  add('Triage note', complaint.triage_note);
  add('Resolution note', complaint.resolution_note);
  box.appendChild(dl);

  // Contact (officers only, shown because API returned it)
  if (complaint.has_contact) {
    const c = el('div', { class: 'panel' }, [el('h2', { text: 'Complainant contact' })]);
    const cdl = el('dl', { class: 'defs' });
    const cadd = (k, v) => { if (!v) return; cdl.append(el('dt', { text: k }), el('dd', { text: v })); };
    cadd('Name', complaint.contact_name);
    cadd('Email', complaint.contact_email);
    cadd('Phone', complaint.contact_phone);
    cdl.append(el('dt', { text: 'Consent to contact' }), el('dd', { text: complaint.consent_to_contact ? 'Yes' : 'No' }));
    c.appendChild(cdl);
    box.appendChild(c);
  }

  renderAttachments(box);
  renderActions(box);
}

function renderAttachments(box) {
  const atts = complaint.attachments || [];
  const sec = el('section', {}, [el('h2', { text: `Attachments (${atts.length})` })]);
  if (!atts.length) { sec.appendChild(el('p', { class: 'muted', text: 'No attachments were submitted.' })); box.appendChild(sec); return; }
  const list = el('ul', {});
  for (const a of atts) {
    const li = el('li', {}, [
      document.createTextNode(`${labelize(a.kind)} — ${a.filename || 'file'} (${a.mime_type || ''}) `),
    ]);
    const btn = el('button', { class: 'btn btn-sm', type: 'button', text: 'View' });
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      try {
        const res = await api.get(`/complaints/${id}/attachments/${a.id}/view`);
        if (res && res.url) window.open(res.url, '_blank', 'noopener');
        else setStatus($('#page-status'), 'No viewable link was returned for this attachment.', 'warn');
      } catch (err) { setStatus($('#page-status'), err instanceof ApiError ? err.message : 'Could not open attachment.', 'error'); }
      finally { btn.disabled = false; }
    });
    li.appendChild(btn);
    list.appendChild(li);
  }
  sec.appendChild(list);
  box.appendChild(sec);
}

function renderActions(box) {
  const bar = el('section', { class: 'panel', 'aria-label': 'Actions' }, [el('h2', { text: 'Actions' })]);
  const row = el('div', { class: 'btn-row' });
  let any = false;

  if (can('complaint.triage')) { any = true; const b = el('button', { class: 'btn', type: 'button', text: 'Triage' }); b.addEventListener('click', triage); row.appendChild(b); }
  if (can('complaint.assign')) { any = true; const b = el('button', { class: 'btn', type: 'button', text: 'Assign officer' }); b.addEventListener('click', assign); row.appendChild(b); }
  if (can('complaint.transition') || can('complaint.triage')) { any = true; const b = el('button', { class: 'btn', type: 'button', text: 'Change state' }); b.addEventListener('click', transition); row.appendChild(b); }
  if (can('inspection.create')) { any = true; const b = el('button', { class: 'btn btn-primary', type: 'button', text: 'Convert to inspection' }); b.addEventListener('click', convert); row.appendChild(b); }

  if (!any) bar.appendChild(el('p', { class: 'muted', text: 'You do not have permission to act on this complaint.' }));
  else bar.appendChild(row);
  box.appendChild(bar);
}

function dialogForm(title, fields, onSubmit, submitLabel = 'Save') {
  const form = el('form', {});
  const status = el('div', { class: 'status-region', 'aria-live': 'polite' });
  form.appendChild(status);
  for (const f of fields) form.appendChild(f);
  const dlg = openDialog({
    title, body: form,
    actions: [
      { label: 'Cancel', value: null },
      { label: submitLabel, class: 'btn-primary', close: false, onClick: async () => {
        setStatus(status, '');
        try { await onSubmit(form); dlg.close(); await load(); }
        catch (err) {
          if (err instanceof ApiError && err.status === 409 && err.code === 'stale_record') setStatus(status, 'This record changed after you loaded it. Close and reload.', 'error');
          else setStatus(status, err instanceof ApiError ? err.message : 'Action failed.', 'error');
        }
        return false;
      } },
    ],
  });
  return dlg;
}

function fieldEl(labelText, control) {
  return el('div', { class: 'field' }, [el('label', { for: control.id, text: labelText }), control]);
}

function triage() {
  const prio = el('input', { type: 'number', id: 't-prio', min: '0', max: '100', value: String(complaint.priority ?? 0) });
  const jcode = el('input', { type: 'text', id: 't-jcode', value: complaint.jurisdiction_code || '' });
  const jname = el('input', { type: 'text', id: 't-jname', value: complaint.jurisdiction_name || '' });
  const note = el('textarea', { id: 't-note', maxlength: '2000' });
  dialogForm('Triage complaint', [
    fieldEl('Priority (0–100)', prio),
    fieldEl('Jurisdiction code', jcode),
    fieldEl('Jurisdiction name', jname),
    fieldEl('Triage note', note),
  ], async () => {
    const payload = { expected_version: complaint.version };
    if (prio.value !== '') payload.priority = Number(prio.value);
    if (jcode.value.trim()) payload.jurisdiction_code = jcode.value.trim();
    if (jname.value.trim()) payload.jurisdiction_name = jname.value.trim();
    if (note.value.trim()) payload.triage_note = note.value.trim();
    await api.post(`/complaints/${id}/triage`, payload);
  });
}

async function assign() {
  const sel = el('select', { id: 'a-officer' });
  sel.appendChild(el('option', { value: '', text: 'Loading officers…' }));
  dialogForm('Assign officer', [fieldEl('Officer', sel)], async () => {
    if (!sel.value) throw new ApiError(422, { error: { message: 'Select an officer.' } });
    await api.post(`/complaints/${id}/assign`, { officer_id: sel.value, expected_version: complaint.version });
  }, 'Assign');
  try {
    const res = await api.get('/users', { page: 1, page_size: 100, active: true });
    clear(sel);
    sel.appendChild(el('option', { value: '', text: 'Select officer…' }));
    for (const u of res.items || []) sel.appendChild(el('option', { value: u.id, text: `${u.name} — ${labelize(u.role)}` }));
  } catch { clear(sel); sel.appendChild(el('option', { value: '', text: 'Could not load officers' })); }
}

function transition() {
  const sel = el('select', { id: 'tr-state' });
  fillSelect(sel, vocab.complaint_states, { blank: 'Select target state…' });
  const reason = el('textarea', { id: 'tr-reason', maxlength: '2000' });
  dialogForm('Change complaint state', [fieldEl('Target state', sel), fieldEl('Reason', reason)], async () => {
    if (!sel.value) throw new ApiError(422, { error: { message: 'Select a state.' } });
    await api.post(`/complaints/${id}/transitions`, { target_state: sel.value, reason: reason.value.trim() || undefined, expected_version: complaint.version });
  }, 'Change state');
}

function convert() {
  const today = new Date().toISOString().slice(0, 10);
  const date = el('input', { type: 'date', id: 'cv-date', value: today, required: true });
  const cat = el('input', { type: 'text', id: 'cv-cat', value: 'unclassified' });
  const prem = el('input', { type: 'text', id: 'cv-prem' });
  const promote = el('input', { type: 'checkbox', id: 'cv-promote', checked: true });
  const promoteWrap = el('div', { class: 'field checkbox-row' }, [promote, el('label', { for: 'cv-promote', text: 'Copy the complainant\u2019s photographs into the inspection as evidence' })]);
  dialogForm('Convert to inspection', [
    fieldEl('Inspection date', date),
    fieldEl('Commodity category', cat),
    fieldEl('Premises name', prem),
    promoteWrap,
  ], async () => {
    const payload = {
      inspection_date: date.value,
      commodity_category: cat.value.trim() || 'unclassified',
      promote_attachments: promote.checked,
      expected_version: complaint.version,
    };
    if (prem.value.trim()) payload.premises_name = prem.value.trim();
    const res = await api.post(`/complaints/${id}/inspection`, payload);
    const insId = res && (res.id || (res.inspection && res.inspection.id));
    if (insId) location.href = `/app/inspection.html?id=${encodeURIComponent(insId)}`;
  }, 'Create inspection');
}

main();
