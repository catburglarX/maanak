// Shared DOM helpers, formatting, and status-tag rendering.
// No innerHTML with untrusted data — build nodes and use textContent.

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

export function fmtDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return String(iso);
  return d.toLocaleString('en-IN', { dateStyle: 'medium', timeStyle: 'short' });
}
export function fmtDay(iso) {
  if (!iso) return '—';
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

// Human label fallback when no vocabulary label is provided.
export function labelize(v) {
  if (v === null || v === undefined || v === '') return '—';
  return String(v).replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
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
