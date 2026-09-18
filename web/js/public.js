// Injects the shared public header and footer, and initialises chrome.
import { el, $ } from './util.js';
import { initChrome } from './chrome.js';

// The two section links point at the homepage anchors, written in absolute form so
// they work from every public page rather than only from the homepage.
const PUBLIC_LINKS = [
  ['/index.html#platform', 'Platform'],
  ['/index.html#method', 'Method'],
  ['/complain.html', 'Report a product'],
];

const UTILITY_LINKS = [
  ['/legal-sources.html', 'Legal sources'],
  ['/contact.html', 'Contact'],
];

// Grouped so that every public route stays reachable from the footer once the
// header carries only the three primary sections.
const FOOTER_GROUPS = [
  ['Platform', [
    ['/login.html', 'Officer sign in'],
    ['/complain.html', 'Report a product'],
    ['/complaint-status.html', 'Check your report'],
    ['/verify.html', 'Verify a report'],
  ]],
  ['Governance', [
    ['/legal-sources.html', 'Legal sources'],
    ['/responsible-use.html', 'Responsible use'],
    ['/security.html', 'Security'],
  ]],
  ['Legal', [
    ['/privacy.html', 'Privacy'],
    ['/accessibility.html', 'Accessibility'],
    ['/terms.html', 'Terms'],
  ]],
];

function currentPage() {
  return location.pathname.split('/').pop() || 'index.html';
}

function buildUtility() {
  const links = el('span', { class: 'util-links' });
  for (const [href, label] of UTILITY_LINKS) {
    links.appendChild(el('a', { href, text: label }));
  }
  return el('div', { class: 'utility' }, [
    el('div', { class: 'container' }, [
      el('span', { text: 'Packaged commodity inspection and review' }),
      links,
    ]),
  ]);
}

function buildHeader() {
  const current = currentPage();
  // The logo artwork already sets the MAANAK wordmark, so no text name is repeated
  // next to it.
  const brand = el('a', { class: 'brand', href: '/index.html' }, [
    el('img', { src: '/assets/maanak-logo.svg', alt: 'Maanak — home' }),
  ]);
  const nav = el('nav', { class: 'site-nav', 'aria-label': 'Primary' });
  for (const [href, label] of PUBLIC_LINKS) {
    const a = el('a', { href, text: label });
    if (!href.includes('#') && href.endsWith(current)) a.setAttribute('aria-current', 'page');
    nav.appendChild(a);
  }
  const signIn = el('a', { class: 'nav-cta', href: '/login.html', text: 'Officer sign in' });
  if (current === 'login.html') signIn.setAttribute('aria-current', 'page');
  nav.appendChild(signIn);
  return el('header', { class: 'site-header' }, [
    el('div', { class: 'bar' }, [brand, nav]),
  ]);
}

function buildFooterGroup(heading, entries) {
  const list = el('ul', {});
  for (const [href, label] of entries) {
    list.appendChild(el('li', {}, [el('a', { href, text: label })]));
  }
  return el('div', {}, [el('h2', { text: heading }), list]);
}

function buildFooter() {
  const cols = el('div', { class: 'cols' }, [
    el('div', { class: 'foot-brand' }, [
      el('img', { src: '/assets/maanak-logo.svg', alt: 'Maanak' }),
      el('p', {
        text: 'Maanak supports inspection and review. It does not create an enforcement '
          + 'decision without authorised human confirmation.',
      }),
    ]),
  ]);
  for (const [heading, entries] of FOOTER_GROUPS) {
    cols.appendChild(buildFooterGroup(heading, entries));
  }
  return el('footer', { class: 'site-footer' }, [
    cols,
    el('div', { class: 'footer-note' }, [
      el('p', {}, [
        el('strong', { text: 'Maanak is not a government service.' }),
        ' It makes no claim of government affiliation, endorsement, statutory certification or completed production accreditation. Seeded rule citations are not yet verified against the gazette and must be confirmed by a qualified authority before any enforcement use.',
      ]),
    ]),
  ]);
}

export function mountPublicChrome() {
  const headerMount = $('#site-header');
  if (headerMount) headerMount.replaceWith(buildUtility(), buildHeader());
  const footerMount = $('#site-footer');
  if (footerMount) footerMount.replaceWith(buildFooter());
  initChrome();
}

// Auto-mount if the page opts in with data-public-chrome on <body>.
if (document.body && document.body.hasAttribute('data-public-chrome')) {
  mountPublicChrome();
}
