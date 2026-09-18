// Public complaint submission: builds the multipart body, handles validation,
// and shows the reference prominently on success.
import { api, ApiError } from '../api.js';
import { $, el, clear, fillSelect, applyFieldErrors, clearFieldErrors, setStatus, fmtDay } from '../util.js';
import { getVocabulary } from '../chrome.js';

const form = $('#complaint-form');
const statusRegion = $('#form-status');
const successBox = $('#success');
const submitBtn = $('#submit-btn');

async function init() {
  try {
    const vocab = await getVocabulary();
    fillSelect($('#category'), vocab.complaint_categories, { blank: 'Select a problem type…' });
  } catch (err) {
    setStatus(statusRegion, 'Could not load the list of problem types. Please refresh.', 'error');
  }
}

function buildComplaintJson() {
  const g = (name) => {
    const v = form.elements[name] ? form.elements[name].value.trim() : '';
    return v === '' ? undefined : v;
  };
  const payload = {
    product_name: g('product_name'),
    brand: g('brand'),
    barcode_value: g('barcode_value'),
    category: g('category'),
    description: g('description'),
    purchase_date: g('purchase_date'),
    seller_name: g('seller_name'),
    marketplace_name: g('marketplace_name'),
    listing_url: g('listing_url'),
    stated_mrp: g('stated_mrp'),
    stated_price_paid: g('stated_price_paid'),
    location_text: g('location_text'),
    contact_name: g('contact_name'),
    contact_email: g('contact_email'),
    contact_phone: g('contact_phone'),
    consent_to_contact: form.elements['consent_to_contact'].checked,
    privacy_notice_acknowledged: form.elements['privacy_notice_acknowledged'].checked,
  };
  // Drop undefined keys so optional fields are truly omitted.
  Object.keys(payload).forEach((k) => payload[k] === undefined && delete payload[k]);
  return payload;
}

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  clearFieldErrors(form);
  setStatus(statusRegion, '');

  if (!form.elements['privacy_notice_acknowledged'].checked) {
    const wrap = form.elements['privacy_notice_acknowledged'].closest('.field');
    wrap.classList.add('field-error');
    wrap.appendChild(el('p', { class: 'field-msg', role: 'alert', text: 'You must acknowledge the privacy notice before submitting.' }));
    return;
  }

  const fd = new FormData();
  fd.append('complaint', JSON.stringify(buildComplaintJson()));
  const images = form.elements['package_images'].files;
  for (const f of images) fd.append('package_images', f);
  const proof = form.elements['purchase_proof'].files;
  if (proof && proof.length) fd.append('purchase_proof', proof[0]);

  submitBtn.disabled = true;
  submitBtn.textContent = 'Submitting…';
  try {
    const res = await api.postForm('/public/complaints', fd);
    showSuccess(res);
  } catch (err) {
    if (err instanceof ApiError && err.status === 422 && applyFieldErrors(form, err)) {
      setStatus(statusRegion, 'Please correct the highlighted fields.', 'error');
    } else {
      setStatus(statusRegion, err instanceof ApiError ? err.message : 'Could not submit your complaint. Please try again.', 'error');
    }
  } finally {
    submitBtn.disabled = false;
    submitBtn.textContent = 'Submit complaint';
  }
});

function showSuccess(res) {
  form.hidden = true;
  successBox.hidden = false;
  $('#ref-value').textContent = res.reference;
  const dl = $('#success-detail');
  clear(dl);
  const rows = [
    ['State', res.state],
    ['Submitted', fmtDay(res.submitted_at)],
    ['Attachments stored', String(res.attachments_stored)],
    ['Status lookup available', res.status_lookup_available ? 'Yes. Use this reference together with the contact detail you gave.' : 'No. You did not give a contact detail, so there is nothing to check the reference against.'],
  ];
  if (res.message) rows.push(['Note', res.message]);
  for (const [k, v] of rows) { dl.appendChild(el('dt', { text: k })); dl.appendChild(el('dd', { text: v })); }
  successBox.scrollIntoView({ behavior: 'smooth', block: 'start' });
  successBox.focus && successBox.setAttribute('tabindex', '-1');
  successBox.focus && successBox.focus();
}

init();
