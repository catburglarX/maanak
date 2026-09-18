// Inspections register with search/filter/pagination and a create dialog.
import { requireAuth, can } from '../workspace.js';
import { mountAppChrome, createRegister } from '../layout.js';
import { api, ApiError } from '../api.js';
import { $, el, fillSelect, tag, fmtDay, applyFieldErrors, clearFieldErrors, setStatus } from '../util.js';
import { getVocabulary, labelFor } from '../chrome.js';
import { openDialog } from '../dialog.js';

let vocab = null;

function productLabel(p) {
  if (!p) return '—';
  return [p.brand, p.name].filter(Boolean).join(' — ') || '—';
}

async function main() {
  mountAppChrome();
  const me = await requireAuth();
  if (!me) return;
  vocab = await getVocabulary();

  fillSelect($('#f-state'), vocab.inspection_states, { blank: 'Any state' });

  const filters = $('#filters');
  const register = createRegister($('#register'), {
    endpoint: '/inspections',
    caption: 'Inspection register',
    emptyTitle: 'No inspections found',
    emptyDetail: 'Adjust your filters, or create a new inspection to begin.',
    params: () => ({
      search: $('#f-search').value.trim(),
      state: $('#f-state').value,
      decided: $('#f-decided').value,
      assigned_to_me: $('#f-mine').checked ? true : '',
      sort: 'updated_desc',
    }),
    rowHref: (it) => `/app/inspection.html?id=${encodeURIComponent(it.id)}`,
    columns: [
      { label: 'Reference', render: (it) => it.reference },
      { label: 'State', render: (it) => tag(it.state, labelFor(vocab.inspection_states, it.state)) },
      { label: 'Product', render: (it) => productLabel(it.product) },
      { label: 'Premises', render: (it) => it.premises_name || '—' },
      { label: 'Source', render: (it) => labelFor(vocab.inspection_sources, it.source) },
      { label: 'Updated', render: (it) => fmtDay(it.updated_at) },
    ],
  });

  filters.addEventListener('submit', (e) => { e.preventDefault(); register.reload(); });
  register.reload();

  const newBtn = $('#new-inspection');
  if (can('inspection.create')) {
    newBtn.hidden = false;
    newBtn.addEventListener('click', () => openCreateDialog(register));
  }
}

function openCreateDialog(register) {
  const form = el('form', { id: 'create-inspection', novalidate: true });
  const status = el('div', { class: 'status-region', 'aria-live': 'polite' });
  const today = new Date().toISOString().slice(0, 10);
  form.appendChild(status);
  form.appendChild(field('inspection_date', 'Inspection date', el('input', { type: 'date', id: 'ci-date', name: 'inspection_date', required: true, 'aria-required': 'true', value: today }), true));
  const sourceSel = el('select', { id: 'ci-source', name: 'source' });
  fillSelect(sourceSel, vocab.inspection_sources, { blank: 'Select source…' });
  form.appendChild(field('source', 'Source', sourceSel));
  form.appendChild(field('premises_name', 'Premises name', el('input', { type: 'text', id: 'ci-prem', name: 'premises_name', maxlength: '240' })));
  form.appendChild(field('jurisdiction_code', 'Jurisdiction code', el('input', { type: 'text', id: 'ci-jcode', name: 'jurisdiction_code' })));
  form.appendChild(field('jurisdiction_name', 'Jurisdiction name', el('input', { type: 'text', id: 'ci-jname', name: 'jurisdiction_name' })));
  form.appendChild(el('p', { class: 'hint', text: 'A product record can be added and completed on the inspection screen.' }));

  const dlg = openDialog({
    title: 'New inspection',
    body: form,
    actions: [
      { label: 'Cancel', value: null },
      {
        label: 'Create', class: 'btn-primary', close: false,
        onClick: async () => {
          clearFieldErrors(form);
          setStatus(status, '');
          const payload = { inspection_date: form.elements['inspection_date'].value };
          for (const k of ['source', 'premises_name', 'jurisdiction_code', 'jurisdiction_name']) {
            const v = form.elements[k].value.trim();
            if (v) payload[k] = v;
          }
          try {
            const res = await api.post('/inspections', payload);
            location.href = `/app/inspection.html?id=${encodeURIComponent(res.id)}`;
          } catch (err) {
            if (err instanceof ApiError && err.status === 422) applyFieldErrors(form, err);
            setStatus(status, err instanceof ApiError ? err.message : 'Could not create the inspection.', 'error');
          }
          return false;
        },
      },
    ],
  });
}

function field(name, label, control, required) {
  control.id = control.id || `f-${name}`;
  return el('div', { class: 'field' }, [
    el('label', { for: control.id }, [label, required ? el('span', { class: 'req', 'aria-hidden': 'true', text: ' *' }) : null]),
    control,
  ]);
}

main();
