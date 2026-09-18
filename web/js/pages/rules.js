// Rules governance list.
import { requireAuth, can } from '../workspace.js';
import { mountAppChrome, createRegister } from '../layout.js';
import { api, ApiError } from '../api.js';
import { $, el, tag, fmtDay, setStatus, NOT_RECORDED } from '../util.js';
import { getVocabulary, labelFor } from '../chrome.js';
import { confirmDialog } from '../dialog.js';

let vocab = null;

async function main() {
  mountAppChrome();
  const me = await requireAuth();
  if (!me) return;
  vocab = await getVocabulary();
  const sel = $('#f-status');
  sel.appendChild(el('option', { value: '', text: 'Any status' }));
  for (const o of vocab.rule_statuses) sel.appendChild(el('option', { value: o.value, text: o.label }));

  const register = createRegister($('#register'), {
    endpoint: '/rules',
    caption: 'Rule versions',
    emptyTitle: 'No rules',
    emptyDetail: 'No rules match your filters. Seed the baseline rules to begin.',
    params: () => ({ search: $('#f-search').value.trim(), status: $('#f-status').value, effective_only: $('#f-effective').checked ? true : '' }),
    rowHref: (r) => `/app/rule.html?id=${encodeURIComponent(r.id)}`,
    columns: [
      { label: 'Code', render: (r) => `${r.code} v${r.version}` },
      { label: 'Title', render: (r) => r.title || NOT_RECORDED },
      { label: 'Status', render: (r) => tag(r.status, labelFor(vocab.rule_statuses, r.status)) },
      { label: 'Legal authority', render: (r) => r.legal_authority_confirmed ? tag('confirmed', 'Confirmed') : tag('unable_to_determine', 'Not confirmed') },
      { label: 'Effective', render: (r) => r.effective_for_inspections ? 'Yes' : 'No' },
      { label: 'From', render: (r) => fmtDay(r.effective_from) },
      { label: 'Updated', render: (r) => fmtDay(r.updated_at) },
    ],
  });
  $('#filters').addEventListener('submit', (e) => { e.preventDefault(); register.reload(); });
  register.reload();

  const seedBtn = $('#seed-rules');
  if (can('rule.create')) {
    seedBtn.hidden = false;
    seedBtn.addEventListener('click', async () => {
      const ok = await confirmDialog('Seed baseline rules', 'This creates the baseline set of draft rules if they do not exist. Continue?', { confirmLabel: 'Seed' });
      if (!ok) return;
      seedBtn.disabled = true;
      try { const res = await api.post('/rules/seed', {}); setStatus($('#page-status'), res && res.message ? res.message : 'Baseline rules seeded.', 'ok'); register.reload(); }
      catch (err) { setStatus($('#page-status'), err instanceof ApiError ? err.message : 'Seeding failed.', 'error'); }
      finally { seedBtn.disabled = false; }
    });
  }
}

main();
