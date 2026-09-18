// Rule detail: citation, interpretation, simulator, status changes, legal authority.
import { requireAuth, can } from '../workspace.js';
import { mountAppChrome } from '../layout.js';
import { api, ApiError } from '../api.js';
import { $, el, clear, tag, fmtDay, labelize, setStatus, showError, fillSelect, NOT_RECORDED } from '../util.js';
import { getVocabulary, labelFor } from '../chrome.js';
import { openDialog } from '../dialog.js';

const id = new URLSearchParams(location.search).get('id');
let vocab = null;
let rule = null;

async function main() {
  mountAppChrome();
  const me = await requireAuth();
  if (!me) return;
  if (!id) { setStatus($('#page-status'), 'No rule specified.', 'error'); return; }
  vocab = await getVocabulary();
  await load();
  wireSim();
}

async function load() {
  try { rule = await api.get(`/rules/${id}`); }
  catch (err) { showError($('#detail'), err); return; }
  render();
}

function render() {
  const box = $('#detail');
  box.removeAttribute('aria-busy');
  clear(box);
  box.appendChild(el('div', { class: 'between' }, [
    el('div', {}, [
      el('h1', { text: `${rule.code} v${rule.version}` }),
      el('div', { class: 'btn-row' }, [
        tag(rule.status, labelFor(vocab.rule_statuses, rule.status)),
        rule.legal_authority_confirmed ? tag('confirmed', 'Legal authority confirmed') : tag('unable_to_determine', 'Legal authority NOT confirmed'),
        rule.effective_for_inspections ? tag('active', 'Effective for inspections') : tag('draft', 'Not effective'),
      ]),
    ]),
    el('a', { class: 'btn btn-sm', href: '/app/rules.html', text: 'Back to rules' }),
  ]));
  box.appendChild(el('h2', { text: rule.title || '' }));

  if (!rule.legal_authority_confirmed) {
    box.appendChild(el('div', { class: 'notice notice-warn' }, [
      el('p', {}, [el('strong', { text: 'Legal authority not confirmed. ' }), 'This interpretation has been approved for use in this workspace but has NOT been endorsed by a statutory authority, and its citation is not verified against the gazette. Confirm before enforcement use.']),
    ]));
  }

  const dl = el('dl', { class: 'defs' });
  const add = (k, v) => { if (v === null || v === undefined || v === '') return; dl.append(el('dt', { text: k }), el('dd', {}, [typeof v === 'object' ? v : document.createTextNode(String(v))])); };
  add('Citation', rule.citation);
  add('Source', rule.source_url ? el('a', { href: rule.source_url, target: '_blank', rel: 'noopener noreferrer', text: rule.source_document_title || rule.source_url }) : null);
  add('Gazette reference', rule.gazette_reference);
  add('Source retrieved on', rule.source_retrieved_on ? fmtDay(rule.source_retrieved_on) : null);
  add('Test kind', labelize(rule.test_kind));
  add('Effective from', fmtDay(rule.effective_from));
  add('Effective to', rule.effective_to ? fmtDay(rule.effective_to) : 'Open');
  add('Commodity scope', Array.isArray(rule.commodity_scope) ? rule.commodity_scope.join(', ') : rule.commodity_scope);
  add('Quantity kind', rule.quantity_kind ? labelize(rule.quantity_kind) : null);
  add('Plain explanation', rule.plain_explanation);
  add('Interpretation note', rule.interpretation_note);
  add('Uncertainty note', rule.uncertainty_note);
  add('Legal authority note', rule.legal_authority_note);
  add('Record version', rule.record_version);
  box.appendChild(dl);

  // Test specification / required inputs
  if (rule.required_inputs && rule.required_inputs.length) {
    box.appendChild(el('h3', { text: 'Required inputs' }));
    box.appendChild(el('div', { class: 'mono-list' }, [el('ul', {}, rule.required_inputs.map((r) => el('li', { text: r })))]));
  }

  // Governance blockers / actions
  renderGovernance(box);
  $('#sim-section').hidden = false;
}

function renderGovernance(box) {
  const gov = rule.governance || {};
  const sec = el('section', { class: 'panel', 'aria-label': 'Governance' }, [el('h2', { text: 'Governance' })]);

  const blockers = gov.approval_blockers || gov.blockers || [];
  if (Array.isArray(blockers) && blockers.length) {
    sec.appendChild(el('div', { class: 'notice notice-warn' }, [
      el('p', { text: 'Approval blockers:' }),
      el('ul', {}, blockers.map((b) => el('li', { text: typeof b === 'string' ? b : (b.message || JSON.stringify(b)) }))),
    ]));
  } else {
    sec.appendChild(el('p', { class: 'muted', text: 'No approval blockers reported, or none applicable at this status.' }));
  }

  const row = el('div', { class: 'btn-row' });
  let any = false;
  if (can('rule.update') || can('rule.submit') || can('rule.approve')) {
    any = true;
    const b = el('button', { class: 'btn', type: 'button', text: 'Change status' });
    b.addEventListener('click', changeStatus);
    row.appendChild(b);
  }
  if (can('rule.approve') || can('rule.simulate')) {
    any = true;
    const b = el('button', { class: 'btn', type: 'button', text: rule.legal_authority_confirmed ? 'Update legal authority' : 'Record legal authority' });
    b.addEventListener('click', legalAuthority);
    row.appendChild(b);
  }
  if (any) sec.appendChild(row);
  box.appendChild(sec);
}

function fieldEl(labelText, control) { return el('div', { class: 'field' }, [el('label', { for: control.id, text: labelText }), control]); }

function changeStatus() {
  const sel = el('select', { id: 'st-target' });
  fillSelect(sel, vocab.rule_statuses, { blank: 'Select target status…' });
  const note = el('textarea', { id: 'st-note', maxlength: '2000' });
  submitDialog('Change rule status', [fieldEl('Target status', sel), fieldEl('Note', note)], async () => {
    if (!sel.value) throw new ApiError(422, { error: { message: 'Select a status.' } });
    await api.post(`/rules/${id}/status`, { target_status: sel.value, note: note.value.trim() || undefined, expected_version: rule.record_version });
  });
}

function legalAuthority() {
  const chk = el('input', { type: 'checkbox', id: 'la-confirmed', checked: !!rule.legal_authority_confirmed });
  const note = el('textarea', { id: 'la-note', maxlength: '2000' });
  submitDialog('Record legal authority', [
    el('div', { class: 'field checkbox-row' }, [chk, el('label', { for: 'la-confirmed', text: 'A qualified authority has confirmed this rule\u2019s legal authority' })]),
    fieldEl('Note (required)', note),
  ], async () => {
    if (!note.value.trim()) throw new ApiError(422, { error: { message: 'A note is required.' } });
    await api.post(`/rules/${id}/legal-authority`, { confirmed: chk.checked, note: note.value.trim(), expected_version: rule.record_version });
  });
}

function submitDialog(title, fields, onSubmit) {
  const form = el('form', {});
  const status = el('div', { class: 'status-region', 'aria-live': 'polite' });
  form.appendChild(status);
  fields.forEach((f) => form.appendChild(f));
  const dlg = openDialog({
    title, body: form,
    actions: [
      { label: 'Cancel', value: null },
      { label: 'Save', class: 'btn-primary', close: false, onClick: async () => {
        setStatus(status, '');
        try { await onSubmit(); dlg.close(); setStatus($('#page-status'), 'Saved.', 'ok'); await load(); }
        catch (err) {
          if (err instanceof ApiError && err.status === 409 && err.code === 'stale_record') setStatus(status, 'This record changed after you loaded it. Close and reload.', 'error');
          else setStatus(status, err instanceof ApiError ? err.message : 'Action failed.', 'error');
        }
        return false;
      } },
    ],
  });
}

function wireSim() {
  $('#run-sim').addEventListener('click', async () => {
    const btn = $('#run-sim');
    btn.disabled = true; btn.textContent = 'Running…';
    const out = $('#sim-result');
    clear(out);
    try {
      const res = await api.post(`/rules/${id}/simulate`, { scenarios: [], use_seeded_scenarios: true, include_generated: true });
      renderSim(res);
    } catch (err) { showError(out, err); }
    finally { btn.disabled = false; btn.textContent = 'Run simulation'; }
  });
}

function renderSim(res) {
  const out = $('#sim-result');
  clear(out);
  const summary = el('div', { class: res.all_passed ? 'notice notice-ok' : 'notice notice-warn' }, [
    el('p', {}, [
      el('strong', { text: res.all_passed ? 'All scenarios passed. ' : 'Some scenarios did not pass. ' }),
      document.createTextNode(`Passed ${res.passed_count}, failed ${res.failed_count}. Approval ready: ${res.approval_ready ? 'yes' : 'no'}.`),
    ]),
  ]);
  out.appendChild(summary);
  if (res.missing_kinds && res.missing_kinds.length) {
    out.appendChild(el('p', { class: 'small muted', text: `Missing scenario kinds for approval: ${res.missing_kinds.join(', ')}` }));
  }
  const scenarios = res.scenarios || [];
  if (!scenarios.length) { out.appendChild(el('p', { class: 'muted', text: 'No scenarios were run.' })); return; }
  const table = el('table', { class: 'register' });
  table.appendChild(el('caption', { text: 'Scenario results' }));
  table.appendChild(el('thead', {}, [el('tr', {}, [
    el('th', { scope: 'col', text: 'Scenario' }),
    el('th', { scope: 'col', text: 'Result' }),
    el('th', { scope: 'col', text: 'Expected' }),
    el('th', { scope: 'col', text: 'Actual' }),
  ])]));
  const tbody = el('tbody');
  for (const s of scenarios) {
    const pass = s.passed === true;
    tbody.appendChild(el('tr', {}, [
      el('th', { scope: 'row', text: s.name || s.label || NOT_RECORDED }),
      el('td', {}, [tag(pass ? 'confirmed' : 'rejected', pass ? 'Pass' : 'Fail')]),
      el('td', { text: s.expected_outcome || NOT_RECORDED }),
      el('td', { text: s.actual_outcome || s.outcome || NOT_RECORDED }),
    ]));
  }
  table.appendChild(tbody);
  out.appendChild(el('div', { class: 'table-wrap' }, [table]));
}

main();
