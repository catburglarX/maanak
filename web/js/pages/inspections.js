// Inspections register with search/filter/pagination and a create dialog.
import { requireAuth, can } from '../workspace.js';
import { mountAppChrome, createRegister } from '../layout.js';
import { api, ApiError } from '../api.js';
import { $, el, fillSelect, tag, fmtDay, applyFieldErrors, clearFieldErrors, setStatus, productLabel, NOT_RECORDED } from '../util.js';
import { getVocabulary, labelFor } from '../chrome.js';
import { openDialog } from '../dialog.js';

let vocab = null;

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
    emptyTitle: 'No inspections',
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
      { label: 'Premises', render: (it) => it.premises_name || NOT_RECORDED },
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

// The API requires a product on every inspection: either an existing product_id or
// enough detail to create one. This dialog previously collected neither and told the
// officer "A product record can be added and completed on the inspection screen",
// which is not true, so Create always came back with a validation error. It now offers
// both routes.
async function loadProductOptions(select) {
  try {
    const res = await api.get('/products', { page_size: 100, sort: 'updated_desc' });
    const items = res.items || [];
    for (const p of items) {
      select.appendChild(el('option', { value: p.id, text: productLabel(p) }));
    }
    return items.length;
  } catch {
    return 0;
  }
}

function openCreateDialog(register) {
  const form = el('form', { id: 'create-inspection', novalidate: true });
  const status = el('div', { class: 'status-region', 'aria-live': 'polite' });
  const today = new Date().toISOString().slice(0, 10);
  form.appendChild(status);
  form.appendChild(field('inspection_date', 'Inspection date', el('input', { type: 'date', id: 'ci-date', name: 'inspection_date', required: true, 'aria-required': 'true', value: today, max: today }), true));
  const sourceSel = el('select', { id: 'ci-source', name: 'source' });
  fillSelect(sourceSel, vocab.inspection_sources, { blank: 'Select source…' });
  form.appendChild(field('source', 'Source', sourceSel));

  // Product: pick one already on record, or describe a new one.
  const productSel = el('select', { id: 'ci-product', name: 'product_id' });
  productSel.appendChild(el('option', { value: '', text: 'Record a new product' }));
  form.appendChild(field('product_id', 'Product', productSel, true));
  const hint = el('p', { class: 'hint', text: 'Loading the products already on record…' });
  form.appendChild(hint);

  const newProduct = el('fieldset', { id: 'ci-new-product' }, [
    el('legend', { text: 'New product' }),
    field('brand', 'Brand', el('input', { type: 'text', id: 'ci-brand', name: 'brand', maxlength: '160' }), true),
    field('name', 'Product name', el('input', { type: 'text', id: 'ci-name', name: 'name', maxlength: '240' }), true),
    field('commodity_category', 'Commodity category', el('input', { type: 'text', id: 'ci-cat', name: 'commodity_category', maxlength: '120', placeholder: 'for example: salt, biscuits, edible oil' }), true),
  ]);
  form.appendChild(newProduct);
  productSel.addEventListener('change', () => { newProduct.hidden = productSel.value !== ''; });

  form.appendChild(field('premises_name', 'Premises name', el('input', { type: 'text', id: 'ci-prem', name: 'premises_name', maxlength: '240' })));
  form.appendChild(field('jurisdiction_code', 'Jurisdiction code', el('input', { type: 'text', id: 'ci-jcode', name: 'jurisdiction_code' })));
  form.appendChild(field('jurisdiction_name', 'Jurisdiction name', el('input', { type: 'text', id: 'ci-jname', name: 'jurisdiction_name' })));
  form.appendChild(el('p', { class: 'hint', text: 'Jurisdiction defaults to your own. The remaining product detail, the evidence and the readings are added on the inspection screen.' }));

  loadProductOptions(productSel).then((count) => {
    hint.textContent = count
      ? `${count} product ${count === 1 ? 'record is' : 'records are'} already on file. Choose one to inspect another package of the same product.`
      : 'No product records yet, so describe this one below.';
  });

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
          const value = (name) => {
            const control = form.elements[name];
            return control && control.value.trim() ? control.value.trim() : '';
          };
          const payload = { inspection_date: value('inspection_date') };
          for (const k of ['source', 'premises_name', 'jurisdiction_code', 'jurisdiction_name']) {
            if (value(k)) payload[k] = value(k);
          }
          if (productSel.value) {
            payload.product_id = productSel.value;
          } else {
            const missing = ['brand', 'name', 'commodity_category'].filter((k) => !value(k));
            if (missing.length) {
              setStatus(status, 'Give the brand, the product name and the commodity category, or choose a product already on record.', 'error');
              form.elements[missing[0]].focus();
              return false;
            }
            payload.product = {
              brand: value('brand'),
              name: value('name'),
              commodity_category: value('commodity_category'),
            };
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
