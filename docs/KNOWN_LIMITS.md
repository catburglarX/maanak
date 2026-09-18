# Known limits

What this system does not do, and what has not been proven. Read this before drawing
conclusions from anything else in the repository.

## Not implemented

| Feature | Status | Why it is absent rather than half-built |
| --- | --- | --- |
| **OCR accuracy benchmark** | Not measured | A benchmark needs a labelled corpus of real package photographs: multiple languages, package types, lighting conditions and camera qualities. No such corpus was available. The pipeline is verified to work end to end on generated labels, which proves the mechanism, not field accuracy. No accuracy figure is claimed anywhere. |
| **E-commerce listing fetcher** | Not implemented | An outbound fetcher needs DNS pinning, redirect refusal, private-address blocking, byte limits, decompression limits and network isolation to be safe. Configuration for it exists in `app/config.py`, but no code fetches a URL, so there is nothing to exploit and nothing to claim. |
| **Offline field capture with sync** | Partial | The interface detects online and offline state and tells the officer. Local draft storage, an upload queue, conflict resolution and device-loss handling are not implemented. |
| **In-page camera** | Works, with one condition | A browser only grants a page access to the camera in a secure context. That covers `http://localhost` and any HTTPS deployment, but not a deployment served over plain HTTP, where the button hands off to the operating system camera app instead and says so. A capture encoded from the page carries no EXIF, so the quality report may mark it as possibly not a photograph, which is a correct reading of an image with no camera metadata. The filename records that it came from the camera. |
| **Food-label tool** | Not implemented | Ingredient, nutrition and allergen extraction is absent. The original prototype had an undocumented "health score", which was removed rather than kept: a score with no reviewed method behind it is worse than nothing. |
| **Digital signature** | Adapter only | The signer returns `none` or a clearly labelled `development` HMAC. There is no certificate and no signing service. Every place the value appears says so. |
| **MFA / SSO** | Architecture only | The `User` model carries `mfa_enabled`, `mfa_secret` and `external_subject`, and the session layer would accommodate either. No second factor is enforced and no OIDC flow exists. |
| **Password reset by email** | Partial | A single-use hashed token is issued and can be redeemed. There is no email delivery: an administrator issues the token and passes it on. `notification_backend` defaults to `console`. |
| **Malware scanning on upload** | Interface only | `Evidence.malware_scan_state` defaults to `not_scanned`. Files are validated as images from their own bytes, which is not the same as scanning them. |
| **Performance and load testing** | Not done | No response-time budget has been measured under load, no concurrency test beyond correctness, no storage-growth projection. |
| **Backup and restore drill** | Script only | `docs/BACKUP_RESTORE.md` documents the procedure and the integrity re-verification, but a full restore into a clean environment was not executed as part of this build. |

## Verified but narrowly

| Claim | What was actually tested | What was not |
| --- | --- | --- |
| OCR reads English and Hindi | Generated label images rendered with DejaVu and Noto Devanagari, read correctly through the full pipeline | Real photographs of real packaging, curved surfaces, reflective film, worn print |
| Image quality signals are calibrated | Thresholds documented with reasoning; verified against generated images covering each failure mode. Two corrections came from real inputs rather than from the generated set: the glare detector once flagged white packaging, and a plain studio backdrop was counted twice, as glare and again as bright clipping, so a catalogue photograph of a panel was refused for a background rather than for the light on it. Both are covered by `tests/test_imaging_quality.py`, in both directions | Calibration against a labelled set of real photographs with human quality judgements. The backdrop test is geometric: a clipped flat region reaching three or more edges of the frame is a background. A photograph where a genuine highlight reaches three edges would be discounted, though such an image is almost entirely blown out and the brightness and clipping signals report it anyway |
| Accessibility targets WCAG 2.2 AA | axe-core (wcag2a, wcag2aa, wcag22aa) reports zero serious or critical violations on eight pages; one `h1` per page, skip link as first tab stop, all form fields labelled, no horizontal overflow at 320px or 390px | Manual screen-reader testing with NVDA, JAWS or VoiceOver; testing with real assistive-technology users; the remaining eighteen pages were not scanned individually |
| Reports render Hindi | The PDF registers Noto Devanagari and selects it automatically for Devanagari text; if the font is missing the document says so instead of printing empty boxes | A report containing substantial Hindi content has not been visually proof-read |
| Jurisdiction isolation | Cross-jurisdiction read returns 404, the register excludes out-of-scope records, and the controller hierarchy works, all against a live API | A systematic sweep of every one of the 97 routes for object-level authorisation |
| Audit chain is tamper-evident | The database refuses UPDATE and DELETE; a forged INSERT with a wrong `previous_hash` is detected and the offending sequence named | Behaviour under an attacker with direct database superuser access, who could drop the trigger |

## Deliberate design limits

These are choices, not gaps.

**Absent evidence is never a violation.** If a declaration cannot be read the outcome is
`unable_to_determine` or `additional_evidence_required`. This means Maanak will
under-report rather than over-report, which is the correct bias for a system whose output
could support enforcement.

**Character height needs a physical measurement.** The system refuses to convert pixels
to millimetres. This makes the check less automatic and more defensible.

**Net quantity is never verified.** Label analysis cannot establish package contents.
Every report states this in its limits section.

**A barcode is not proof of authenticity.** A successful read proves an identifier is
printed on a package. `officer_confirmed` is recorded separately, and nothing infers
country of origin from a GS1 prefix.

**Products are not jurisdiction-scoped.** The product catalogue is a shared national
reference; inspections are scoped. Duplicating a package variant per district would
defeat the purpose of a shared repository.

**Rate limiting fails closed on sensitive endpoints.** If Redis is unavailable, sign-in,
password reset and public complaint submission are refused rather than allowed
unprotected. This trades availability for safety on exactly the endpoints where that is
the right trade.

## Operational limits of this build

- `MAANAK_ENV=development` by default. Cookies are not `Secure`, API documentation is
  exposed, and `TRUSTED_HOSTS` is `*`. `MAANAK_ENV=production` refuses to start until
  those are corrected, but it cannot check the non-technical items.
- Secrets live in a `.env` file. A real deployment needs a managed secret store.
- MinIO uses its built-in KMS with a local key. Production needs a real KMS or the
  storage service's own encryption at rest.
- The application connects to PostgreSQL as the owning role. The append-only audit
  guarantee currently rests on triggers rather than on a role that lacks `UPDATE` and
  `DELETE` grants. Adding a restricted role is straightforward and is the stronger
  control.
- Contact details on the public pages are placeholders and are labelled as such. They
  must be replaced with verified official contacts.

## What would change these limits

In rough order of value:

1. A labelled corpus of real package photographs, which would turn the OCR and quality
   claims from "the mechanism works" into measured figures with error bars.
2. Confirmation of every rule citation and threshold by a qualified authority, which is
   what moves `legal_authority_confirmed` from false to true.
3. Manual screen-reader testing, which is the half of accessibility that automated tools
   cannot cover.
4. A restricted database role for the application, removing the trigger as the only
   thing standing between a compromised application and the audit trail.
5. Load testing with realistic image sizes and concurrency, to set a real performance
   budget rather than an assumed one.
