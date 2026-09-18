// Inspection detail — the split evidence-review screen controller.
import { requireAuth, can } from '../workspace.js';
import { mountAppChrome } from '../layout.js';
import { api, ApiError } from '../api.js';
import {
  $, el, clear, tag, fmtDate, fmtDay, labelize,
  setStatus, showError, fillSelect,
} from '../util.js';
import { getVocabulary, labelFor } from '../chrome.js';
import { reasonDialog, confirmDialog } from '../dialog.js';
import { createViewer } from './inspection-viewer.js';

const params = new URLSearchParams(location.search);
const inspectionId = params.get('id');

let vocab = null;
let inspection = null;
let viewer = null;
let selectedEvidence = null;
let selectedCandidateId = null;
const jobPollers = new Map();

const pageStatus = () => $('#page-status');

function vlabel(list, v) { return labelFor(vocab[list], v); }

async function main() {
  mountAppChrome();
  const me = await requireAuth();
  if (!me) return;
  if (!inspectionId) {
    setStatus(pageStatus(), 'No inspection was specified.', 'error');
    return;
  }
  vocab = await getVocabulary();
  fillSelect($('#up-face'), vocab.package_faces, {});
  wireUpload();
  wireRunChecks();
  await load();
}

async function load() {
  try {
    inspection = await api.get(`/inspections/${encodeURIComponent(inspectionId)}`);
  } catch (err) {
    showError($('#context-body'), err);
    return;
  }
  renderContext();
  renderCoverage();
  renderEvidenceThumbs();
  renderCandidates();
  renderFindings();
  renderActionBar();
}

/* ---------- Context ---------- */
function renderContext() {
  const box = $('#context-body');
  box.removeAttribute('aria-busy');
  clear(box);
  const p = inspection.product;
  const productName = p ? [p.brand, p.name].filter(Boolean).join(' — ') : null;

  box.appendChild(el('div', { class: 'between' }, [
    el('div', {}, [
      el('h1', { text: `Inspection ${inspection.reference}` }),
      el('div', { class: 'btn-row' }, [
        tag(inspection.state, vlabel('inspection_states', inspection.state)),
        inspection.decision ? tag(inspection.decision, vlabel('legal_outcomes', inspection.decision)) : null,
      ]),
    ]),
    el('a', { class: 'btn btn-sm', href: '/app/inspections.html', text: 'Back to register' }),
  ]));

  const dl = el('dl', { class: 'defs' });
  const add = (k, v) => { dl.appendChild(el('dt', { text: k })); dl.appendChild(el('dd', {}, [typeof v === 'string' || typeof v === 'number' ? document.createTextNode(String(v)) : (v || document.createTextNode('—'))])); };
  add('Product', productName || 'Not yet recorded');
  if (p && p.commodity_category) add('Category', labelize(p.commodity_category));
  if (p && p.declared_net_quantity) add('Declared net quantity', `${p.declared_net_quantity} ${p.declared_net_quantity_unit || ''}`.trim());
  add('Source', vlabel('inspection_sources', inspection.source));
  add('Inspection date', fmtDay(inspection.inspection_date));
  if (inspection.premises_name) add('Premises', inspection.premises_name);
  if (inspection.jurisdiction_name) add('Jurisdiction', inspection.jurisdiction_name);
  add('Evidence files', inspection.evidence_count);
  add('Pending candidates', inspection.pending_candidate_count);
  add('Rules evaluated', inspection.rules_evaluated_count ?? 0);
  if (inspection.checks_executed_at) add('Checks last run', fmtDate(inspection.checks_executed_at));
  add('Version', inspection.version);
  if (inspection.decision_note) add('Decision note', inspection.decision_note);
  box.appendChild(dl);
}

/* ---------- Coverage ---------- */
function renderCoverage() {
  const list = $('#coverage');
  clear(list);
  const cov = inspection.coverage || {};
  const faces = cov.faces || [];
  if (!faces.length) {
    list.appendChild(el('li', { class: 'coverage-item' }, [el('span', { text: 'No faces have been defined for this inspection yet.' })]));
    return;
  }
  const canMark = can('inspection.update') || can('evidence.upload');
  for (const f of faces) {
    const item = el('li', { class: 'coverage-item' }, [
      el('span', { class: 'face-name', text: vlabel('package_faces', f.face) }),
      el('div', { class: 'btn-row my-tight' }, [
        tag(f.capture_state, vlabel('face_capture_states', f.capture_state)),
        el('span', { class: 'small', text: `${f.evidence_count} image(s)` }),
      ]),
      f.reason ? el('p', { class: 'small muted', text: `Reason: ${f.reason}` }) : null,
    ]);
    if (canMark) {
      const btn = el('button', { class: 'btn btn-sm', type: 'button', text: 'Mark state' });
      btn.addEventListener('click', () => markFace(f));
      item.appendChild(btn);
    }
    list.appendChild(item);
  }
  const outstanding = cov.required_outstanding || [];
  if (outstanding.length) {
    list.parentElement.insertBefore(
      el('div', { class: 'notice notice-warn', role: 'status' }, [
        el('p', { text: `Outstanding required faces: ${outstanding.map((v) => vlabel('package_faces', v)).join(', ')}.` }),
      ]),
      list,
    );
  }
}

async function markFace(face) {
  const form = el('form', {});
  const sel = el('select', { id: 'fs-state', name: 'capture_state' });
  fillSelect(sel, vocab.face_capture_states, {});
  sel.value = face.capture_state;
  const reason = el('textarea', { id: 'fs-reason', name: 'reason', maxlength: '2000' });
  form.appendChild(el('div', { class: 'field' }, [el('label', { for: 'fs-state', text: 'Capture state' }), sel]));
  form.appendChild(el('div', { class: 'field' }, [el('label', { for: 'fs-reason', text: 'Reason (required when marking absent / not applicable / unable to capture)' }), reason]));

  const { openDialog } = await import('../dialog.js');
  const dlg = openDialog({
    title: `Mark ${vlabel('package_faces', face.face)}`,
    body: form,
    actions: [
      { label: 'Cancel', value: null },
      {
        label: 'Save', class: 'btn-primary', close: false,
        onClick: async () => {
          const state = sel.value;
          const needsReason = ['absent', 'not_applicable', 'unable_to_capture'].includes(state);
          if (needsReason && !reason.value.trim()) { reason.focus(); return false; }
          try {
            await api.post(`/inspections/${inspectionId}/faces`, {
              face: face.face,
              capture_state: state,
              reason: reason.value.trim() || undefined,
            });
            dlg.close();
            setStatus(pageStatus(), 'Face state updated.', 'ok');
            await load();
          } catch (err) { handleMutationError(err); }
          return false;
        },
      },
    ],
  });
}

/* ---------- Evidence thumbnails + viewer ---------- */
function renderEvidenceThumbs() {
  if (!viewer) viewer = createViewer();
  const thumbs = $('#thumbs');
  clear(thumbs);
  const evidence = inspection.evidence || [];
  if (!evidence.length) {
    thumbs.appendChild(el('span', { class: 'small muted', text: 'No evidence uploaded yet.' }));
    viewer.show(null);
    return;
  }
  evidence.forEach((ev, i) => {
    const btn = el('button', { type: 'button', 'aria-pressed': String(i === 0), title: vlabel('package_faces', ev.face) }, [
      ev.thumbnail_url || ev.view_url
        ? el('img', { src: ev.thumbnail_url || ev.view_url, alt: '' })
        : el('span', { class: 'thumb-face', text: vlabel('package_faces', ev.face) }),
      el('span', { class: 'thumb-face', text: vlabel('package_faces', ev.face) }),
    ]);
    btn.addEventListener('click', () => selectEvidence(ev, btn));
    thumbs.appendChild(btn);
  });
  selectEvidence(evidence[0], thumbs.querySelector('button'));
}

function selectEvidence(ev, btn) {
  selectedEvidence = ev;
  $('#thumbs').querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', String(b === btn)));
  viewer.show(ev);
  // Reset OCR panel when switching.
  const ocrPanel = $('#ocr-panel');
  if (!ocrPanel.hidden) loadOcr(ev);
  // Re-highlight selected candidate if it belongs to this evidence.
  const cand = findCandidate(selectedCandidateId);
  if (cand && cand.evidence_id === ev.id) viewer.highlight(cand.region);
  else viewer.highlight(null);
}

$('#toggle-ocr').addEventListener('click', async () => {
  const panel = $('#ocr-panel');
  const btn = $('#toggle-ocr');
  const show = panel.hidden;
  panel.hidden = !show;
  btn.setAttribute('aria-pressed', String(show));
  if (show && selectedEvidence) loadOcr(selectedEvidence);
});

async function loadOcr(ev) {
  const raw = $('#ocr-raw');
  raw.textContent = 'Loading OCR…';
  try {
    const res = await api.get(`/inspections/${inspectionId}/evidence/${ev.id}/ocr`);
    if (!res || res.available === false) {
      raw.textContent = res && res.failure_reason ? `OCR unavailable: ${res.failure_reason}` : 'OCR output is not available for this image.';
      return;
    }
    const header = `Engine: ${res.engine || '—'} ${res.engine_version || ''}\nLanguages: ${(res.languages || []).join(', ')}\nMean confidence: ${res.mean_confidence ?? '—'} · Words: ${res.word_count ?? '—'}\n\n`;
    raw.textContent = header + (res.raw_text || '(no text detected)');
  } catch (err) {
    raw.textContent = err instanceof ApiError ? `Could not load OCR: ${err.message}` : 'Could not load OCR output.';
  }
}

/* ---------- Candidates ---------- */
function findCandidate(id) {
  return (inspection.candidates || []).find((c) => c.id === id) || null;
}

function renderCandidates() {
  const box = $('#candidates');
  box.removeAttribute('aria-busy');
  clear(box);
  const candidates = inspection.candidates || [];
  if (!candidates.length) {
    box.appendChild(el('div', { class: 'state' }, [
      el('h3', { text: 'No candidates yet' }),
      el('p', { text: 'Upload package evidence and run analysis. Detected declarations will appear here for review.' }),
    ]));
    return;
  }
  // Group by declaration type.
  const groups = new Map();
  for (const c of candidates) {
    const key = c.declaration_type || 'other';
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(c);
  }
  for (const [type, list] of groups) {
    const group = el('div', { class: 'candidate-group' }, [el('h3', { text: vlabel('declaration_types', type) })]);
    for (const c of list) group.appendChild(renderCandidate(c));
    box.appendChild(group);
  }
}

function renderCandidate(c) {
  // The card holds its own review buttons, so it must not itself be a control:
  // role="button" around them nests interactive elements, and aria-selected is not a
  // permitted attribute on that role. Selection is therefore carried by a real button
  // in the card head, using aria-pressed, and the card is a plain container.
  const node = el('div', { class: 'candidate', 'data-selected': String(c.id === selectedCandidateId) });
  const selectBtn = el('button', {
    type: 'button',
    class: 'cand-select',
    'aria-pressed': String(c.id === selectedCandidateId),
    text: vlabel('declaration_types', c.declaration_type),
  });
  const select = () => {
    selectedCandidateId = c.id;
    $('#candidates').querySelectorAll('.candidate').forEach((n) => n.setAttribute('data-selected', 'false'));
    $('#candidates').querySelectorAll('.cand-select').forEach((b) => b.setAttribute('aria-pressed', 'false'));
    node.setAttribute('data-selected', 'true');
    selectBtn.setAttribute('aria-pressed', 'true');
    // Switch to the evidence this candidate belongs to, then highlight.
    const ev = (inspection.evidence || []).find((e) => e.id === c.evidence_id);
    if (ev && (!selectedEvidence || selectedEvidence.id !== ev.id)) {
      const btn = Array.from($('#thumbs').querySelectorAll('button'))[(inspection.evidence || []).indexOf(ev)];
      selectEvidence(ev, btn);
    }
    if (viewer) viewer.highlight(c.region);
  };
  selectBtn.addEventListener('click', select);
  // Clicking anywhere on the card is a mouse convenience. The container carries no
  // role and no tabindex, so it adds nothing to the keyboard path.
  node.addEventListener('click', (e) => {
    if (e.target.closest('.review-controls, .review-form, .cand-select')) return;
    select();
  });

  node.appendChild(el('div', { class: 'cand-head' }, [
    selectBtn,
    el('span', { class: 'conf', text: c.machine_confidence != null ? `Confidence: ${(c.machine_confidence * (c.machine_confidence <= 1 ? 100 : 1)).toFixed(0)}%` : 'Confidence: —' }),
  ]));

  // Two SEPARATE labelled states — never merged.
  node.appendChild(el('div', { class: 'states' }, [
    el('span', { class: 'label-pair' }, [el('span', { class: 'k', text: 'Machine observed' }), tag(c.machine_state, vlabel('machine_states', c.machine_state))]),
    el('span', { class: 'label-pair' }, [el('span', { class: 'k', text: 'Officer review' }), tag(c.review_state, vlabel('review_states', c.review_state))]),
  ]));

  if (c.matched_text) node.appendChild(el('div', { class: 'matched', text: c.matched_text }));
  // normalised_value is a structured object such as
  // {kind: 'money', amount: '24.00', currency: 'INR', display: '₹24.00'}. Only its
  // display field is text; interpolating the object itself printed "[object Object]".
  // An empty object is still truthy, so the row is suppressed when there is nothing
  // to show rather than left with a dangling label.
  const shown = c.display_value || (c.normalised_value && c.normalised_value.display) || '';
  if (shown) {
    node.appendChild(el('p', { class: 'small' }, [
      el('span', { class: 'muted', text: 'Normalised value: ' }),
      document.createTextNode(`${shown}${c.unit ? ' ' + c.unit : ''}`),
    ]));
  }
  if (c.machine_explanation) node.appendChild(el('p', { class: 'small muted', text: c.machine_explanation }));
  if (c.correction_reason) node.appendChild(el('p', { class: 'small', text: `Correction reason: ${c.correction_reason}` }));
  if (c.review_note) node.appendChild(el('p', { class: 'small', text: `Review note: ${c.review_note}` }));

  if (can('candidate.review')) node.appendChild(renderReviewControls(c));

  // History link
  const hist = el('button', { class: 'btn btn-sm', type: 'button', text: 'History' });
  hist.addEventListener('click', () => showCandidateHistory(c));
  node.appendChild(el('div', { class: 'mt-half' }, [hist]));

  return node;
}

function renderReviewControls(c) {
  const wrap = el('div', { class: 'review-controls' });
  const confirmBtn = el('button', { class: 'btn btn-sm', type: 'button', text: 'Confirm' });
  const correctBtn = el('button', { class: 'btn btn-sm', type: 'button', text: 'Correct' });
  const rejectBtn = el('button', { class: 'btn btn-sm btn-danger', type: 'button', text: 'Reject' });
  const naBtn = el('button', { class: 'btn btn-sm', type: 'button', text: 'Not applicable' });

  confirmBtn.addEventListener('click', () => submitReview(c, { review_state: 'confirmed' }));
  naBtn.addEventListener('click', () => submitReview(c, { review_state: 'not_applicable' }));

  correctBtn.addEventListener('click', async () => {
    const { openDialog } = await import('../dialog.js');
    const text = el('input', { type: 'text', id: 'corr-text', value: c.matched_text || '' });
    const reason = el('textarea', { id: 'corr-reason' });
    const form = el('form', {}, [
      el('div', { class: 'field' }, [el('label', { for: 'corr-text', text: 'Corrected text' }), text]),
      el('div', { class: 'field' }, [el('label', { for: 'corr-reason', text: 'Correction reason' }), reason]),
    ]);
    const dlg = openDialog({
      title: 'Correct candidate', body: form,
      actions: [
        { label: 'Cancel', value: null },
        { label: 'Save correction', class: 'btn-primary', close: false, onClick: async () => {
          if (!text.value.trim() || !reason.value.trim()) { (text.value.trim() ? reason : text).focus(); return false; }
          await submitReview(c, { review_state: 'corrected', corrected_text: text.value.trim(), correction_reason: reason.value.trim() });
          dlg.close();
          return false;
        } },
      ],
    });
  });

  rejectBtn.addEventListener('click', async () => {
    const note = await reasonDialog('Reject candidate', { label: 'Review note (why is this being rejected?)', minLength: 1, confirmLabel: 'Reject', danger: true });
    if (note) await submitReview(c, { review_state: 'rejected', review_note: note });
  });

  wrap.append(confirmBtn, correctBtn, rejectBtn, naBtn);
  return wrap;
}

async function submitReview(c, patch) {
  setStatus($('#candidate-status'), '');
  try {
    await api.patch(`/candidates/${c.id}`, { ...patch, expected_version: c.version });
    setStatus($('#candidate-status'), 'Candidate review saved.', 'ok');
    await load();
  } catch (err) { handleMutationError(err, $('#candidate-status')); }
}

async function showCandidateHistory(c) {
  const { openDialog } = await import('../dialog.js');
  const body = el('div', {}, [el('p', { text: 'Loading history…' })]);
  const dlg = openDialog({ title: 'Candidate history', body, actions: [{ label: 'Close', value: null }] });
  try {
    const res = await api.get(`/candidates/${c.id}/history`);
    const items = res.items || res || [];
    clear(body);
    if (!items.length) { body.appendChild(el('p', { text: 'No history recorded.' })); return; }
    const ul = el('ul', {});
    for (const h of items) {
      ul.appendChild(el('li', {}, [
        el('strong', { text: `${labelize(h.review_state || h.action || 'change')} ` }),
        document.createTextNode(`${h.correction_reason || h.review_note || ''} `),
        el('span', { class: 'small muted', text: h.recorded_at ? fmtDate(h.recorded_at) : (h.created_at ? fmtDate(h.created_at) : '') }),
      ]));
    }
    body.appendChild(ul);
  } catch (err) {
    clear(body);
    body.appendChild(el('p', { class: 'field-msg', role: 'alert', text: err instanceof ApiError ? err.message : 'Could not load history.' }));
  }
}

/* ---------- Findings ---------- */
function renderFindings() {
  const box = $('#findings');
  clear(box);
  const findings = inspection.findings || [];
  if (!findings.length) {
    box.appendChild(el('div', { class: 'state' }, [
      el('h3', { text: 'No findings yet' }),
      el('p', { text: 'Run the checks once candidates are reviewed. If no rule applies, that will be stated here rather than left blank.' }),
    ]));
    return;
  }
  for (const f of findings) box.appendChild(renderFinding(f));
}

function renderFinding(f) {
  const outcome = f.outcome;
  const node = el('div', { class: `finding out-${outcome}` });
  node.appendChild(el('div', { class: 'between' }, [
    el('strong', { text: vlabel('declaration_types', f.declaration_type) }),
    el('div', { class: 'btn-row' }, [
      el('span', { class: 'label-pair' }, [el('span', { class: 'k', text: 'Engine outcome' }), tag(outcome, vlabel('legal_outcomes', outcome))]),
      f.officer_outcome ? el('span', { class: 'label-pair' }, [el('span', { class: 'k', text: 'Officer override' }), tag(f.officer_outcome, vlabel('legal_outcomes', f.officer_outcome))]) : null,
    ]),
  ]));
  if (f.explanation) node.appendChild(el('p', { text: f.explanation }));
  if (f.officer_note) node.appendChild(el('p', { class: 'small' }, [el('strong', { text: 'Officer note: ' }), document.createTextNode(f.officer_note)]));

  if (f.expected_value != null || f.observed_value != null) {
    const cmp = el('dl', { class: 'compare' });
    cmp.append(el('dt', { text: 'Expected' }), el('dd', { text: f.expected_value ?? '—' }));
    cmp.append(el('dt', { text: 'Observed' }), el('dd', { text: f.observed_value ?? '—' }));
    node.appendChild(cmp);
  }

  // Calculation — monospace auditable maths.
  if (Array.isArray(f.calculation) && f.calculation.length) {
    const list = el('ol', {});
    for (const step of f.calculation) list.appendChild(el('li', { text: step }));
    node.appendChild(el('div', {}, [el('p', { class: 'small muted', text: 'Calculation' }), el('div', { class: 'mono-list' }, [list])]));
  }

  // Rule block.
  const r = f.rule || {};
  if (r.code) {
    const rb = el('div', { class: 'rule-block' });
    rb.appendChild(el('p', {}, [el('strong', { text: `${r.code} v${r.version} — ${r.title || ''}` })]));
    if (r.citation) rb.appendChild(el('p', { class: 'small', text: `Citation: ${r.citation}` }));
    if (r.plain_explanation) rb.appendChild(el('p', { class: 'small', text: r.plain_explanation }));
    if (r.interpretation_note) rb.appendChild(el('p', { class: 'small muted', text: `Interpretation: ${r.interpretation_note}` }));
    if (r.uncertainty_note) rb.appendChild(el('p', { class: 'small muted', text: `Uncertainty: ${r.uncertainty_note}` }));
    const dates = [r.effective_from ? `from ${r.effective_from}` : null, r.effective_to ? `to ${r.effective_to}` : null].filter(Boolean).join(' ');
    if (dates || r.source_url) {
      rb.appendChild(el('p', { class: 'small' }, [
        r.source_url ? el('a', { href: r.source_url, rel: 'noopener noreferrer', target: '_blank', text: 'Source' }) : null,
        dates ? document.createTextNode(` · Effective ${dates}`) : null,
      ]));
    }
    if (r.legal_authority_confirmed === false) {
      rb.appendChild(el('div', { class: 'notice notice-warn', role: 'note' }, [
        el('p', {}, [el('strong', { text: 'Legal authority not confirmed. ' }), 'This interpretation has been approved for use in this workspace but has NOT been endorsed by a statutory authority. It must be confirmed by a qualified authority before enforcement use.']),
      ]));
    }
    node.appendChild(rb);
  } else {
    node.appendChild(el('p', { class: 'small muted', text: 'No applicable rule was recorded for this finding.' }));
  }

  // Officer override control.
  if (can('finding.override')) {
    const btn = el('button', { class: 'btn btn-sm', type: 'button', text: f.officer_outcome ? 'Change override' : 'Record override' });
    btn.addEventListener('click', () => overrideFinding(f));
    node.appendChild(el('div', { class: 'mt-half' }, [btn]));
  }
  return node;
}

async function overrideFinding(f) {
  const { openDialog } = await import('../dialog.js');
  const sel = el('select', { id: 'ov-outcome' });
  fillSelect(sel, vocab.legal_outcomes, { blank: 'Select outcome…', selected: f.officer_outcome || undefined });
  const note = el('textarea', { id: 'ov-note' });
  const form = el('form', {}, [
    el('div', { class: 'field' }, [el('label', { for: 'ov-outcome', text: 'Officer outcome' }), sel]),
    el('div', { class: 'field' }, [el('label', { for: 'ov-note', text: 'Officer note (required)' }), note]),
  ]);
  const dlg = openDialog({
    title: 'Override finding', body: form,
    actions: [
      { label: 'Cancel', value: null },
      { label: 'Save override', class: 'btn-primary', close: false, onClick: async () => {
        if (!sel.value || !note.value.trim()) { (sel.value ? note : sel).focus(); return false; }
        try {
          await api.patch(`/findings/${f.id}`, { officer_outcome: sel.value, officer_note: note.value.trim(), expected_version: f.version });
          dlg.close();
          setStatus(pageStatus(), 'Finding override recorded.', 'ok');
          await load();
        } catch (err) { handleMutationError(err); }
        return false;
      } },
    ],
  });
}

/* ---------- Action bar: run checks, transitions, decision ---------- */
function renderActionBar() {
  $('#action-bar').hidden = false;
  renderFindingsSummary();

  const runBtn = $('#run-checks');
  if (can('inspection.run_checks')) runBtn.hidden = false;

  renderTransitions();
  renderDecision();
}

// findings_summary holds a nested counts object plus a suggested outcome, a rationale
// and a standing note. Rendering it with a generic key/value loop printed the counts
// object as "[object Object]", so each part is composed deliberately here.
function renderFindingsSummary() {
  const box = $('#findings-summary');
  clear(box);
  const fs = inspection.findings_summary || {};
  const counts = fs.counts && typeof fs.counts === 'object' ? fs.counts : {};
  const total = (inspection.findings || []).length;

  const tallies = Object.entries(counts)
    .filter(([, n]) => Number(n) > 0)
    .sort((a, b) => Number(b[1]) - Number(a[1]));

  if (!total && !tallies.length) {
    box.appendChild(el('p', { class: 'small muted', text: 'No checks have been run yet.' }));
    return;
  }

  const line = el('p', {}, [
    el('strong', { text: `${total} finding${total === 1 ? '' : 's'}` }),
  ]);
  if (tallies.length) line.appendChild(document.createTextNode(' — '));
  tallies.forEach(([outcome, n], index) => {
    if (index) line.appendChild(document.createTextNode(' '));
    line.appendChild(tag(outcome, `${n} ${vlabel('legal_outcomes', outcome)}`));
  });
  box.appendChild(line);

  if (fs.suggested_outcome) {
    box.appendChild(el('p', { class: 'small' }, [
      el('strong', { text: 'Suggested outcome: ' }),
      document.createTextNode(vlabel('legal_outcomes', fs.suggested_outcome)),
      fs.rationale ? document.createTextNode(`. ${fs.rationale}`) : null,
    ]));
  }
  if (fs.note) {
    box.appendChild(el('p', { class: 'small muted', text: fs.note }));
  }
}

function renderTransitions() {
  const box = $('#transitions');
  clear(box);
  const transitions = inspection.available_transitions || [];
  if (!transitions.length) return;
  for (const t of transitions) {
    const blocked = Array.isArray(t.blocked_by) && t.blocked_by.length > 0;
    const btn = el('button', {
      class: `btn btn-sm ${blocked ? 'blocked' : ''}`, type: 'button',
      disabled: blocked,
      'aria-disabled': String(blocked),
      text: t.label || labelize(t.target_state),
    });
    if (!blocked) btn.addEventListener('click', () => doTransition(t));
    const wrap = el('div', {}, [btn]);
    if (blocked) wrap.appendChild(el('span', { class: 'blocked-reason', text: `Blocked: ${t.blocked_by.join('; ')}` }));
    box.appendChild(wrap);
  }
}

async function doTransition(t) {
  let reason;
  if (t.reason_required) {
    reason = await reasonDialog(`Transition to ${t.label}`, { label: 'Reason', minLength: 1, confirmLabel: 'Confirm' });
    if (reason === null) return;
  }
  try {
    await api.post(`/inspections/${inspectionId}/transitions`, {
      target_state: t.target_state,
      reason: reason || undefined,
      expected_version: inspection.version,
    });
    setStatus(pageStatus(), `Moved to ${t.label}.`, 'ok');
    await load();
  } catch (err) { handleMutationError(err); }
}

function renderDecision() {
  const mount = $('#decision-mount');
  clear(mount);
  if (!can('inspection.decide')) return;
  // No inline style attribute: the policy is style-src 'self' with no unsafe-inline,
  // so an inline style is refused and logged as a violation.
  const form = el('form', { class: 'panel-white mt-half' });
  const sel = el('select', { id: 'dec-outcome', name: 'decision' });
  fillSelect(sel, vocab.legal_outcomes, { blank: 'Select decision…', selected: inspection.decision || undefined });
  const note = el('textarea', { id: 'dec-note', name: 'note', minlength: '20', 'aria-describedby': 'dec-hint' });
  const status = el('div', { class: 'status-region', 'aria-live': 'polite' });
  form.append(
    el('h3', { text: 'Record final decision' }),
    status,
    el('div', { class: 'field' }, [el('label', { for: 'dec-outcome', text: 'Decision' }), sel]),
    el('div', { class: 'field' }, [
      el('label', { for: 'dec-note', text: 'Reasoned note' }),
      el('p', { class: 'hint', id: 'dec-hint', text: 'At least 20 characters. This is the officer\'s reasoning and is recorded permanently.' }),
      note,
    ]),
    el('div', { class: 'btn-row' }, [el('button', { class: 'btn btn-accent', type: 'submit', text: 'Record decision' })]),
  );
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    setStatus(status, '');
    if (!sel.value) { setStatus(status, 'Select a decision.', 'error'); return; }
    if (note.value.trim().length < 20) { setStatus(status, 'The note must be at least 20 characters.', 'error'); note.focus(); return; }
    try {
      await api.post(`/inspections/${inspectionId}/decision`, {
        decision: sel.value, note: note.value.trim(), expected_version: inspection.version,
      });
      setStatus(pageStatus(), 'Decision recorded.', 'ok');
      await load();
    } catch (err) { handleMutationError(err, status); }
  });
  mount.appendChild(form);
}

/* ---------- Run checks ---------- */
function wireRunChecks() {
  $('#run-checks').addEventListener('click', async () => {
    const btn = $('#run-checks');
    btn.disabled = true; btn.textContent = 'Running…';
    try {
      const res = await api.post(`/inspections/${inspectionId}/checks`, {});
      const msg = res && res.findings_written != null
        ? `Checks complete. ${res.findings_written} finding(s) written, ${res.rules_applied ?? res.rules_considered ?? 0} rule(s) applied.`
        : 'Checks complete.';
      setStatus(pageStatus(), msg, 'ok');
      if (res && Array.isArray(res.unreviewed_candidates) && res.unreviewed_candidates.length) {
        setStatus(pageStatus(), `${msg} Note: ${res.unreviewed_candidates.length} candidate(s) are still unreviewed.`, 'warn');
      }
      await load();
    } catch (err) { handleMutationError(err); }
    finally { btn.disabled = false; btn.textContent = 'Run checks'; }
  });
}

/* ---------- Upload + jobs ---------- */
function wireUpload() {
  $('#upload-form').addEventListener('submit', (e) => { e.preventDefault(); doUpload(); });
}

async function doUpload(overrideReason) {
  const status = $('#upload-status');
  setStatus(status, '');
  $('#up-quality').replaceChildren();
  const fileInput = $('#up-file');
  if (!fileInput.files.length) { setStatus(status, 'Choose an image file.', 'error'); return; }
  const fd = new FormData();
  fd.append('face', $('#up-face').value);
  fd.append('file', fileInput.files[0]);
  if (overrideReason) fd.append('quality_override_reason', overrideReason);
  const btn = $('#up-btn');
  btn.disabled = true; btn.textContent = 'Uploading…';
  try {
    const res = await api.postForm(`/inspections/${inspectionId}/evidence`, fd);
    renderQuality(res.quality);
    setStatus(status, res.message || 'Evidence uploaded. Analysis has been queued.', 'ok');
    if (res.job_id) pollJob(res.job_id);
    await load();
  } catch (err) {
    if (err instanceof ApiError && err.status === 422 && err.code === 'evidence_quality_insufficient') {
      renderQualityBlock(err.details && err.details.quality, fileInput.files[0]);
      setStatus(status, err.message || 'The image quality is insufficient. Review the signals below.', 'warn');
    } else {
      setStatus(status, err instanceof ApiError ? err.message : 'Upload failed.', 'error');
    }
  } finally {
    btn.disabled = false; btn.textContent = 'Upload & analyse';
  }
}

function renderQuality(quality) {
  const box = $('#up-quality');
  box.replaceChildren();
  if (!quality) return;
  box.appendChild(el('p', {}, [el('strong', { text: 'Quality: ' }), tag(quality.verdict, labelize(quality.verdict))]));
  const signals = quality.signals || [];
  if (signals.length) {
    const wrap = el('div', { class: 'panel' });
    for (const s of signals) {
      wrap.appendChild(el('div', { class: 'signal' }, [
        tag(s.severity === 'blocking' ? 'non_compliant' : s.severity === 'advisory' ? 'unable_to_determine' : 'compliant', labelize(s.severity)),
        el('span', {}, [document.createTextNode(`${s.message}${s.value != null ? ` (${s.value}${s.unit || ''}` : ''}${s.threshold != null ? ` / threshold ${s.threshold}${s.unit || ''})` : (s.value != null ? ')' : '')}`)]),
      ]));
    }
    box.appendChild(wrap);
  }
}

function renderQualityBlock(quality, file) {
  const box = $('#up-quality');
  box.replaceChildren();
  renderQuality(quality);
  const reason = el('textarea', { id: 'q-override', minlength: '15' });
  const form = el('div', { class: 'panel' }, [
    el('label', { for: 'q-override', class: 'form-label', text: 'Use this image anyway — record a reason (minimum 15 characters)' }),
    reason,
    el('div', { class: 'btn-row' }, [el('button', { class: 'btn btn-primary', type: 'button', text: 'Upload with reason' })]),
  ]);
  form.querySelector('button').addEventListener('click', () => {
    if (reason.value.trim().length < 15) { reason.focus(); return; }
    // Re-attach the file to the input so doUpload re-sends it.
    const dt = new DataTransfer();
    dt.items.add(file);
    $('#up-file').files = dt.files;
    doUpload(reason.value.trim());
  });
  box.appendChild(form);
}

function pollJob(jobId) {
  if (jobPollers.has(jobId)) return;
  const jobsBox = $('#jobs');
  const node = el('div', { class: 'job', id: `job-${jobId}` }, [
    el('p', { class: 'small' }, [el('strong', { text: 'Analysis job ' }), el('span', { class: 'mono', text: jobId })]),
    el('div', { class: 'progress-track' }, [el('div', { class: 'progress-fill' })]),
    el('p', { class: 'small', 'aria-live': 'polite', text: 'Queued…' }),
  ]);
  jobsBox.appendChild(node);
  const fill = node.querySelector('.progress-fill');
  const label = node.querySelector('p:last-child');

  const tick = async () => {
    try {
      const j = await api.get(`/jobs/${jobId}`);
      const pct = typeof j.progress === 'number' ? Math.round(j.progress * (j.progress <= 1 ? 100 : 1)) : 0;
      fill.style.width = `${pct}%`;
      label.textContent = `${labelize(j.state)} — ${j.stage || ''} ${pct ? `(${pct}%)` : ''}`.trim();
      if (j.state === 'queued' || j.state === 'running') {
        jobPollers.set(jobId, setTimeout(tick, 2000));
      } else {
        jobPollers.delete(jobId);
        if (j.state === 'failed') {
          label.textContent = `Analysis failed: ${j.failure_reason || 'unknown reason'}`;
          node.classList.add('finding', 'out-non_compliant');
        } else {
          label.textContent = 'Analysis complete.';
          await load();
        }
      }
    } catch (err) {
      jobPollers.delete(jobId);
      label.textContent = err instanceof ApiError ? `Could not read job status: ${err.message}` : 'Could not read job status.';
    }
  };
  tick();
}

/* ---------- Errors ---------- */
function handleMutationError(err, region) {
  const target = region || pageStatus();
  if (err instanceof ApiError && err.status === 409 && err.code === 'stale_record') {
    clear(target);
    const box = el('div', { class: 'notice notice-warn', role: 'alert' }, [
      el('p', { text: 'This record changed after you loaded it.' }),
      el('button', { class: 'btn btn-sm', type: 'button', text: 'Reload' }),
    ]);
    box.querySelector('button').addEventListener('click', () => load());
    target.replaceChildren(box);
    return;
  }
  setStatus(target, err instanceof ApiError ? err.message : 'The action could not be completed.', 'error');
}

main();
