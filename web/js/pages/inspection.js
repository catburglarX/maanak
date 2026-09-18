// Inspection detail: the split evidence-review screen controller.
import { requireAuth, can } from '../workspace.js';
import { mountAppChrome } from '../layout.js';
import { api, ApiError } from '../api.js';
import {
  $, el, clear, tag, fmtDate, fmtDay, labelize, counted, productLabel, NOT_RECORDED,
  setStatus, showError, fillSelect,
} from '../util.js';
import { getVocabulary, labelFor } from '../chrome.js';
import { reasonDialog, confirmDialog } from '../dialog.js';
import { createViewer } from './inspection-viewer.js';
import { takePhotograph, cameraUnavailableReason } from './inspection-camera.js';

const params = new URLSearchParams(location.search);
const inspectionId = params.get('id');

let vocab = null;
let inspection = null;
let viewer = null;
let selectedEvidence = null;
let selectedCandidateId = null;
const jobPollers = new Map();

const pageStatus = () => $('#page-status');

// Mirrors FROZEN_STATES in app/domain/enums.py: after these, evidence and findings are
// read-only and the service refuses a change.
const FROZEN_STATES = ['report_issued', 'case_opened', 'closed', 'archived'];

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
  renderActions();
}

/* ---------- Context ---------- */
function renderContext() {
  const box = $('#context-body');
  box.removeAttribute('aria-busy');
  clear(box);
  const p = inspection.product;

  box.appendChild(el('div', { class: 'between' }, [
    el('div', {}, [
      el('h1', { text: `Inspection ${inspection.reference}` }),
      el('div', { class: 'btn-row' }, [
        tag(inspection.state, vlabel('inspection_states', inspection.state)),
        inspection.decision ? tag(inspection.decision, vlabel('decision_outcomes', inspection.decision)) : null,
      ]),
    ]),
    el('div', { class: 'btn-row' }, [
      // Editing is refused once the record is frozen, which the service enforces. The
      // control is offered only while a change is still possible, so the officer is not
      // invited to open a form that cannot be saved.
      can('inspection.update') && !FROZEN_STATES.includes(inspection.state)
        ? (() => {
            const btn = el('button', { class: 'btn btn-sm', type: 'button', text: 'Edit details' });
            btn.addEventListener('click', editDetails);
            return btn;
          })()
        : null,
      el('a', { class: 'btn btn-sm', href: '/app/inspections.html', text: 'Back to register' }),
    ]),
  ]));
  const dl = el('dl', { class: 'defs' });
  const add = (k, v) => {
    dl.appendChild(el('dt', { text: k }));
    const value = typeof v === 'string' || typeof v === 'number'
      ? document.createTextNode(String(v))
      : (v || document.createTextNode(NOT_RECORDED));
    dl.appendChild(el('dd', {}, [value]));
  };
  add('Product', p ? productLabel(p) : 'Not yet recorded');
  if (p && p.brand && p.name && !p.name.toLowerCase().startsWith(p.brand.toLowerCase())) add('Brand', p.brand);
  if (p && p.commodity_category) add('Category', labelize(p.commodity_category));
  if (p && p.declared_net_quantity) add('Declared net quantity', `${p.declared_net_quantity} ${p.declared_net_quantity_unit || ''}`.trim());
  add('Source', vlabel('inspection_sources', inspection.source));
  add('Inspection date', fmtDay(inspection.inspection_date));
  if (inspection.premises_name) add('Premises', inspection.premises_name);
  if (inspection.premises_address) add('Premises address', inspection.premises_address);
  // Recorded on the inspection and editable, but never shown until now, so a
  // correction saved without any visible effect.
  if (inspection.marketplace_name) add('Marketplace', inspection.marketplace_name);
  if (inspection.listing_url) add('Listing', el('a', { href: inspection.listing_url, rel: 'noopener noreferrer', target: '_blank', text: inspection.listing_url }));
  if (inspection.batch_reference) add('Batch reference', inspection.batch_reference);
  if (inspection.jurisdiction_name) add('Jurisdiction', inspection.jurisdiction_name);
  add('Evidence files', inspection.evidence_count);
  add('Readings awaiting review', inspection.pending_candidate_count);
  add('Rules evaluated', inspection.rules_evaluated_count ?? 0);
  if (inspection.checks_executed_at) add('Checks last run', fmtDate(inspection.checks_executed_at));
  // The record version, used for the optimistic-concurrency check on every write.
  add('Record version', inspection.version);
  if (inspection.decision_note) add('Decision note', inspection.decision_note);
  box.appendChild(dl);
}

// Correcting the premises, the marketplace or the batch reference. These are typed from
// a shop floor and get typed wrong; the API accepted a correction all along but nothing
// offered one.
async function editDetails() {
  const { openDialog } = await import('../dialog.js');
  const text = (id, value, max) => el('input', { type: 'text', id, value: value || '', maxlength: String(max) });
  const premises = text('ed-prem', inspection.premises_name, 240);
  const address = el('textarea', { id: 'ed-addr', maxlength: '1000' });
  address.value = inspection.premises_address || '';
  const marketplace = text('ed-market', inspection.marketplace_name, 160);
  const listing = el('input', { type: 'url', id: 'ed-listing', value: inspection.listing_url || '', maxlength: '2000' });
  const batch = text('ed-batch', inspection.batch_reference, 60);
  const status = el('div', { class: 'status-region', 'aria-live': 'polite' });
  const form = el('form', {}, [
    status,
    el('div', { class: 'field' }, [el('label', { for: 'ed-prem', text: 'Premises name' }), premises]),
    el('div', { class: 'field' }, [el('label', { for: 'ed-addr', text: 'Premises address' }), address]),
    el('div', { class: 'field' }, [el('label', { for: 'ed-market', text: 'Marketplace' }), el('p', { class: 'hint', text: 'For an inspection of an online listing rather than a shop.' }), marketplace]),
    el('div', { class: 'field' }, [el('label', { for: 'ed-listing', text: 'Listing address' }), listing]),
    el('div', { class: 'field' }, [el('label', { for: 'ed-batch', text: 'Batch reference' }), batch]),
  ]);
  const dlg = openDialog({
    title: 'Edit inspection details',
    body: form,
    actions: [
      { label: 'Cancel', value: null },
      {
        label: 'Save', class: 'btn-primary', close: false,
        onClick: async () => {
          const value = (control) => (control.value.trim() ? control.value.trim() : null);
          try {
            await api.patch(`/inspections/${inspectionId}`, {
              premises_name: value(premises),
              premises_address: value(address),
              marketplace_name: value(marketplace),
              listing_url: value(listing),
              batch_reference: value(batch),
              expected_version: inspection.version,
            });
            dlg.close();
            setStatus(pageStatus(), 'Inspection details updated.', 'ok');
            await load();
          } catch (err) { handleMutationError(err, status); }
          return false;
        },
      },
    ],
  });
}

/* ---------- Coverage ---------- */function renderCoverage() {
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
        el('span', { class: 'small', text: counted(f.evidence_count, 'image') }),
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
    const names = outstanding.map((v) => vlabel('package_faces', v)).join(', ');
    list.parentElement.insertBefore(
      el('div', { class: 'notice notice-warn', role: 'status' }, [
        el('p', {}, [
          el('strong', { text: `Still to capture: ` }),
          document.createTextNode(`${names}. A face can also be marked absent or not applicable, with a reason.`),
        ]),
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
  // The original bytes, exactly as uploaded, with the filename the officer used. This
  // is the file the SHA-256 in the report was taken over.
  const download = $('#download-original');
  if (download && can('evidence.download_original')) {
    download.hidden = false;
    download.href = `/api/v1/inspections/${encodeURIComponent(inspectionId)}/evidence/${encodeURIComponent(ev.id)}/download`;
    download.setAttribute('download', ev.original_filename || 'evidence');
    download.title = `Download ${ev.original_filename || 'the original file'} as uploaded`;
  }
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
  if (!show) return;
  if (selectedEvidence) loadOcr(selectedEvidence);
  else $('#ocr-raw').textContent = 'Select a package face first, then the text read from it appears here.';
});

// The API reports the Tesseract language string as it was passed to the engine, for
// example "eng+hin". Treating it as an array and calling join on it threw a TypeError
// that the catch below reported as "Could not load OCR output.", so this panel showed a
// failure for every image while the endpoint itself was returning the text correctly.
function languageList(value) {
  if (Array.isArray(value)) return value.join(', ');
  if (typeof value === 'string' && value.trim()) return value.split('+').join(', ');
  return 'not recorded';
}

async function loadOcr(ev) {
  const raw = $('#ocr-raw');
  raw.textContent = 'Loading the text read from this image…';
  try {
    const res = await api.get(`/inspections/${inspectionId}/evidence/${ev.id}/ocr`);
    if (!res || res.available === false) {
      raw.textContent = res && res.failure_reason ? `No text could be read: ${res.failure_reason}` : 'No text has been read from this image yet.';
      return;
    }
    const confidence = typeof res.mean_confidence === 'number'
      ? `${Math.round(res.mean_confidence * (res.mean_confidence <= 1 ? 100 : 1))}%`
      : 'not measured';
    const header = [
      `Engine: ${[res.engine, res.engine_version].filter(Boolean).join(' ') || 'not recorded'}`,
      `Languages: ${languageList(res.languages)}`,
      `Mean confidence: ${confidence} · Words read: ${res.word_count ?? 'not counted'}`,
      '',
      '',
    ].join('\n');
    raw.textContent = header + (res.raw_text || '(no text was detected in this image)');
  } catch (err) {
    raw.textContent = err instanceof ApiError
      ? `The text could not be loaded: ${err.message}`
      : 'The text could not be loaded. Reload the page and try again.';
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
    el('span', { class: 'conf', text: c.machine_confidence != null ? `Confidence: ${(c.machine_confidence * (c.machine_confidence <= 1 ? 100 : 1)).toFixed(0)}%` : 'Confidence not measured' }),
  ]));

  // Two SEPARATE labelled states. Never merged.
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

function findingTitle(f) {
  if (f.declaration_type) return vlabel('declaration_types', f.declaration_type);
  const rule = f.rule || {};
  if (rule.title) return rule.title;
  const kind = f.test_inputs && f.test_inputs.test_kind;
  return kind ? labelize(kind) : 'Rule test';
}

function renderFinding(f) {
  const outcome = f.outcome;
  const node = el('div', { class: `finding out-${outcome}` });
  node.appendChild(el('div', { class: 'between' }, [
    // A rule that accepts several declarations, such as date marking accepting any of
    // manufacture, packing or import, carries no single declaration_type and returns
    // null. Titling the card from that alone left it blank, so the rule's own title is
    // used when there is no one declaration to name.
    el('strong', { text: findingTitle(f) }),
    el('div', { class: 'btn-row' }, [
      el('span', { class: 'label-pair' }, [el('span', { class: 'k', text: 'Engine outcome' }), tag(outcome, vlabel('legal_outcomes', outcome))]),
      f.officer_outcome ? el('span', { class: 'label-pair' }, [el('span', { class: 'k', text: 'Officer override' }), tag(f.officer_outcome, vlabel('legal_outcomes', f.officer_outcome))]) : null,
    ]),
  ]));
  if (f.explanation) node.appendChild(el('p', { text: f.explanation }));
  if (f.officer_note) node.appendChild(el('p', { class: 'small' }, [el('strong', { text: 'Officer note: ' }), document.createTextNode(f.officer_note)]));

  // Only the side that has a value. Rendering both unconditionally printed
  // "Expected: Not recorded" for a rule that tests presence and so has nothing to
  // compare against, which reads as a missing record rather than as nothing to show.
  if (f.expected_value != null || f.observed_value != null) {
    const cmp = el('dl', { class: 'compare' });
    if (f.expected_value != null) cmp.append(el('dt', { text: 'Expected' }), el('dd', { text: f.expected_value }));
    if (f.observed_value != null) cmp.append(el('dt', { text: 'Observed' }), el('dd', { text: f.observed_value }));
    node.appendChild(cmp);
  }

  // Calculation: monospace auditable maths.
  if (Array.isArray(f.calculation) && f.calculation.length) {
    const list = el('ol', {});
    for (const step of f.calculation) list.appendChild(el('li', { text: step }));
    node.appendChild(el('div', {}, [el('p', { class: 'small muted', text: 'Calculation' }), el('div', { class: 'mono-list' }, [list])]));
  }

  // Rule block.
  const r = f.rule || {};
  if (r.code) {
    const rb = el('div', { class: 'rule-block' });
    rb.appendChild(el('p', { class: 'rule-title' }, [
      el('strong', { text: r.title || 'Rule' }),
      el('span', { class: 'small mono rule-code', text: `${r.code} v${r.version}` }),
    ]));
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

/* ---------- Check summary, transitions, decision ---------- */
function renderActions() {
  renderFindingsSummary();
  if (can('checks.run')) $('#run-checks').hidden = false;
  renderTransitions();
  renderDecision();
  renderReport();
}

/* ---------- Report, and the case that can follow it ---------- */
// The API could issue a report and open a case from the moment an inspection was
// decided, and the seeder did exactly that, but no screen offered either. An officer
// working through the interface reached a recorded decision and stopped.
function renderReport() {
  const section = $('#report-section');
  const mount = $('#report-mount');
  clear(mount);

  const reports = inspection.reports || [];
  const decided = !!inspection.decision;
  const mayIssue = can('report.issue');
  const mayOpenCase = can('case.create');

  if (!reports.length && !decided) {
    section.hidden = true;
    return;
  }
  section.hidden = false;

  if (!reports.length) {
    mount.appendChild(el('p', {}, [
      document.createTextNode('This inspection has a recorded decision, so a report can be issued from it. '),
      document.createTextNode('Issuing freezes the evidence, the readings and the findings, then renders the documents from that frozen record.'),
    ]));
    if (!mayIssue) {
      mount.appendChild(el('p', { class: 'small muted', text: 'Issuing a report needs the reviewer or controller role.' }));
      return;
    }
    const btn = el('button', { class: 'btn btn-accent', type: 'button', text: 'Issue report' });
    btn.addEventListener('click', () => issueReport(btn));
    mount.appendChild(el('div', { class: 'btn-row' }, [btn]));
    return;
  }

  for (const report of reports) {
    const panel = el('div', { class: 'panel-white' });
    panel.appendChild(el('div', { class: 'between' }, [
      el('h3', { class: 'mt-0', text: report.reference }),
      tag(report.state, vlabel('report_states', report.state)),
    ]));
    const dl = el('dl', { class: 'defs' });
    const add = (k, v) => { if (v) { dl.append(el('dt', { text: k }), el('dd', {}, [typeof v === 'string' ? document.createTextNode(v) : v])); } };
    add('Revision', String(report.revision ?? 1));
    add('Issued', report.issued_at ? fmtDate(report.issued_at) : null);
    add('Content hash', el('span', { class: 'mono small', text: report.snapshot_sha256 || '' }));
    add('Verification code', el('span', { class: 'mono', text: report.verification_code || '' }));
    panel.appendChild(dl);

    const links = el('div', { class: 'btn-row' }, [
      el('a', { class: 'btn btn-sm', href: `/api/v1/reports/${report.id}/document.pdf`, target: '_blank', rel: 'noopener', text: 'Open the PDF' }),
      el('a', { class: 'btn btn-sm', href: `/api/v1/reports/${report.id}/document.docx`, target: '_blank', rel: 'noopener', text: 'Open the document' }),
      report.verification_url ? el('a', { class: 'btn btn-sm', href: report.verification_url, target: '_blank', rel: 'noopener', text: 'Public verification page' }) : null,
    ]);
    panel.appendChild(links);
    mount.appendChild(panel);
  }

  // A case can only follow an issued report, which the service enforces.
  const issued = reports.find((r) => r.state === 'issued');
  if (issued && mayOpenCase) {
    mount.appendChild(el('h3', { text: 'Enforcement case' }));
    mount.appendChild(el('p', { text: 'A case carries the report into correspondence with the party responsible for the package. Opening one does not issue any notice by itself.' }));
    const btn = el('button', { class: 'btn', type: 'button', text: 'Open a case' });
    btn.addEventListener('click', () => openCase(issued));
    mount.appendChild(el('div', { class: 'btn-row' }, [btn]));
  }
}

async function issueReport(btn) {
  const confirmed = await confirmDialog(
    'Issue the report',
    'This freezes the evidence, the readings and the findings for this inspection. They cannot be changed afterwards. The documents are rendered from that frozen record.',
    { confirmLabel: 'Issue report' },
  );
  if (!confirmed) return;
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = 'Issuing…';
  try {
    const res = await api.post(`/reports/inspections/${inspectionId}`, {});
    setStatus($('#report-status'), `Report ${res.reference} issued.`, 'ok');
    await load();
  } catch (err) {
    handleMutationError(err, $('#report-status'));
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

async function openCase(report) {
  const { openDialog } = await import('../dialog.js');
  const product = inspection.product || {};
  const name = el('input', { type: 'text', id: 'oc-name', maxlength: '240', value: product.brand || '' });
  const roleSel = el('select', { id: 'oc-role' });
  fillSelect(roleSel, [
    { value: '', label: 'Not stated' },
    { value: 'manufacturer', label: 'Manufacturer' },
    { value: 'packer', label: 'Packer' },
    { value: 'importer', label: 'Importer' },
    { value: 'marketer', label: 'Marketer' },
    { value: 'brand_owner', label: 'Brand owner' },
  ], {});
  const subject = el('textarea', { id: 'oc-subject', maxlength: '300' });
  subject.value = `${productLabel(product)}: ${vlabel('decision_outcomes', inspection.decision)} recorded in ${inspection.reference}`.slice(0, 300);
  const address = el('textarea', { id: 'oc-address', maxlength: '1000' });
  const form = el('form', {}, [
    el('div', { class: 'field' }, [el('label', { for: 'oc-name', text: 'Respondent name' }), el('p', { class: 'hint', text: 'The party the case is against, as it appears on the package.' }), name]),
    el('div', { class: 'field' }, [el('label', { for: 'oc-role', text: 'Respondent role' }), roleSel]),
    el('div', { class: 'field' }, [el('label', { for: 'oc-subject', text: 'Subject' }), el('p', { class: 'hint', text: 'At least 10 characters. This heads the case file.' }), subject]),
    el('div', { class: 'field' }, [el('label', { for: 'oc-address', text: 'Respondent address' }), address]),
  ]);
  const dlg = openDialog({
    title: 'Open a case',
    body: form,
    actions: [
      { label: 'Cancel', value: null },
      {
        label: 'Open case', class: 'btn-primary', close: false,
        onClick: async () => {
          if (name.value.trim().length < 2) { name.focus(); return false; }
          if (subject.value.trim().length < 10) { subject.focus(); return false; }
          try {
            const res = await api.post('/cases', {
              inspection_id: inspection.id,
              respondent_name: name.value.trim(),
              respondent_role: roleSel.value || undefined,
              respondent_address: address.value.trim() || undefined,
              subject: subject.value.trim(),
            });
            dlg.close();
            location.href = `/app/case.html?id=${encodeURIComponent(res.id)}`;
          } catch (err) {
            handleMutationError(err, $('#report-status'));
          }
          return false;
        },
      },
    ],
  });
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
    box.appendChild(el('p', { class: 'muted', text: 'The checks have not been run on this inspection yet.' }));
    return;
  }

  const panel = el('div', { class: 'check-summary' });

  // The tally. Outcome labels are lower-cased inside the count so each pill reads as
  // a phrase: "7 compliant", not "7 Compliant".
  panel.appendChild(el('p', { class: 'tally' }, [
    el('span', { class: 'tally-count', text: counted(total, 'finding') }),
    ...tallies.map(([outcome, n]) => tag(outcome, `${n} ${vlabel('legal_outcomes', outcome).toLowerCase()}`)),
  ]));

  if (fs.suggested_outcome) {
    panel.appendChild(el('div', { class: 'suggested' }, [
      el('span', { class: 'k', text: 'Suggested outcome' }),
      tag(fs.suggested_outcome, vlabel('legal_outcomes', fs.suggested_outcome)),
      fs.rationale ? el('span', { class: 'rationale', text: fs.rationale }) : null,
    ]));
  }
  if (fs.note) {
    panel.appendChild(el('p', { class: 'small muted standing-note', text: fs.note }));
  }
  box.appendChild(panel);
}

function renderTransitions() {
  const box = $('#transitions');
  const section = $('#next-section');
  const sub = $('#next-sub');
  clear(box);
  const transitions = inspection.available_transitions || [];
  if (!transitions.length) {
    section.hidden = true;
    return;
  }
  section.hidden = false;
  const blockedCount = transitions.filter((t) => Array.isArray(t.blocked_by) && t.blocked_by.length).length;
  sub.textContent = blockedCount
    ? 'These are the moves open from the current state. A blocked move says what is missing.'
    : 'These are the moves open from the current state.';
  for (const t of transitions) {
    const blocked = Array.isArray(t.blocked_by) && t.blocked_by.length > 0;
    const btn = el('button', {
      class: 'btn btn-sm', type: 'button',
      disabled: blocked,
      'aria-disabled': String(blocked),
      text: t.label || labelize(t.target_state),
    });
    if (!blocked) btn.addEventListener('click', () => doTransition(t));
    const wrap = el('div', { class: blocked ? 'transition blocked' : 'transition' }, [btn]);
    if (blocked) wrap.appendChild(el('span', { class: 'blocked-reason', text: t.blocked_by.join(' ') }));
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

  // A decision can only be recorded from reviewer_review, and the state machine has
  // guards behind it. The form used to render in every state, so an officer filled in
  // a reasoned note and only then learned the inspection was not at that stage yet.
  // The available transitions say whether a decision is reachable right now, so they
  // decide whether to offer the form or explain what has to happen first.
  const transitions = inspection.available_transitions || [];
  const decisionTargets = (vocab.decision_outcomes || []).map((o) => o.value);
  const open = transitions.filter((t) => decisionTargets.includes(t.target_state));
  const reachable = open.filter((t) => !(Array.isArray(t.blocked_by) && t.blocked_by.length));

  if (!open.length) {
    if (inspection.decision) {
      mount.appendChild(el('p', {}, [
        document.createTextNode('The decision on record is '),
        el('strong', { text: vlabel('decision_outcomes', inspection.decision) }),
        document.createTextNode('. Reopening it is offered under Next step, and the previous decision stays in the record.'),
      ]));
    } else {
      mount.appendChild(el('p', { text: 'No decision can be recorded from the current state. Send the inspection for reviewer decision first, under Next step below.' }));
    }
    return;
  }
  if (!reachable.length) {
    const blockers = [...new Set(open.flatMap((t) => t.blocked_by || []))];
    mount.appendChild(el('div', { class: 'notice notice-warn', role: 'status' }, [
      el('p', {}, [el('strong', { text: 'Not ready for a decision yet. ' }), document.createTextNode(blockers.join(' '))]),
    ]));
    return;
  }

  // No inline style attribute: the policy is style-src 'self' with no unsafe-inline,
  // so an inline style is refused and logged as a violation.
  const form = el('form', { class: 'panel-white mt-half' });
  const sel = el('select', { id: 'dec-outcome', name: 'decision' });
  // decision_outcomes, not legal_outcomes. The two are different vocabularies: a
  // single rule test can return "not applicable" or "additional evidence required",
  // an inspection decision cannot. Populating this from legal_outcomes offered three
  // values the endpoint rejects and omitted "violation found" altogether, which is
  // the one an officer needs most.
  fillSelect(sel, vocab.decision_outcomes, { blank: 'Select a decision…', selected: inspection.decision || undefined });
  const note = el('textarea', { id: 'dec-note', name: 'note', minlength: '20', 'aria-describedby': 'dec-hint' });
  const status = el('div', { class: 'status-region', 'aria-live': 'polite' });
  form.append(
    el('h3', { text: 'Record the decision' }),
    status,
    el('div', { class: 'field' }, [el('label', { for: 'dec-outcome', text: 'Decision' }), sel]),
    el('div', { class: 'field' }, [
      el('label', { for: 'dec-note', text: 'Reason' }),
      el('p', { class: 'hint', id: 'dec-hint', text: 'At least 20 characters. This is your reasoning, it is recorded against your name, and it is printed in the report.' }),
      note,
    ]),
    el('div', { class: 'btn-row' }, [el('button', { class: 'btn btn-accent', type: 'submit', text: 'Record decision' })]),
  );
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    setStatus(status, '');
    if (!sel.value) { setStatus(status, 'Choose a decision first.', 'error'); return; }
    if (note.value.trim().length < 20) { setStatus(status, 'The reason needs at least 20 characters.', 'error'); note.focus(); return; }
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
      const written = res && res.findings_written != null ? res.findings_written : null;
      const applied = res && (res.rules_applied ?? res.rules_considered ?? 0);
      const msg = written != null
        ? `Checks complete. ${counted(written, 'finding')} written from ${counted(applied, 'rule')}.`
        : 'Checks complete.';
      setStatus(pageStatus(), msg, 'ok');
      if (res && Array.isArray(res.unreviewed_candidates) && res.unreviewed_candidates.length) {
        const n = res.unreviewed_candidates.length;
        setStatus(pageStatus(), `${msg} ${counted(n, 'reading')} ${n === 1 ? 'is' : 'are'} still unreviewed and did not reach the rule engine.`, 'warn');
      }
      await load();
    } catch (err) { handleMutationError(err); }
    finally { btn.disabled = false; btn.textContent = 'Run checks'; }
  });
}

/* ---------- Upload + jobs ---------- */

// The image waiting to be uploaded. A camera capture never touches the file input, so
// the two routes share one place to hold what was chosen.
let pendingFile = null;

function describeChosen() {
  const box = $('#up-chosen');
  clear(box);
  if (!pendingFile) return;
  const kb = Math.round(pendingFile.size / 1024);
  box.appendChild(el('p', { class: 'small' }, [
    el('strong', { text: 'Ready to upload: ' }),
    document.createTextNode(`${pendingFile.name} (${kb} KB)`),
  ]));
}

function wireUpload() {
  $('#upload-form').addEventListener('submit', (e) => { e.preventDefault(); doUpload(); });
  $('#up-file').addEventListener('change', () => {
    pendingFile = $('#up-file').files[0] || null;
    describeChosen();
  });

  const camera = $('#up-camera');
  const hint = $('#up-camera-hint');
  const reason = cameraUnavailableReason();
  hint.textContent = reason
    ? `${reason} Either way the photograph is stored unchanged and hashed on arrival.`
    : 'The camera opens here, so you can check the panel fills the frame before the photograph is kept.';
  camera.addEventListener('click', async () => {
    const face = $('#up-face');
    const label = face.options[face.selectedIndex] ? face.options[face.selectedIndex].text : '';
    camera.disabled = true;
    try {
      const file = await takePhotograph({ face: label });
      if (!file) return;
      pendingFile = file;
      describeChosen();
      // Clear the file input so there is one unambiguous source for the upload.
      $('#up-file').value = '';
      setStatus($('#upload-status'), 'Photograph taken. Upload it to have it read.', 'ok');
    } finally {
      camera.disabled = false;
    }
  });
}

async function doUpload(overrideReason) {
  const status = $('#upload-status');
  setStatus(status, '');
  $('#up-quality').replaceChildren();
  if (!pendingFile) {
    setStatus(status, 'Choose an image file, or take a photograph.', 'error');
    return;
  }
  const fd = new FormData();
  fd.append('face', $('#up-face').value);
  fd.append('file', pendingFile, pendingFile.name);
  // The client clock, recorded alongside the server's own arrival time. The endpoint
  // accepted this from the start and nothing was sending it, so every record carried a
  // server time and no device time to compare it against.
  fd.append('capture_client_time', new Date().toISOString());
  if (overrideReason) fd.append('quality_override_reason', overrideReason);
  const btn = $('#up-btn');
  btn.disabled = true; btn.textContent = 'Uploading…';
  try {
    const res = await api.postForm(`/inspections/${inspectionId}/evidence`, fd);
    renderQuality(res.quality);
    setStatus(status, res.message || 'Evidence stored and queued for reading.', 'ok');
    if (res.job_id) pollJob(res.job_id);
    pendingFile = null;
    $('#up-file').value = '';
    describeChosen();
    await load();
  } catch (err) {
    if (err instanceof ApiError && err.status === 422 && err.code === 'evidence_quality_insufficient') {
      renderQualityBlock(err.details && err.details.quality, pendingFile);
      setStatus(status, err.message || 'This image is not good enough to read reliably. The measurements are below.', 'warn');
    } else {
      setStatus(status, err instanceof ApiError ? err.message : 'The upload did not complete.', 'error');
    }
  } finally {
    btn.disabled = false; btn.textContent = 'Upload and read';
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
    el('label', { for: 'q-override', class: 'form-label', text: 'Use this image anyway' }),
    el('p', { class: 'hint', text: 'Say why the image is good enough despite the signals above. At least 15 characters, and it is kept with the evidence.' }),
    reason,
    el('div', { class: 'btn-row' }, [el('button', { class: 'btn btn-primary', type: 'button', text: 'Upload with this reason' })]),
  ]);
  form.querySelector('button').addEventListener('click', () => {
    if (reason.value.trim().length < 15) { reason.focus(); return; }
    // The rejected image is held in pendingFile already, and a camera capture was never
    // in the file input to begin with, so a DataTransfer round trip would lose it.
    pendingFile = file;
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
      label.textContent = [labelize(j.state), j.stage, pct ? `(${pct}%)` : ''].filter(Boolean).join(' ');
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
