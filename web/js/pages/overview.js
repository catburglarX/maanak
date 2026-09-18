// Overview: real counts derived from pagination totals and health components.
import { requireAuth, can } from '../workspace.js';
import { mountAppChrome } from '../layout.js';
import { api, readiness } from '../api.js';
import { $, el, clear, tag, showError, showEmpty, fmtDay, labelize } from '../util.js';
import { getVocabulary, labelFor } from '../chrome.js';

async function totalFor(endpoint, params) {
  try {
    const res = await api.get(endpoint, { page: 1, page_size: 1, ...params });
    return res && res.meta ? res.meta.total : null;
  } catch { return null; }
}

async function renderHealth() {
  const region = $('#health');
  const h = await readiness().catch(() => null);
  if (!h) return;
  const ok = h.status === 'ok' || h.status === 'ready' || h.status === 'healthy';
  const comps = h.components || {};
  const parts = Object.entries(comps).map(([k, v]) => `${labelize(k)}: ${v}`).join(' · ');
  region.appendChild(el('div', { class: ok ? 'notice notice-ok' : 'notice notice-warn', role: 'status' }, [
    el('p', {}, [el('strong', { text: `System status: ${h.status}. ` }), parts]),
  ]));
}

async function renderMetrics() {
  const grid = $('#metrics');
  const vocab = await getVocabulary().catch(() => null);
  const defs = [
    ['Inspections in officer review', '/inspections', { state: 'officer_review' }],
    ['Inspections in reviewer review', '/inspections', { state: 'reviewer_review' }],
    ['Inspections awaiting evidence', '/inspections', { state: 'evidence_pending' }],
    ['Unassigned complaints', '/complaints', { unassigned: true }],
    ['Complaints received', '/complaints', { state: 'received' }],
    ['Overdue cases', '/cases', { overdue_only: true }],
    ['Rules under review', '/rules', { status: 'under_review' }],
    ['Reports issued', '/reports', {}],
  ];
  const results = await Promise.all(defs.map(([, ep, p]) => totalFor(ep, p)));
  clear(grid);
  grid.removeAttribute('aria-busy');
  let any = false;
  defs.forEach(([label], i) => {
    const n = results[i];
    if (n === null) return;
    any = true;
    grid.appendChild(el('div', { class: 'metric' }, [
      el('span', { class: 'num', text: String(n) }),
      el('span', { class: 'lbl', text: label }),
    ]));
  });
  if (!any) showEmpty(grid, 'No counts available', 'The workspace could not be queried, or you do not have access to these registers.');
}

async function renderMine() {
  const box = $('#mine');
  try {
    const vocab = await getVocabulary().catch(() => null);
    const res = await api.get('/inspections', { page: 1, page_size: 10, assigned_to_me: true, decided: false, sort: 'updated_desc' });
    const items = res.items || [];
    clear(box);
    if (!items.length) {
      showEmpty(box, 'Nothing assigned to you', 'Inspections assigned to you and not yet decided will appear here.');
      return;
    }
    const table = el('table', { class: 'register' });
    table.appendChild(el('caption', { text: 'Open inspections assigned to you' }));
    const thead = el('thead', {}, [el('tr', {}, [
      el('th', { scope: 'col', text: 'Reference' }),
      el('th', { scope: 'col', text: 'State' }),
      el('th', { scope: 'col', text: 'Product' }),
      el('th', { scope: 'col', text: 'Updated' }),
    ])]);
    table.appendChild(thead);
    const tbody = el('tbody');
    for (const it of items) {
      const product = it.product ? [it.product.brand, it.product.name].filter(Boolean).join(' — ') : '—';
      tbody.appendChild(el('tr', {}, [
        el('th', { scope: 'row' }, [el('a', { href: `/app/inspection.html?id=${encodeURIComponent(it.id)}`, text: it.reference })]),
        el('td', {}, [tag(it.state, vocab ? labelFor(vocab.inspection_states, it.state) : undefined)]),
        el('td', { text: product }),
        el('td', { text: fmtDay(it.updated_at) }),
      ]));
    }
    table.appendChild(tbody);
    box.appendChild(el('div', { class: 'table-wrap' }, [table]));
  } catch (err) {
    showError(box, err);
  }
}

async function main() {
  mountAppChrome();
  const me = await requireAuth();
  if (!me) return;
  renderHealth();
  renderMetrics();
  renderMine();
}
main();
