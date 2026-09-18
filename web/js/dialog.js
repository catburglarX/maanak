// Accessible modal dialog helper using the native <dialog> element.
// Traps focus, closes on Escape, returns focus to the trigger.
import { el, clear } from './util.js';

export function openDialog({ title, body, actions }) {
  const trigger = document.activeElement;
  const dlg = document.createElement('dialog');
  dlg.setAttribute('aria-labelledby', 'dlg-title');

  const head = el('div', { class: 'dlg-head' }, [
    el('h2', { id: 'dlg-title', text: title }),
  ]);
  const closeX = el('button', { class: 'btn btn-sm', type: 'button', text: 'Close', 'aria-label': 'Close dialog' });
  head.appendChild(closeX);

  const bodyWrap = el('div', { class: 'dlg-body' });
  if (typeof body === 'string') bodyWrap.appendChild(el('p', { text: body }));
  else if (body) bodyWrap.appendChild(body);

  const foot = el('div', { class: 'dlg-foot' });

  const close = (result) => {
    if (dlg.open) dlg.close();
    dlg.remove();
    if (trigger && typeof trigger.focus === 'function') trigger.focus();
    if (dlg._onClose) dlg._onClose(result);
  };

  closeX.addEventListener('click', () => close(null));
  dlg.addEventListener('cancel', (e) => { e.preventDefault(); close(null); });

  (actions || []).forEach((a) => {
    const b = el('button', { class: `btn ${a.class || ''}`, type: 'button', text: a.label });
    b.addEventListener('click', () => {
      if (a.onClick) { const r = a.onClick(); if (r === false) return; }
      if (a.close !== false) close(a.value);
    });
    foot.appendChild(b);
  });

  dlg.appendChild(head);
  dlg.appendChild(bodyWrap);
  if ((actions || []).length) dlg.appendChild(foot);
  document.body.appendChild(dlg);
  dlg.showModal();

  const focusable = dlg.querySelector('input, select, textarea, button');
  if (focusable) focusable.focus();

  return {
    close,
    element: dlg,
    onClose(fn) { dlg._onClose = fn; },
  };
}

// A simple confirm dialog returning a promise<boolean>.
export function confirmDialog(title, message, { confirmLabel = 'Confirm', danger = false } = {}) {
  return new Promise((resolve) => {
    const d = openDialog({
      title,
      body: message,
      actions: [
        { label: 'Cancel', value: false },
        { label: confirmLabel, class: danger ? 'btn-danger' : 'btn-primary', value: true },
      ],
    });
    d.onClose((r) => resolve(r === true));
  });
}

// Prompt for a required reason string. Resolves to string or null.
export function reasonDialog(title, { label = 'Reason', minLength = 0, confirmLabel = 'Submit', danger = false } = {}) {
  return new Promise((resolve) => {
    const ta = el('textarea', { id: 'reason-input', required: true, 'aria-required': 'true' });
    const msg = el('p', { class: 'field-msg', role: 'alert', hidden: true });
    const wrap = el('div', { class: 'field' }, [
      el('label', { for: 'reason-input', text: label }),
      minLength ? el('p', { class: 'hint', text: `Minimum ${minLength} characters.` }) : null,
      ta, msg,
    ]);
    const d = openDialog({
      title,
      body: wrap,
      actions: [
        { label: 'Cancel', value: null },
        {
          label: confirmLabel,
          class: danger ? 'btn-danger' : 'btn-primary',
          close: false,
          onClick: () => {
            const v = ta.value.trim();
            if (v.length < minLength) {
              msg.hidden = false;
              msg.textContent = `Please enter at least ${minLength} characters.`;
              ta.focus();
              return false;
            }
            d.close(v);
          },
        },
      ],
    });
    d.onClose((r) => resolve(typeof r === 'string' ? r : null));
  });
}
