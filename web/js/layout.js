// Workspace layout: injects the app header/nav, and a reusable paginated
// register that fetches pages and renders rows.
import { el, $, clear, showLoading, showEmpty, showError } from './util.js';
import { api } from './api.js';

// The work an officer does every day. These carry the visual weight.
const PRIMARY_NAV = [
  ['/app/overview.html', 'Overview'],
  ['/app/inspections.html', 'Inspections'],
  ['/app/complaints.html', 'Complaints'],
  ['/app/cases.html', 'Cases'],
  ['/app/rules.html', 'Rules'],
];

// Reference registers and administration: reached often enough to belong in the
// header, but they should not compete with the primary work.
const SECONDARY_NAV = [
  ['/app/products.html', 'Products'],
  ['/app/reports.html', 'Reports'],
  ['/app/audit.html', 'Audit'],
  ['/app/admin.html', 'Administration'],
];

export function mountAppChrome() {
  const mount = $('#app-header');
  if (!mount) return;
  const current = location.pathname.split('/').pop();
  const brand = el('a', { class: 'brand', href: '/app/overview.html' }, [
    // The light variant: the primary mark's wordmark is #101820, which is invisible
    // on the near-black workspace header. The artwork already sets the name, so the
    // only text beside it is the label separating this from the public site.
    el('img', { src: '/assets/maanak-logo-light.svg', alt: 'Maanak' }),
    el('span', { class: 'brand-sub', text: 'Workspace' }),
  ]);

  const nav = el('nav', { class: 'app-nav', 'aria-label': 'Workspace' });
  const addLinks = (group, className) => {
    const set = el('div', { class: className });
    for (const [href, label] of group) {
      const a = el('a', { href, text: label });
      if (href.endsWith(current)) a.setAttribute('aria-current', 'page');
      set.appendChild(a);
    }
    nav.appendChild(set);
  };
  addLinks(PRIMARY_NAV, 'nav-primary');
  addLinks(SECONDARY_NAV, 'nav-secondary');
  const userBar = el('div', { class: 'app-user', id: 'user-bar' });
  const header = el('header', { class: 'app-header' }, [
    el('div', { class: 'bar' }, [brand, nav, userBar]),
  ]);
  mount.replaceWith(header);
}

// Reusable paginated register.
// opts: { endpoint, params(), columns:[{key,label,render(item)}], rowHref(item), caption, emptyTitle, emptyDetail }
export function createRegister(container, opts) {
  let page = 1;
  const pageSize = opts.pageSize || 25;

  async function load() {
    showLoading(container, 'Loading…');
    try {
      const params = { page, page_size: pageSize, ...(opts.params ? opts.params() : {}) };
      const res = await api.get(opts.endpoint, params);
      render(res);
    } catch (err) {
      showError(container, err);
    }
  }

  function render(res) {
    clear(container);
    container.removeAttribute('aria-busy');
    const items = res.items || [];
    const meta = res.meta || { page: 1, total: 0, total_pages: 1, has_next: false, has_previous: false };
    if (!items.length) {
      showEmpty(container, opts.emptyTitle || 'Nothing to show', opts.emptyDetail || 'There are no records matching your filters yet.');
      renderPager(meta);
      return;
    }
    const table = el('table', { class: 'register' });
    if (opts.caption) table.appendChild(el('caption', { text: opts.caption }));
    const thead = el('thead');
    const trh = el('tr');
    for (const c of opts.columns) trh.appendChild(el('th', { scope: 'col', text: c.label }));
    thead.appendChild(trh);
    table.appendChild(thead);
    const tbody = el('tbody');
    for (const item of items) {
      const tr = el('tr');
      opts.columns.forEach((c, i) => {
        const content = c.render ? c.render(item) : (item[c.key] ?? '—');
        const cell = el(i === 0 ? 'th' : 'td', i === 0 ? { scope: 'row' } : {});
        if (i === 0 && opts.rowHref) {
          cell.appendChild(el('a', { href: opts.rowHref(item) }, [typeof content === 'string' ? document.createTextNode(content) : content]));
        } else {
          cell.appendChild(typeof content === 'string' ? document.createTextNode(content) : content);
        }
        tr.appendChild(cell);
      });
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    container.appendChild(el('div', { class: 'table-wrap' }, [table]));
    renderPager(meta);
  }

  function renderPager(meta) {
    const pager = el('div', { class: 'pager' });
    const prev = el('button', { class: 'btn btn-sm', type: 'button', text: 'Previous', disabled: !meta.has_previous });
    const next = el('button', { class: 'btn btn-sm', type: 'button', text: 'Next', disabled: !meta.has_next });
    prev.addEventListener('click', () => { if (meta.has_previous) { page -= 1; load(); } });
    next.addEventListener('click', () => { if (meta.has_next) { page += 1; load(); } });
    pager.appendChild(prev);
    pager.appendChild(next);
    pager.appendChild(el('span', { class: 'meta', 'aria-live': 'polite', text: `Page ${meta.page} of ${meta.total_pages} · ${meta.total} total` }));
    container.appendChild(pager);
  }

  return {
    reload() { page = 1; load(); },
    load,
    goFirst() { page = 1; },
  };
}
