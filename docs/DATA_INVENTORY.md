# Data inventory

What personal data this system holds, why, who can see it, and what happens to it.

## Officer accounts

| Data | Purpose | Stored in | Who can read it | Retention |
| --- | --- | --- | --- | --- |
| Name, email, designation | Attribute every decision to a named officer | `users` | Administrators; controllers within their subtree | For the life of the account, then as long as any record attributed to it |
| Role, jurisdiction | Authorisation | `users` | Same | Same |
| Password | Authentication | `users.password_hash` | **Nobody.** Argon2id hash only | Replaced on change |
| Sign-in attempts, IP, user agent | Lockout and investigation | `login_attempts` | Administrators | Operational; no automatic purge in this build |
| Session records, IP, user agent | Session management and revocation | `user_sessions` | The account holder for their own; administrators for any | Until expiry or revocation |
| MFA secret | Reserved | `users.mfa_secret` | Nobody | Unused in this build |

An officer's name and email appear in report snapshots, which are immutable. Removing an
account does not remove them from a report that was already issued: the report records who
decided, and that is the point of it.

## Complainants

| Data | Purpose | Stored in | Who can read it | Retention |
| --- | --- | --- | --- | --- |
| Name, email, phone | Follow up, and status lookup | `complaints` | Officers who can read the complaint | For the life of the complaint |
| Contact hash | Status lookup without exposing the contact in a URL | `complaints.contact_lookup_hash` | Nobody reads it directly; it is compared | Same |
| Description, product, seller, prices, purchase date | The substance of the complaint | `complaints` | Same | Same |
| Photographs, purchase proof | Evidence | `complaint_attachments`, object storage | Same | Same, or longer once promoted to inspection evidence |
| Submission IP, user agent | Abuse handling | `complaints` | Officers who can read the complaint | Same |
| Consent flag and time, privacy notice version | Demonstrating what was agreed to | `complaints` | Same | Same |

Contact details are **optional**. A complaint can be submitted without them; the status
page is then unavailable for it, and the form says so before submission.

Contact details are never returned by any public endpoint. Status lookup compares a hash
of the supplied value against the stored hash and returns only progress.

## Evidence

| Data | Purpose | Stored in | Notes |
| --- | --- | --- | --- |
| Package photographs | The evidence itself | Object storage, `evidence` | May incidentally contain a person, a shopfront or a bill |
| SHA-256, size, dimensions | Integrity | `evidence` | |
| Uploader, upload time, IP | Chain of custody | `evidence` | |
| Device make and model | Chain of custody | `evidence` | From EXIF when present |
| Capture coordinates | Locating an inspection | `evidence` | **Only when the officer explicitly permits it.** Never taken from EXIF GPS |
| EXIF summary | Chain of custody | `evidence.exif_summary` | A restricted subset; GPS is excluded |

GPS is deliberately not harvested from photograph metadata. Location is recorded from an
explicit action or not at all, so a photograph cannot silently place an officer somewhere.

Derivatives are stripped of EXIF. Originals keep theirs, because altering an original
would break the chain of custody.

## Records and decisions

| Data | Purpose | Retention |
| --- | --- | --- |
| Inspections, findings, decisions and reasons | The record | Governed by `retention_state`, `retain_until` and `legal_hold` |
| Report snapshots and documents | Immutable issued record | Not deleted while any case relies on them |
| Cases, notices, respondent contact details | Enforcement correspondence | For the life of the case |
| State transitions | Timeline | With the parent record |
| Audit events | Accountability | **Append-only.** Archived by sequence range, never deleted |

Respondent details on a case are business contact details of a regulated party, recorded
because a notice has to be addressed to somebody.

## Technical data

| Data | Purpose | Where | Notes |
| --- | --- | --- | --- |
| Request id | Correlating a response, a log line and an audit event | Logs, `audit_events.request_id` | Not personal by itself |
| Structured logs | Operations | stdout | Scrubbed: passwords, tokens, secrets and cookies are replaced with `[redacted]` |
| Metrics | Operations | `/metrics` | Aggregate counts and histograms only, no identifiers |
| Rate-limit counters | Abuse protection | Redis | Keyed by address or account, expire with the window |

## What is never stored

- Plaintext passwords, anywhere, at any point.
- Plaintext refresh tokens or password-reset tokens — hashes only.
- Card or payment data — the system has no payment function.
- Biometric data. No facial recognition, no biometric identification.
- GPS from photograph metadata.

## What is never logged

`app/observability.py` scrubs, and `app/services/audit.py` redacts, these keys wherever
they appear, including nested: `password`, `new_password`, `current_password`,
`password_hash`, `token`, `access_token`, `refresh_token`, `token_hash`, `secret`,
`jwt_secret`, `s3_secret_key`, `mfa_secret`, `authorization`, `cookie`, `set-cookie`,
`signature_value`.

## Rights, and how each is served

| Right | Mechanism | Limit |
| --- | --- | --- |
| Access | An officer can read a complaint in full; a complainant sees progress through the status page | The status page shows progress, not the inspection that followed |
| Correction | Officers correct records; a correction is recorded alongside the original, never over it | An issued report cannot be altered. A corrected inspection produces a new report and the old one is withdrawn |
| Erasure | `retention_state` and manual deletion | Constrained by `legal_hold` and by the audit trail, which is append-only. A record relied on by an issued report or an open case cannot be erased without destroying the record it supports |
| Export | `GET /audit/export.csv`; report PDF and DOCX | |

The tension between erasure and an append-only audit trail is real and is not resolved by
pretending otherwise. The audit trail records that an action occurred, by whom and when;
that is the basis on which any decision can later be defended. A deployment that must
support erasure of audit content needs a legal basis and a documented procedure, and this
build does not provide an endpoint for it.

## Sample data

`POST /api/v1/rules/seed` inserts eleven rule versions. It contains no personal data.

Every synthetic label generated by the verification suites is marked `(SAMPLE DATA)` in
the image itself, and uses `example.org` addresses and invented company names. No real
person, brand, premises or officer appears anywhere in this repository.

## Before deployment

- Publish a privacy notice matching the actual processing and set
  `PRIVACY_NOTICE_VERSION` accordingly; the version agreed to is stored per complaint.
- Decide and implement retention periods. **No automatic deletion job runs in this
  build** — nothing is deleted until an operator acts.
- Replace the placeholder contact details on the public pages with verified official
  contacts.
- Review the processing against current MeitY and Digital Personal Data Protection Act
  requirements with someone qualified to do so. This document describes what the software
  does; it is not a legal assessment.
