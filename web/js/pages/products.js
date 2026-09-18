// Products register with create dialog.
import { requireAuth, can } from '../workspace.js';
import { mountAppChrome, createRegister } from '../layout.js';
import { api, ApiError } from '../api.js';
import { $, el, fmtDay, labelize, fillSelect, setStatus, applyFieldErrors, clearFieldErrors, NOT_RECORDED } from '../util.js';
import { getVocabulary } from '../chrome.js';
import { openDialog } from '../dialog.js';

let vocab = null;

async function main() {
  mountAppChrome();
  const me = await requireAuth();
  if (!me) return;
  vocab = await getVocabulary();

  const register = createRegister($('#register'), {
    endpoint: '/products',
    caption: 'Product records',
    emptyTitle: 'No products',
    emptyDetail: 'Product records created during inspections will appear here.',
    params: () => ({ search: $('#f-search').value.trim(), barcode: $('#f-barcode').value.trim() }),
    rowHref: () => '#',
    columns: [
      { label: 'Brand', render: (p) => p.brand || NOT_RECORDED },
      { label: 'Name', render: (p) => p.name || NOT_RECORDED },
      { label: 'Category', render: (p) => labelize(p.commodity_category) },
      { label: 'Net quantity', render: (p) => p.declared_net_quantity ? `${p.declared_net_quantity} ${p.declared_net_quantity_unit || ''}`.trim() : NOT_RECORDED },
      { label: 'GTIN', render: (p) => p.primary_gtin || NOT_RECORDED },
      { label: 'Inspections', render: (p) => String(p.inspection_count ?? 0) },
      { label: 'Updated', render: (p) => fmtDay(p.updated_at) },
    ],
  });
  // First column should not be a link (products have no dedicated page); render plain.
  $('#filters').addEventListener('submit', (e) => { e.preventDefault(); register.reload(); });
  register.reload();

  const btn = $('#new-product');
  if (can('product.create')) { btn.hidden = false; btn.addEventListener('click', () => openCreate(register)); }
}

function fieldEl(name, label, control, required) {
  control.id = `p-${name}`; control.name = name;
  return el('div', { class: 'field' }, [
    el('label', { for: control.id }, [label, required ? el('span', { class: 'req', 'aria-hidden': 'true', text: ' *' }) : null]),
    control,
  ]);
}

function openCreate(register) {
  const form = el('form', { novalidate: true });
  const status = el('div', { class: 'status-region', 'aria-live': 'polite' });
  const catSel = el('input', { type: 'text' }); // free text category
  const pkgSel = el('select', {}); fillSelect(pkgSel, vocab.package_types, { blank: 'Select…' });
  const qkSel = el('select', {}); fillSelect(qkSel, vocab.quantity_kinds, { blank: 'Select…' });
  const foodChk = el('input', { type: 'checkbox' }); foodChk.id = 'p-is_food'; foodChk.name = 'is_food';
  form.append(
    status,
    fieldEl('brand', 'Brand', el('input', { type: 'text', maxlength: '160' }), true),
    fieldEl('name', 'Name', el('input', { type: 'text', maxlength: '240' }), true),
    fieldEl('commodity_category', 'Commodity category', catSel, true),
    fieldEl('common_generic_name', 'Common / generic name', el('input', { type: 'text' })),
    fieldEl('package_type', 'Package type', pkgSel),
    fieldEl('quantity_kind', 'Quantity kind', qkSel),
    fieldEl('declared_net_quantity', 'Declared net quantity', el('input', { type: 'number', step: 'any', min: '0' })),
    fieldEl('declared_net_quantity_unit', 'Net quantity unit', el('input', { type: 'text', maxlength: '16' })),
    el('div', { class: 'field checkbox-row' }, [foodChk, el('label', { for: 'p-is_food', text: 'This is a food product' })]),
  );
  const dlg = openDialog({
    title: 'New product', body: form,
    actions: [
      { label: 'Cancel', value: null },
      { label: 'Create', class: 'btn-primary', close: false, onClick: async () => {
        clearFieldErrors(form); setStatus(status, '');
        const g = (n) => { const e = form.elements[n]; return e && e.value.trim() ? e.value.trim() : undefined; };
        const payload = {
          brand: g('brand'), name: g('name'), commodity_category: g('commodity_category'),
          common_generic_name: g('common_generic_name'),
          package_type: g('package_type'), quantity_kind: g('quantity_kind'),
          declared_net_quantity: g('declared_net_quantity') ? Number(g('declared_net_quantity')) : undefined,
          declared_net_quantity_unit: g('declared_net_quantity_unit'),
          is_food: foodChk.checked,
        };
        Object.keys(payload).forEach((k) => payload[k] === undefined && delete payload[k]);
        try { await api.post('/products', payload); dlg.close(); register.reload(); }
        catch (err) { if (err instanceof ApiError && err.status === 422) applyFieldErrors(form, err); setStatus(status, err instanceof ApiError ? err.message : 'Could not create product.', 'error'); }
        return false;
      } },
    ],
  });
}

main();
