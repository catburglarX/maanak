// Cases register.
import { requireAuth } from '../workspace.js';
import { mountAppChrome, createRegister } from '../layout.js';
import { $, el, tag, fmtDay, NOT_RECORDED } from '../util.js';
import { getVocabulary, labelFor } from '../chrome.js';

let vocab = null;

async function main() {
  mountAppChrome();
  const me = await requireAuth();
  if (!me) return;
  vocab = await getVocabulary();
  const sel = $('#f-state');
  sel.appendChild(el('option', { value: '', text: 'Any state' }));
  for (const o of vocab.case_states) sel.appendChild(el('option', { value: o.value, text: o.label }));

  const register = createRegister($('#register'), {
    endpoint: '/cases',
    caption: 'Enforcement cases',
    emptyTitle: 'No cases',
    emptyDetail: 'Cases opened from inspections will appear here.',
    params: () => ({ state: $('#f-state').value, overdue_only: $('#f-overdue').checked ? true : '' }),
    rowHref: (c) => `/app/case.html?id=${encodeURIComponent(c.id)}`,
    columns: [
      { label: 'Reference', render: (c) => c.reference },
      { label: 'State', render: (c) => tag(c.state, labelFor(vocab.case_states, c.state)) },
      { label: 'Respondent', render: (c) => c.respondent_name || NOT_RECORDED },
      { label: 'Subject', render: (c) => c.subject || NOT_RECORDED },
      { label: 'Notices', render: (c) => String((c.notices || []).length) },
      { label: 'Opened', render: (c) => fmtDay(c.created_at) },
    ],
  });
  $('#filters').addEventListener('submit', (e) => { e.preventDefault(); register.reload(); });
  register.reload();
}

main();
