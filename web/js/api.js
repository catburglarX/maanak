// Maanak API client — cookie-based auth, CSRF header on unsafe methods,
// single refresh-and-retry on 401, structured error surface.

const BASE = '/api/v1';

export class ApiError extends Error {
  constructor(status, body) {
    const err = (body && body.error) || {};
    super(err.message || `Request failed (${status})`);
    this.name = 'ApiError';
    this.status = status;
    this.code = err.code || null;
    this.details = err.details || null;
    this.requestId = err.request_id || null;
  }
}

function readCookie(name) {
  const parts = document.cookie ? document.cookie.split('; ') : [];
  for (const p of parts) {
    const idx = p.indexOf('=');
    const k = idx === -1 ? p : p.slice(0, idx);
    if (k === name) return decodeURIComponent(p.slice(idx + 1));
  }
  return null;
}

export function csrfToken() {
  return readCookie('maanak_csrf');
}

const UNSAFE = new Set(['POST', 'PATCH', 'PUT', 'DELETE']);

async function parseBody(res) {
  const ct = res.headers.get('content-type') || '';
  if (ct.includes('application/json')) {
    try { return await res.json(); } catch { return null; }
  }
  return null;
}

async function doFetch(method, path, { body, headers, isForm } = {}) {
  const opts = {
    method,
    credentials: 'same-origin',
    headers: { ...(headers || {}) },
  };
  if (UNSAFE.has(method)) {
    const token = csrfToken();
    if (token) opts.headers['X-CSRF-Token'] = token;
  }
  if (body !== undefined) {
    if (isForm) {
      opts.body = body; // FormData — let the browser set the boundary.
    } else {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
  }
  const url = path.startsWith('/api') ? path : BASE + path;
  return fetch(url, opts);
}

let refreshInFlight = null;

async function refreshOnce() {
  if (!refreshInFlight) {
    refreshInFlight = doFetch('POST', '/auth/refresh')
      .then((r) => r.ok)
      .catch(() => false)
      .finally(() => { refreshInFlight = null; });
  }
  return refreshInFlight;
}

function redirectToLogin() {
  const next = encodeURIComponent(location.pathname + location.search);
  location.href = `/login.html?next=${next}`;
}

async function request(method, path, opts = {}) {
  let res = await doFetch(method, path, opts);
  if (res.status === 401) {
    const body = await parseBody(res);
    const code = body && body.error && body.error.code;
    if (code === 'session_expired' || code === 'not_authenticated') {
      const ok = await refreshOnce();
      if (ok) {
        res = await doFetch(method, path, opts);
      } else {
        if (!opts.noRedirect) redirectToLogin();
        throw new ApiError(401, body);
      }
    } else {
      throw new ApiError(401, body);
    }
  }
  if (!res.ok) {
    const body = await parseBody(res);
    throw new ApiError(res.status, body);
  }
  if (res.status === 204) return null;
  return parseBody(res);
}

function withQuery(path, params) {
  if (!params) return path;
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === '') continue;
    q.append(k, v);
  }
  const s = q.toString();
  return s ? `${path}?${s}` : path;
}

export const api = {
  get: (path, params) => request('GET', withQuery(path, params)),
  post: (path, body) => request('POST', path, { body }),
  patch: (path, body) => request('PATCH', path, { body }),
  del: (path) => request('DELETE', path),
  postForm: (path, formData) => request('POST', path, { body: formData, isForm: true }),
  // Raw fetch for downloads / blob URLs (still credentialed).
  raw: (path, opts) => doFetch(opts && opts.method ? opts.method : 'GET', path, opts || {}),
};

// Convenience auth helpers.
export const auth = {
  status: () => api.get('/auth/status'),
  me: () => api.get('/auth/me', undefined),
  signIn: (email, password) => api.post('/auth/sign-in', { email, password }),
  signOut: () => api.post('/auth/sign-out'),
  bootstrap: (payload) => api.post('/auth/bootstrap', payload),
  passwordPolicy: () => api.get('/auth/password-policy'),
};

// Readiness sits outside the versioned API surface, at /health/ready, so that it can
// answer even when the versioned routes cannot. Routing it through api.get() would
// prefix it with /api/v1 and return 404, which is what used to happen here.
//
// A 503 is a real answer, not a failure: the body names which dependency is down, and
// that is exactly what the officer needs to see.
export async function readiness() {
  const res = await fetch('/health/ready', { credentials: 'same-origin' });
  if (res.status !== 200 && res.status !== 503) return null;
  const type = res.headers.get('content-type') || '';
  if (!type.includes('application/json')) return null;
  try {
    return await res.json();
  } catch {
    return null;
  }
}
