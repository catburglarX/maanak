// Shared DOM helpers, formatting, and status-tag rendering.
// No innerHTML with untrusted data. Build nodes and use textContent.

import { ApiError } from './api.js';

export function $(sel, root = document) { return root.querySelector(sel); }
export function $all(sel, root = document) { return Array.from(root.querySelectorAll(sel)); }

// Create an element. attrs may include: text, html(safe only), class, dataset, aria*, on{Event}, and any attribute.
export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'text') { node.textContent = v; }
    else if (k === 'class') { node.className = v; }
    else if (k === 'dataset') { for (const [dk, dv] of Object.entries(v)) node.dataset[dk] = dv; }
    else if (k === 'style') { node.style.cssText = v; } // CSSOM, not the CSP-restricted inline attribute
    else if (k.startsWith('on') && typeof v === 'function') { node.addEventListener(k.slice(2).toLowerCase(), v); }
    else if (k === 'html') { node.innerHTML = v; } // only used with hard-coded strings
    else if (v === true) { node.setAttribute(k, ''); }
    else { node.setAttribute(k, v); }
  }
  const kids = Array.isArray(children) ? children : [children];
  for (const c of kids) {
    if (c === null || c === undefined || c === false) continue;
    node.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
  }
  return node;
}

export function clear(node) { while (node && node.firstChild) node.removeChild(node.firstChild); }

// What to print where a record holds no value. A dash in a cell tells the reader
// nothing and a screen reader announces it as punctuation or skips it, so the absence
// is stated in words instead.
export const NOT_RECORDED = 'Not recorded';

// Singular or plural form of a noun for a count, so no message has to fall back to
// the "1 image(s)" shorthand.
export function plural(noun, count) {
  if (count === 1) return noun;
  if (/[^aeiou]y$/.test(noun)) return `${noun.slice(0, -1)}ies`;
  if (/(s|x|z|ch|sh)$/.test(noun)) return `${noun}es`;
  return `${noun}s`;
}

// "1 image", "3 images", "no images".
export function counted(count, noun, { zero = 'no' } = {}) {
  if (count === 0 && zero !== null) return `${zero} ${plural(noun, 0)}`;
  return `${count} ${plural(noun, count)}`;
}

// Brand and product name on one line. Joining them unconditionally produced
// "Riverside Riverside Iodised Salt 1 kg" for records whose name already carries the
// brand, so a brand at the front of the name is not repeated.
export function productLabel(p) {
  if (!p) return NOT_RECORDED;
  const name = (p.name || '').trim();
  const brand = (p.brand || '').trim();
  if (!name) return brand || NOT_RECORDED;
  if (!brand || name.toLowerCase().startsWith(brand.toLowerCase())) return name;
  return `${brand} ${name}`;
}

export function fmtDate(iso) {
  if (!iso) return NOT_RECORDED;
  const d = new Date(iso);
  if (isNaN(d)) return String(iso);
  return d.toLocaleString('en-IN', { dateStyle: 'medium', timeStyle: 'short' });
}
export function fmtDay(iso) {
  if (!iso) return NOT_RECORDED;
  const d = new Date(iso);
  if (isNaN(d)) return String(iso);
  return d.toLocaleDateString('en-IN', { dateStyle: 'medium' });
}

// Colour + glyph mapping for meaning-based status. Never colour alone.
const TAG_MAP = {
  // outcomes
  compliant: ['tag-green', '✓'],
  non_compliant: ['tag-red', '✕'],
  violation_found: ['tag-red', '✕'],
  unable_to_determine: ['tag-amber', '?'],
  additional_evidence_required: ['tag-amber', '+'],
  not_applicable: ['tag-grey', '–'],
  // review states
  pending: ['tag-amber', '•'],
  confirmed: ['tag-green', '✓'],
  rejected: ['tag-red', '✕'],
  corrected: ['tag-blue', '✎'],
  // machine states
  located: ['tag-blue', '◎'],
  not_detected: ['tag-grey', '–'],
  unreadable: ['tag-amber', '!'],
  ambiguous: ['tag-amber', '?'],
  incomplete_evidence: ['tag-amber', '…'],
  processing_failed: ['tag-red', '✕'],
  // rule / case / complaint / report generic
  draft: ['tag-grey', '•'],
  under_review: ['tag-amber', '•'],
  changes_required: ['tag-amber', '!'],
  approved: ['tag-green', '✓'],
  active: ['tag-green', '●'],
  superseded: ['tag-grey', '–'],
  withdrawn: ['tag-grey', '–'],
  received: ['tag-blue', '•'],
  triaged: ['tag-blue', '»'],
  assigned: ['tag-blue', '→'],
  resolved: ['tag-green', '✓'],
  closed: ['tag-grey', '■'],
  escalated: ['tag-red', '↑'],
  duplicate: ['tag-grey', '⊂'],
  issued: ['tag-green', '✓'],
  // evidence verdicts
  acceptable: ['tag-green', '✓'],
  review_recommended: ['tag-amber', '!'],
  recapture_recommended: ['tag-red', '↺'],
};

export function tag(value, label) {
  const [cls, glyph] = TAG_MAP[value] || ['tag-grey', '•'];
  return el('span', { class: `tag ${cls}`, 'data-glyph': glyph, text: label || labelize(value) });
}

// Values whose mechanical label is wrong: acronyms, hyphenated compounds, and a few
// phrases that read better reordered.
//
// This mirrors OVERRIDES in api/app/domain/labels.py, which is the authority. Most
// labels arrive from /reference/vocabulary and need nothing here, but a few values are
// rendered without a vocabulary lookup: a role on the account page, an actor role in
// the audit trail. A unit test compares the two tables, so they cannot drift.
export const LABEL_OVERRIDES = {
  mrp: 'MRP',
  mrp_close_up: 'MRP close-up',
  mrp_overcharge: 'MRP overcharge',
  fssai_licence: 'FSSAI licence',
  ocr_input: 'OCR input',
  net_quantity_close_up: 'Net quantity close-up',
  date_marking_close_up: 'Date marking close-up',
  non_compliant: 'Non-compliant',
  follow_up_inspection: 'Follow-up inspection',
  show_cause: 'Show-cause notice',
  ecommerce_listing: 'E-commerce listing',
  veg_nonveg_mark: 'Veg or non-veg mark',
  side_panel_left: 'Left side panel',
  side_panel_right: 'Right side panel',
  admin: 'Administrator',
  rule_admin: 'Rule administrator',
  expired_or_date_issue: 'Expired or wrong date',
};

// Human label when the API supplies none. Sentence case, not title case: a label reads
// as part of the page, and "Principal Display Panel" reads as a headline.
export function labelize(v) {
  if (v === null || v === undefined || v === '') return NOT_RECORDED;
  const key = String(v);
  if (LABEL_OVERRIDES[key]) return LABEL_OVERRIDES[key];
  const words = key.replace(/_/g, ' ');
  return words.charAt(0).toUpperCase() + words.slice(1);
}

// Loading / empty / error state helpers into a container.
export function showLoading(container, msg = 'Loading…') {
  clear(container);
  container.setAttribute('aria-busy', 'true');
  container.appendChild(el('div', { class: 'state', role: 'status' }, [
    el('p', { text: msg }),
  ]));
}
export function showEmpty(container, title, detail, action) {
  clear(container);
  container.removeAttribute('aria-busy');
  const kids = [el('h3', { text: title })];
  if (detail) kids.push(el('p', { text: detail }));
  if (action) kids.push(action);
  container.appendChild(el('div', { class: 'state' }, kids));
}
export function errorMessage(err) {
  if (err instanceof ApiError) {
    let msg = err.message;
    if (err.status >= 500 && err.requestId) msg += ` (Reference: ${err.requestId})`;
    return msg;
  }
  return err && err.message ? err.message : 'Something went wrong.';
}
export function showError(container, err) {
  clear(container);
  container.removeAttribute('aria-busy');
  const box = el('div', { class: 'notice notice-error', role: 'alert' }, [
    el('p', { text: errorMessage(err) }),
  ]);
  container.appendChild(box);
}

// Toast-like status region messages.
export function setStatus(region, msg, kind = 'ok') {
  if (!region) return;
  clear(region);
  if (!msg) return;
  const cls = kind === 'error' ? 'notice notice-error' : kind === 'warn' ? 'notice notice-warn' : 'notice notice-ok';
  const role = kind === 'error' ? 'alert' : 'status';
  region.appendChild(el('div', { class: cls, role }, [el('p', { text: msg })]));
}

// Attach 422 field problems to form fields via aria-describedby + role=alert.
export function applyFieldErrors(form, err) {
  clearFieldErrors(form);
  if (!(err instanceof ApiError) || err.status !== 422) return false;
  const fields = err.details && err.details.fields;
  if (!Array.isArray(fields)) return false;
  let attached = false;
  for (const f of fields) {
    const name = f.field;
    const input = form.querySelector(`[name="${CSS.escape(name)}"]`);
    if (!input) continue;
    const wrap = input.closest('.field') || input.parentElement;
    wrap.classList.add('field-error');
    const msgId = `err-${name}`;
    const msg = el('p', { id: msgId, class: 'field-msg', role: 'alert', text: f.problem });
    wrap.appendChild(msg);
    input.setAttribute('aria-describedby', msgId);
    input.setAttribute('aria-invalid', 'true');
    attached = true;
  }
  return attached;
}
export function clearFieldErrors(form) {
  $all('.field-error', form).forEach((w) => w.classList.remove('field-error'));
  $all('.field-msg', form).forEach((m) => m.remove());
  $all('[aria-invalid]', form).forEach((i) => { i.removeAttribute('aria-invalid'); i.removeAttribute('aria-describedby'); });
}

// Populate a <select> from a vocabulary list [{value,label}]. Optional blank first option.
export function fillSelect(select, options, { blank, selected } = {}) {
  clear(select);
  if (blank) select.appendChild(el('option', { value: '', text: blank }));
  for (const o of options || []) {
    const opt = el('option', { value: o.value, text: o.label || labelize(o.value) });
    if (selected !== undefined && o.value === selected) opt.selected = true;
    select.appendChild(opt);
  }
}

// Confirm-navigation guard for dirty forms.
export function trackDirty(form) {
  const state = { dirty: false };
  form.addEventListener('input', () => { state.dirty = true; });
  const handler = (e) => { if (state.dirty) { e.preventDefault(); e.returnValue = ''; } };
  window.addEventListener('beforeunload', handler);
  return {
    clean() { state.dirty = false; },
    release() { window.removeEventListener('beforeunload', handler); },
    isDirty() { return state.dirty; },
  };
}
