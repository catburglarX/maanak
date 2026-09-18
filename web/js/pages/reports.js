// Reports register: list, open documents, verification link, withdraw.
import { requireAuth, can } from '../workspace.js';
import { mountAppChrome, createRegister } from '../layout.js';
import { api, ApiError } from '../api.js';
import { $, el, tag, fmtDay, setStatus } from '../util.js';
import { getVocabulary, labelFor } from '../chrome.js';
import { reasonDialog } from '../dialog.js';

let vocab = null;
let register = null;

async function main() {
  mountAppChrome();
  const me = await requireAuth();
  if (!me) return;
  vocab = await getVocabulary();
  register = createRegister($('#register'), {
    endpoint: '/reports',
    caption: 'Issued reports',
    emptyTitle: 'No reports',
    emptyDetail: 'Reports produced from decided inspections will appear here.',
    columns: [
      { label: 'Reference', render: (r) => el('span', {}, [document.createTextNode(`${r.reference} `), el('span', { class: 'small muted', text: `rev ${r.revision}` })]) },
      { label: 'State', render: (r) => tag(r.state, labelFor(vocab.report_states, r.state)) },
      { label: 'Inspection', render: (r) => r.inspection_id ? el('a', { href: `/app/inspection.html?id=${encodeURIComponent(r.inspection_id)}`, text: r.inspection_reference || 'View' }) : '—' },
      { label: 'Product', render: (r) => r.product || '—' },
      { label: 'Issued', render: (r) => fmtDay(r.issued_at) },
      { label: 'Documents', render: (r) => renderDocs(r) },
    ],
  });
  register.reload();
}

function renderDocs(r) {
  const wrap = el('div', { class: 'btn-row' });
  wrap.appendChild(el('a', { class: 'btn btn-sm', href: `/api/v1/reports/${r.id}/document.pdf`, target: '_blank', rel: 'noopener', text: 'PDF' }));
  wrap.appendChild(el('a', { class: 'btn btn-sm', href: `/api/v1/reports/${r.id}/document.docx`, target: '_blank', rel: 'noopener', text: 'DOCX' }));
  if (r.verification_url) wrap.appendChild(el('a', { class: 'btn btn-sm', href: r.verification_url, target: '_blank', rel: 'noopener', text: 'Verify' }));
  if (can('report.withdraw') && r.state === 'issued') {
    const w = el('button', { class: 'btn btn-sm btn-danger', type: 'button', text: 'Withdraw' });
    w.addEventListener('click', () => withdraw(r));
    wrap.appendChild(w);
  }
  return wrap;
}

async function withdraw(r) {
  const reason = await reasonDialog(`Withdraw ${r.reference}`, { label: 'Reason for withdrawal', minLength: 1, confirmLabel: 'Withdraw', danger: true });
  if (!reason) return;
  try {
    await api.post(`/api/v1/reports/${r.id}/withdraw?reason=${encodeURIComponent(reason)}`);
    setStatus($('#page-status'), 'Report withdrawn.', 'ok');
    register.reload();
  } catch (err) { setStatus($('#page-status'), err instanceof ApiError ? err.message : 'Could not withdraw the report.', 'error'); }
}

main();
