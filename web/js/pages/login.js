// Sign-in and one-time bootstrap. Honours ?next=.
import { auth, ApiError } from '../api.js';
import { $, el, setStatus, applyFieldErrors, clearFieldErrors } from '../util.js';

const signinForm = $('#signin-form');
const signinStatus = $('#signin-status');
const bootstrapSection = $('#bootstrap-section');
const bootstrapForm = $('#bootstrap-form');
const bootstrapStatus = $('#bootstrap-status');

function nextTarget() {
  const q = new URLSearchParams(location.search);
  const next = q.get('next');
  // Only allow same-origin relative paths.
  if (next && next.startsWith('/') && !next.startsWith('//')) return next;
  return '/app/overview.html';
}

async function init() {
  try {
    const status = await auth.status();
    if (status && status.initialised === false) {
      bootstrapSection.hidden = false;
      loadPolicy();
    }
  } catch { /* if status fails, just show sign-in */ }
}

async function loadPolicy() {
  try {
    const policy = await auth.passwordPolicy();
    const items = (policy.requirements || []).join(' ');
    $('#pw-policy').textContent = items || `Minimum ${policy.min_length} characters.`;
  } catch {
    $('#pw-policy').textContent = 'Choose a strong password.';
  }
}

signinForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  clearFieldErrors(signinForm);
  setStatus(signinStatus, '');
  const btn = $('#signin-btn');
  btn.disabled = true; btn.textContent = 'Signing in…';
  try {
    await auth.signIn(signinForm.elements['email'].value.trim(), signinForm.elements['password'].value);
    location.href = nextTarget();
  } catch (err) {
    if (err instanceof ApiError && err.status === 422) applyFieldErrors(signinForm, err);
    setStatus(signinStatus, err instanceof ApiError ? err.message : 'Sign-in failed. Please try again.', 'error');
  } finally {
    btn.disabled = false; btn.textContent = 'Sign in';
  }
});

bootstrapForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  clearFieldErrors(bootstrapForm);
  setStatus(bootstrapStatus, '');
  const btn = $('#bootstrap-btn');
  btn.disabled = true; btn.textContent = 'Creating…';
  const payload = {
    name: bootstrapForm.elements['name'].value.trim(),
    email: bootstrapForm.elements['email'].value.trim(),
    password: bootstrapForm.elements['password'].value,
    jurisdiction_code: bootstrapForm.elements['jurisdiction_code'].value.trim(),
    jurisdiction_name: bootstrapForm.elements['jurisdiction_name'].value.trim(),
  };
  const desig = bootstrapForm.elements['designation'].value.trim();
  if (desig) payload.designation = desig;
  try {
    await auth.bootstrap(payload);
    setStatus(bootstrapStatus, 'Administrator created. You can now sign in above.', 'ok');
    bootstrapForm.reset();
    bootstrapSection.hidden = true;
    $('#email').focus();
  } catch (err) {
    if (err instanceof ApiError && err.status === 422) applyFieldErrors(bootstrapForm, err);
    setStatus(bootstrapStatus, err instanceof ApiError ? err.message : 'Could not create the administrator.', 'error');
  } finally {
    btn.disabled = false; btn.textContent = 'Create administrator';
  }
});

init();
