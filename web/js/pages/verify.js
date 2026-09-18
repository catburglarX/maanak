// Public report verification. Reads ?report= (and ?code=) from the query so a
// printed/QR URL works, and verifies content integrity honestly.
import { api, ApiError } from '../api.js';
import { $, el, clear, setStatus, fmtDate, tag } from '../util.js';

const form = $('#verify-form');
const statusRegion = $('#form-status');
const result = $('#result');

function prefillFromQuery() {
  const q = new URLSearchParams(location.search);
  const ref = q.get('report') || q.get('reference');
  const code = q.get('code');
  if (ref) form.elements['reference'].value = ref;
  if (code) form.elements['code'].value = code;
  if (ref || code) form.requestSubmit();
}

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  setStatus(statusRegion, '');
  clear(result);
  const reference = form.elements['reference'].value.trim();
  const code = form.elements['code'].value.trim();
  if (!reference && !code) {
    setStatus(statusRegion, 'Enter the report reference or the verification code.', 'error');
    return;
  }
  const btn = $('#verify-btn');
  btn.disabled = true; btn.textContent = 'Verifying…';
  try {
    const res = await api.get('/public/reports/verify', { reference, code });
    render(res);
  } catch (err) {
    setStatus(statusRegion, err instanceof ApiError ? err.message : 'Verification failed. Please try again.', 'error');
  } finally {
    btn.disabled = false; btn.textContent = 'Verify';
  }
});

function render(res) {
  clear(result);
  if (!res.found) {
    result.appendChild(el('div', { class: 'notice notice-warn', role: 'status' }, [
      el('p', { text: res.detail || 'No report matches what you entered.' }),
    ]));
    return;
  }
  const intact = res.content_intact === true;
  const banner = el('div', { class: intact ? 'notice notice-ok' : 'notice notice-error', role: 'status' }, [
    el('p', {}, [
      el('strong', { text: intact ? 'Content intact. ' : 'Content does NOT match the recorded hash. ' }),
      intact
        ? 'A report with this reference was issued and its stored content still matches the hash recorded when it was issued.'
        : 'The stored content no longer matches the hash recorded at issue. Treat this document with caution and contact the issuing workspace.',
    ]),
  ]);
  result.appendChild(banner);

  const dl = el('dl', { class: 'defs' });
  const add = (k, v) => { dl.appendChild(el('dt', { text: k })); dl.appendChild(el('dd', {}, [typeof v === 'string' ? document.createTextNode(v) : v])); };
  add('Report reference', el('span', { class: 'mono', text: res.report_reference || '—' }));
  if (res.verification_code) add('Verification code', el('span', { class: 'mono', text: res.verification_code }));
  add('Status', tag(res.state, res.state));
  if (res.revision !== undefined && res.revision !== null) add('Revision', String(res.revision));
  add('Issued', fmtDate(res.issued_at));
  add('Issuing workspace', res.issuing_workspace || '—');
  if (res.jurisdiction) add('Jurisdiction', res.jurisdiction);
  if (res.document_hash) add('Document hash', el('span', { class: 'mono small', text: `${res.hash_algorithm || 'hash'}: ${res.document_hash}` }));
  if (res.withdrawn_at) add('Withdrawn', fmtDate(res.withdrawn_at));
  if (res.superseded) add('Superseded', 'Yes — a newer revision exists');
  if (res.signature_status) add('Signature status', res.signature_status);

  const box = el('div', { class: 'panel-white' }, [el('h2', { text: 'Report verification' }), dl]);
  if (res.note) box.appendChild(el('p', { class: 'small muted', text: res.note }));
  if (res.not_a_government_service) box.appendChild(el('p', { class: 'small muted', text: res.not_a_government_service }));
  result.appendChild(box);
}

prefillFromQuery();
