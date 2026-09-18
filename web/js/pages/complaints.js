// Complaint triage queue — priority ordered, shows priority reason.
import { requireAuth } from '../workspace.js';
import { mountAppChrome, createRegister } from '../layout.js';
import { $, el, tag, fmtDay } from '../util.js';
import { getVocabulary, labelFor } from '../chrome.js';

let vocab = null;

async function main() {
  mountAppChrome();
  const me = await requireAuth();
  if (!me) return;
  vocab = await getVocabulary();
  fillState();
  const register = createRegister($('#register'), {
    endpoint: '/complaints',
    caption: 'Complaints by priority',
    emptyTitle: 'No complaints',
    emptyDetail: 'Complaints submitted by the public will appear here, highest priority first.',
    params: () => ({
      search: $('#f-search').value.trim(),
      state: $('#f-state').value,
      unassigned: $('#f-unassigned').checked ? true : '',
      mine: $('#f-mine').checked ? true : '',
    }),
    rowHref: (it) => `/app/complaint.html?id=${encodeURIComponent(it.id)}`,
    columns: [
      { label: 'Reference', render: (it) => it.reference },
      { label: 'Priority', render: (it) => el('span', {}, [
        el('strong', { text: String(it.priority ?? 0) }),
        it.priority_reason ? el('span', { class: 'small muted', text: ` — ${it.priority_reason}` }) : null,
      ]) },
      { label: 'State', render: (it) => tag(it.state, labelFor(vocab.complaint_states, it.state)) },
      { label: 'Category', render: (it) => labelFor(vocab.complaint_categories, it.category) },
      { label: 'Product', render: (it) => it.product_name || '—' },
      { label: 'Received', render: (it) => fmtDay(it.created_at) },
      { label: 'Assigned', render: (it) => it.assigned_officer_id ? 'Yes' : 'Unassigned' },
    ],
  });
  $('#filters').addEventListener('submit', (e) => { e.preventDefault(); register.reload(); });
  register.reload();
}

function fillState() {
  const sel = $('#f-state');
  sel.appendChild(el('option', { value: '', text: 'Any state' }));
  for (const o of vocab.complaint_states) sel.appendChild(el('option', { value: o.value, text: o.label }));
}

main();
