# Threat model

## What is worth attacking

| Asset | Why it matters |
| --- | --- |
| Evidence originals and their hashes | A conclusion that cannot be traced to unaltered evidence is worthless |
| The audit trail | It is the record of who decided what |
| Report snapshots | An issued report may be relied on outside the system |
| Officer sessions | A stolen session can create findings attributed to a named officer |
| Rule versions | Changing an approved interpretation changes every future finding |
| Complainant contact details | Personal data supplied by members of the public |

## Who might attack it

| Actor | Capability | Motive |
| --- | --- | --- |
| Unauthenticated internet user | Public endpoints only | Enumerate references, submit abusive complaints, exhaust resources |
| Regulated party | May know a report reference or a complaint reference | Discover whether they are under inspection; alter or discredit a finding |
| Authorised officer acting outside their remit | A valid session | Read or act on records in another jurisdiction; approve their own rule interpretation |
| Compromised officer account | A valid session | Anything that role permits |
| Attacker with application-level database access | SQL as the application role | Rewrite history to remove a finding |
| Attacker with database superuser access | Full control | Everything |

## Controls, and what each actually stops

### Authentication

| Threat | Control |
| --- | --- |
| Credential stuffing | Argon2id with configurable cost; per-account lockout after 5 failures; per-IP and per-account rate limits that **fail closed** if Redis is unavailable |
| Account enumeration | Identical error code and message for unknown account and wrong password; `dummy_verify()` spends comparable CPU on the unknown-account path so timing does not distinguish them (verified: ratio within 3×) |
| Token theft via script injection | The access token is in an `HttpOnly` cookie. A bearer header is deliberately not accepted, so there is no token for injected script to read |
| Cross-site request forgery | Double-submit: a readable `maanak_csrf` cookie must match an `X-CSRF-Token` header on every unsafe method, plus `SameSite=Lax` |
| Refresh token theft | Refresh tokens are opaque random values stored only as SHA-256 hashes. Every use rotates them. Presenting a rotated token revokes the **whole family** |
| Stale privileges | The session row is re-read on every request, so revocation takes effect immediately. A role or jurisdiction change invalidates the token's claims and forces a refresh |
| Signing-key compromise | `JWT_SECRET_PREVIOUS` allows rotation without ending sessions |

Residual: no second factor is enforced. A stolen password plus a stolen device is enough.
MFA fields exist on the model; nothing uses them.

### Authorisation

| Threat | Control |
| --- | --- |
| Broken object-level authorisation | Jurisdiction scope is applied **inside the SQL query**, not after loading. Out-of-scope records return **404, not 403**, so a guessed UUID cannot confirm existence |
| Privilege escalation through the request body | Every request schema sets `extra="forbid"`; an unexpected field is a 422, not a silent write |
| Self-escalation | No account can change its own role or deactivate itself |
| Prefix confusion in jurisdiction codes | Descendant matching requires a whole segment: `IN-HR` does not cover `IN-HRX` (tested) |
| Rubber-stamping a rule interpretation | The author of a rule version cannot approve it, and approval is refused until a simulator run covering every mandatory case passes |

Residual: jurisdiction isolation is proven on inspections, complaints, cases, reports and
the audit trail. All 97 routes were not independently swept.

### Uploads

| Threat | Control |
| --- | --- |
| Malicious file disguised as an image | The type is read from the file's own bytes; a mismatch with the declared type is a 415 before any decoder runs |
| Decompression bomb | An explicit `Image.MAX_IMAGE_PIXELS`, a configured pixel ceiling, and a size limit enforced while reading rather than after |
| Storage exhaustion | Per-user upload rate limit; `client_max_body_size` at the proxy; duplicate uploads refused by hash |
| Path traversal in object keys | Keys are built from server-controlled parts only and validated against a pattern that refuses `..` |
| Malware in a stored file | **Not addressed.** `malware_scan_state` is `not_scanned`. Validating that a file is an image is not scanning it |

### Injection and output

| Threat | Control |
| --- | --- |
| SQL injection | Every query goes through SQLAlchemy with bound parameters. The few raw statements use bound parameters and fixed identifiers |
| Stored cross-site scripting | The API returns JSON with a `Content-Security-Policy` of `default-src 'none'`. The browser application builds DOM nodes and sets `textContent`; it does not assemble HTML from stored values |
| Reflected content | Validation errors report the **field and the rule**, never the submitted value |
| Internal disclosure | Driver text, table names and stack traces are never returned. `IntegrityError` is mapped to a safe message by constraint kind |
| Clickjacking | `X-Frame-Options: DENY` and `frame-ancestors 'none'` |
| Server-side request forgery | No code fetches a client-supplied URL in this build |

### Data integrity

| Threat | Control |
| --- | --- |
| Silent evidence substitution | A database trigger refuses any change to an evidence row's hash, size, storage location or inspection. Originals are versioned |
| Altering an issued report | A trigger refuses any change to a snapshot, its hash, its issue time, its reference or its verification code. Documents are re-hashed before being served and refused on mismatch |
| Changing a served notice | A trigger refuses any change to a frozen notice's text, hash, template version or issue time |
| Rewriting history | `audit_events` refuses `UPDATE` and `DELETE`. Each event carries the previous event's hash, so an insertion or removal breaks the chain and `/audit/verify` names the sequence |
| Concurrent overwrite | Optimistic locking on inspections, complaints, cases, candidates, findings, products and rule versions. A stale write is a 409 |
| Duplicate references under load | References come from PostgreSQL sequences, not `SELECT max()+1` |
| Forked audit chain under load | Appends take a transaction-level advisory lock |

Residual: the application connects as the owning role, so the append-only guarantee rests
on triggers rather than on absent `UPDATE` and `DELETE` grants. An attacker who can drop
triggers can rewrite history. A restricted role is the stronger control and is listed in
`docs/KNOWN_LIMITS.md`.

### Public surface

| Threat | Control |
| --- | --- |
| Enumerating report references | Verification reveals only issue metadata and the hash. An unknown reference and a wrong code give the **same** answer |
| Enumerating complaints | Status lookup requires the reference **and** the contact detail supplied at submission. A wrong contact gives the same answer as an unknown reference |
| Complaint spam | Per-IP rate limit that fails closed; a mandatory privacy acknowledgement; attachment count and size limits |
| Harvesting complainant contacts | Contact details are never returned by any public endpoint. Status lookup matches against a SHA-256 of the contact rather than echoing it |

Residual: no CAPTCHA or proof-of-work. A determined actor with many addresses can still
submit noise, which is then an operational triage problem rather than a security one.

### Transport and configuration

| Threat | Control |
| --- | --- |
| Session cookies over plaintext | `COOKIE_SECURE`, and `MAANAK_ENV=production` refuses to start without it |
| Host header attacks | `TrustedHostMiddleware` when `TRUSTED_HOSTS` is explicit; production refuses `*` |
| Address spoofing in the audit trail | The API trusts forwarded headers, so `--forwarded-allow-ips` must name the proxy in production. **Currently `*`** |
| Secrets in the repository | `.env` is git-ignored and excluded from the release archive, which also scans for private-key and AWS-key patterns before writing |
| Accidental production start with unsafe settings | `Settings.assert_production_ready()` fails startup and names every problem at once |

## Not defended against

Stated plainly rather than implied:

- **Database superuser access.** Triggers can be dropped and rows rewritten. Detection
  depends on comparing against an off-host backup whose chain head was recorded.
- **Malware in an uploaded file.** Files are validated as images, not scanned.
- **A compromised officer account.** Every action it takes is attributed to that officer
  and recorded, which supports investigation but does not prevent the action.
- **Denial of service.** Rate limits protect specific endpoints. There is no protection
  against volumetric attack; that belongs at the edge.
- **Insider misuse within scope.** An officer acting inside their jurisdiction and role
  can record whatever they choose. The audit trail makes it attributable and permanent,
  which is the control that actually applies.

## Verification

Security controls are asserted, not assumed:

| Suite | Covers |
| --- | --- |
| `verify_security.py` (51) | Hashing, timing equalisation, tokens, permissions, jurisdiction scope |
| `verify_auth_flow.py` (53) | Cookie attributes, CSRF, refresh rotation, reuse detection, revocation, mass assignment, security headers, error shape |
| `verify_pipeline.py` (56) | Upload validation, type mismatch rejection, duplicate rejection, hash integrity |
| `verify_workflow.py` (117) | Cross-jurisdiction denial, decision gating, report immutability, public verification non-disclosure |
| `verify_matters.py` (86) | Complaint non-disclosure, audit chain verification, database refusal of audit mutation, forged-event detection |
| `verify_schema.py` (14) | Database-level protections exist after migration |

CI additionally runs `pip-audit --strict`, a gitleaks secret scan and a Trivy container
scan that fails on HIGH or CRITICAL.
