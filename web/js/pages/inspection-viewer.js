// Evidence image viewer: zoom, pan (drag), rotate, and a scaled region overlay
// drawn in original-pixel coordinates supplied on the candidate.
import { $ } from '../util.js';

export function createViewer() {
  const img = $('#evidence-img');
  const wrap = $('#canvas-wrap');
  const box = $('#region-box');
  const stage = $('#viewer-stage');
  const empty = $('#viewer-empty');
  const zoomLabel = $('#zoom-level');

  let scale = 1;
  let rotation = 0;
  let current = null; // { view_url, original_width, original_height }
  let pendingRegion = null;

  function applyTransform() {
    wrap.style.transform = `rotate(${rotation}deg) scale(${scale})`;
    zoomLabel.textContent = `${Math.round(scale * 100)}%`;
  }

  function positionRegion() {
    if (!pendingRegion || !current || !img.naturalWidth) { box.hidden = true; return; }
    const [x1, y1, x2, y2] = pendingRegion;
    // The <img> is displayed at its natural size inside wrap; scale/rotation are
    // applied to the wrapper, so region coordinates map 1:1 to the natural image.
    const rw = img.naturalWidth;
    const rh = img.naturalHeight;
    // Guard against out-of-range coordinates.
    const left = Math.max(0, Math.min(x1, rw));
    const top = Math.max(0, Math.min(y1, rh));
    const w = Math.max(2, Math.min(x2, rw) - left);
    const h = Math.max(2, Math.min(y2, rh) - top);
    box.style.left = `${left}px`;
    box.style.top = `${top}px`;
    box.style.width = `${w}px`;
    box.style.height = `${h}px`;
    box.hidden = false;
  }

  img.addEventListener('load', () => { positionRegion(); });

  function show(evidence) {
    current = evidence;
    pendingRegion = null;
    box.hidden = true;
    if (evidence && evidence.view_url) {
      empty.hidden = true;
      img.hidden = false;
      img.src = evidence.view_url;
      img.alt = `Evidence: ${evidence.face ? evidence.face.replace(/_/g, ' ') : 'package face'}`;
      scale = 1; rotation = 0;
      applyTransform();
    } else {
      img.hidden = true;
      empty.hidden = false;
      empty.textContent = evidence ? 'This evidence image cannot be displayed (no viewable link).' : 'No evidence selected.';
    }
  }

  function highlight(region) {
    pendingRegion = Array.isArray(region) && region.length === 4 ? region : null;
    positionRegion();
    if (pendingRegion) {
      // Scroll region into view within the stage.
      const [x1, y1] = pendingRegion;
      stage.scrollTo({ left: Math.max(0, x1 * scale - 60), top: Math.max(0, y1 * scale - 60), behavior: 'smooth' });
    }
  }

  // Controls
  $('#zoom-in').addEventListener('click', () => { scale = Math.min(6, scale + 0.25); applyTransform(); });
  $('#zoom-out').addEventListener('click', () => { scale = Math.max(0.25, scale - 0.25); applyTransform(); });
  $('#rotate').addEventListener('click', () => { rotation = (rotation + 90) % 360; applyTransform(); });
  $('#reset-view').addEventListener('click', () => { scale = 1; rotation = 0; applyTransform(); stage.scrollTo(0, 0); });

  // Drag to pan
  let dragging = false; let sx = 0; let sy = 0; let sl = 0; let st = 0;
  stage.addEventListener('pointerdown', (e) => {
    if (img.hidden) return;
    dragging = true; sx = e.clientX; sy = e.clientY; sl = stage.scrollLeft; st = stage.scrollTop;
    stage.setPointerCapture(e.pointerId);
  });
  stage.addEventListener('pointermove', (e) => {
    if (!dragging) return;
    stage.scrollLeft = sl - (e.clientX - sx);
    stage.scrollTop = st - (e.clientY - sy);
  });
  const stop = () => { dragging = false; };
  stage.addEventListener('pointerup', stop);
  stage.addEventListener('pointercancel', stop);

  return { show, highlight };
}
