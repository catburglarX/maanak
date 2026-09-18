// Photograph a package with the device camera, without leaving the inspection screen.
//
// Two routes, because one device cannot serve both cases:
//
//   * A phone or tablet opens a live viewfinder in the page through getUserMedia,
//     asking for the rear camera. The officer sees what the reader will see before
//     committing to it, which matters when the whole point is a legible panel.
//   * Where getUserMedia is unavailable, which means an insecure origin or an older
//     browser, a file input carrying capture="environment" hands off to the operating
//     system camera app instead. On a phone that is the native camera; on a desktop it
//     degrades to the ordinary file picker.
//
// getUserMedia needs a secure context. http://localhost counts as secure, so this works
// in local development, but a deployment served over plain HTTP silently loses the
// viewfinder and falls back. That is stated to the officer rather than left to be
// discovered.
//
// The capture is encoded from a canvas, so it carries no EXIF: no camera make, no model,
// no timestamp of its own. The image quality report notices exactly that and may mark
// the file as possibly not a photograph, which is the correct reading of the evidence.
// So the filename records how it was taken, the client clock is sent with the upload,
// and neither is inferred.

import { el } from '../util.js';
import { openDialog } from '../dialog.js';
export function cameraAvailable() {
  return !!(navigator.mediaDevices && typeof navigator.mediaDevices.getUserMedia === 'function');
}

export function secureEnough() {
  return window.isSecureContext === true;
}

// Why the live viewfinder is not on offer, in words an officer can act on.
export function cameraUnavailableReason() {
  if (!cameraAvailable()) return 'This browser does not offer camera access to a page.';
  if (!secureEnough()) {
    return 'A browser only grants camera access over HTTPS. This deployment is served '
      + 'over plain HTTP, so the camera opens in a separate app instead.';
  }
  return '';
}

export function captureFilename(now = new Date()) {
  const stamp = now.toISOString().replace(/[:.]/g, '-').slice(0, 19);
  return `camera-capture-${stamp}.jpg`;
}

/** Open the viewfinder. Resolves to a File, or null if the officer cancelled. */
export function openCamera({ face = '' } = {}) {
  return new Promise((resolve) => {
    let stream = null;
    let settled = false;

    const video = el('video', { id: 'cam-view', playsinline: true, muted: true, 'aria-label': 'Camera viewfinder' });
    video.muted = true;
    const canvas = el('canvas', { id: 'cam-canvas', hidden: true });
    const preview = el('img', { id: 'cam-preview', alt: 'The photograph just taken', hidden: true });
    const message = el('p', { class: 'hint', id: 'cam-hint', text: 'Fill the frame with the printed panel, then take the photograph.' });
    const stage = el('div', { class: 'camera-stage' }, [video, preview, canvas]);
    const body = el('div', {}, [
      face ? el('p', { class: 'small muted', text: `Capturing: ${face}` }) : null,
      stage,
      message,
    ]);

    const stop = () => {
      if (stream) stream.getTracks().forEach((track) => track.stop());
      stream = null;
    };
    const finish = (value) => {
      if (settled) return;
      settled = true;
      stop();
      resolve(value);
    };

    const dlg = openDialog({
      title: 'Take a photograph',
      body,
      actions: [
        { label: 'Cancel', value: null },
        { label: 'Retake', class: '', close: false, onClick: () => retake() },
        { label: 'Take photograph', class: 'btn-primary', close: false, onClick: () => shoot() },
      ],
    });
    dlg.onClose(() => finish(null));

    const buttons = dlg.element.querySelectorAll('.dlg-foot button');
    const retakeBtn = buttons[1];
    const shootBtn = buttons[2];
    retakeBtn.hidden = true;

    function retake() {
      preview.hidden = true;
      video.hidden = false;
      retakeBtn.hidden = true;
      shootBtn.textContent = 'Take photograph';
      message.textContent = 'Fill the frame with the printed panel, then take the photograph.';
      shootBtn.onclick = null;
      return false;
    }

    function shoot() {
      if (!video.videoWidth) {
        message.textContent = 'The camera is still starting. Try again in a moment.';
        return false;
      }
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
      canvas.toBlob((blob) => {
        if (!blob) {
          message.textContent = 'The photograph could not be encoded. Try again.';
          return;
        }
        preview.src = URL.createObjectURL(blob);
        preview.hidden = false;
        video.hidden = true;
        retakeBtn.hidden = false;
        message.textContent = `Captured ${canvas.width} by ${canvas.height} pixels. Use it, or retake.`;
        shootBtn.textContent = 'Use this photograph';
        shootBtn.onclick = () => {
          const file = new File([blob], captureFilename(), { type: 'image/jpeg' });
          dlg.close();
          finish(file);
        };
      }, 'image/jpeg', 0.95);
      return false;
    }

    navigator.mediaDevices
      .getUserMedia({
        video: {
          // The rear camera on a phone. Ignored by a laptop with one camera.
          facingMode: { ideal: 'environment' },
          width: { ideal: 2560 },
          height: { ideal: 1440 },
        },
        audio: false,
      })
      .then((granted) => {
        if (settled) {
          granted.getTracks().forEach((track) => track.stop());
          return;
        }
        stream = granted;
        video.srcObject = granted;
        return video.play();
      })
      .catch((err) => {
        const denied = err && (err.name === 'NotAllowedError' || err.name === 'SecurityError');
        message.textContent = denied
          ? 'Camera access was refused. Allow it in the browser address bar, or choose an image file instead.'
          : 'No camera could be opened. Choose an image file instead.';
        shootBtn.disabled = true;
      });
  });
}

/**
 * Hand off to the operating system camera app through a file input.
 * Used when the in-page viewfinder is not available.
 */
export function openSystemCamera() {
  return new Promise((resolve) => {
    const input = el('input', { type: 'file', accept: 'image/*', capture: 'environment', class: 'visually-hidden' });
    input.addEventListener('change', () => {
      const file = input.files && input.files[0] ? input.files[0] : null;
      input.remove();
      resolve(file);
    });
    // Cancelling a file picker fires no event, so the element is left for the garbage
    // collector rather than waited on.
    document.body.appendChild(input);
    input.click();
  });
}

/** The route this device can actually use. */
export async function takePhotograph(options = {}) {
  if (cameraAvailable() && secureEnough()) return openCamera(options);
  return openSystemCamera();
}
