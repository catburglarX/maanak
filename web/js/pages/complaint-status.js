// Public complaint status lookup.
import { api, ApiError } from '../api.js';
import { $, el, clear, setStatus, fmtDate, tag, NOT_RECORDED } from '../util.js';
import { getVocabulary, labelFor } from '../chrome.js';

const form = $('#status-form');
const statusRegion = $('#form-status');
const result = $('#result');
let vocab = null;

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  setStatus(statusRegion, '');
  clear(result);
  const reference = form.elements['reference'].value.trim();
  const contact = form.elements['contact'].value.trim();
  const btn = $('#lookup-btn');
  btn.disabled = true; btn.textContent = 'Looking up…';
  try {
    if (!vocab) vocab = await getVocabulary();
    const res = await api.get('/public/complaints/status', { reference, contact });
    render(res);
  } catch (err) {
    setStatus(statusRegion, err instanceof ApiError ? err.message : 'Lookup failed. Please try again.', 'error');
  } finally {
    btn.disabled = false; btn.textContent = 'Look up';
  }
});

function render(res) {
  clear(result);
  if (!res.found) {
    result.appendChild(el('div', { class: 'notice notice-warn', role: 'status' }, [
      el('p', { text: res.detail || 'No complaint matches that reference and contact detail.' }),
    ]));
    return;
  }
  const dl = el('dl', { class: 'defs' });
  const add = (k, v) => { dl.appendChild(el('dt', { text: k })); dl.appendChild(el('dd', {}, [typeof v === 'string' ? document.createTextNode(v) : v])); };
  add('Reference', el('span', { class: 'mono', text: res.reference }));
  add('Status', tag(res.state, labelFor(vocab.complaint_states, res.state)));
  if (res.state_description) add('What this means', res.state_description);
  add('Category', labelFor(vocab.complaint_categories, res.category));
  add('Product', res.product_name || NOT_RECORDED);
  add('Submitted', fmtDate(res.submitted_at));
  if (res.resolved_at) add('Resolved', fmtDate(res.resolved_at));
  if (res.resolution_note) add('Resolution note', res.resolution_note);
  if (res.closed_at) add('Closed', fmtDate(res.closed_at));

  const box = el('div', { class: 'panel-white' }, [el('h2', { text: 'Complaint status' }), dl]);
  if (res.note) box.appendChild(el('p', { class: 'small muted', text: res.note }));
  result.appendChild(box);
}
