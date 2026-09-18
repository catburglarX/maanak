# Architecture

## Shape

A modular monolith with one background worker. Not microservices: the whole system is
one bounded domain with one transactional boundary, and splitting it would add network
failure modes and distributed-transaction problems without solving anything Maanak
actually has.

```
                    ┌────────────────────────────────────┐
  browser  ────────▶│ nginx (web)                        │
                    │  static files, /api proxy, CSP     │
                    └───────────────┬────────────────────┘
                                    │
                    ┌───────────────▼────────────────────┐
                    │ FastAPI (api)                      │
                    │  routes → services → models        │
                    │  cookie sessions, CSRF, scope      │
                    └──┬─────────┬──────────┬────────────┘
                       │         │          │
        ┌──────────────▼─┐  ┌────▼─────┐ ┌──▼──────────────┐
        │ PostgreSQL 16  │  │ Redis 7  │ │ MinIO (S3 API)  │
        │ records, audit │  │ queue,   │ │ originals,      │
        │ chain, locks   │  │ limits   │ │ derivatives,    │
        └──────────────┬─┘  └────┬─────┘ │ reports         │
                       │         │       └──▲──────────────┘
                    ┌──▼─────────▼──────────┴────────┐
                    │ arq worker                     │
                    │  imaging → OCR → extraction    │
                    │  Tesseract eng+hin, zxing-cpp  │
                    └────────────────────────────────┘
```

## Why each piece

**PostgreSQL 16** is the only relational store, as required. It is used for more than
row storage: `CHECK` constraints derived from the domain enums, transaction-level
advisory locks for the audit chain and the one-time bootstrap, sequences for
concurrency-safe references, triggers enforcing append-only audit and immutable report
snapshots, and optimistic locking through SQLAlchemy version columns. Putting these in
the database means a bug in the application cannot bypass them.

**FastAPI** gives typed request and response schemas that generate the OpenAPI document
directly from the code, so the API documentation cannot drift from the implementation.
`extra="forbid"` on every request model is what prevents mass assignment.

**A separate arq worker** runs OCR. A full-resolution package photograph takes seconds
to read; doing that in a request handler would hold a connection open and make the
interface feel broken. The queue record lives in PostgreSQL and is the source of truth;
Redis only carries the wake-up message, so a Redis restart loses scheduling but never
loses the fact that work is outstanding.

**Redis** carries the job queue and the rate-limit counters. Both are ephemeral by
nature. Rate limiting fails *closed* on sign-in, password reset and public complaint
submission: refusing a request is better than silently dropping brute-force protection.

**MinIO** provides the S3 API locally so the storage code path is identical to a
production deployment against any S3-compatible service. Three buckets separate data by
lifecycle: `originals` (never modified, versioning enabled), `derivatives`
(regenerable), `reports` (issued documents). Server-side encryption is requested on
every object; MinIO's built-in KMS is configured so that request succeeds locally rather
than being quietly disabled.

**Tesseract** for OCR. It runs offline with no per-page cost and no data leaving the
deployment, which matters for evidence. Language data for English and Hindi is installed
in the image. See `docs/KNOWN_LIMITS.md` for what has and has not been measured.

**nginx** serves the browser application and proxies `/api`. Keeping the frontend on
the same origin as the API is what allows `SameSite` cookies and a strict
`Content-Security-Policy` of `default-src 'self'`.

**No frontend build step.** Plain HTML, CSS and ES modules. The deployment target is a
government-adjacent environment where a reproducible build without a Node toolchain is
worth more than the conveniences a framework would bring.

## Request path

```
nginx → TrustedHost → CORS → request-context middleware → route
                                                            │
                                            get_principal ──┤ cookie → JWT claims
                                                            │ session row re-read
                                                            │ CSRF double-submit
                                                            │ role permission
                                                            ▼
                                                     service layer
                                                            │ jurisdiction scope
                                                            │ applied inside SQL
                                                            ▼
                                                     PostgreSQL
```

Every mutating request also appends one hash-chained audit event inside the same
transaction, so a change and its record either both commit or neither does.

## Layering

| Layer | Directory | Rule |
| --- | --- | --- |
| Routes | `app/api/v1/` | Parse, authorise, call one service, serialise. No business logic. |
| Schemas | `app/schemas/` | Request and response shapes. `extra="forbid"` on requests. |
| Services | `app/services/` | All behaviour. Take a session, never a request. |
| Models | `app/models/` | Tables and constraints only. |
| Domain | `app/domain/` | Enumerations and state machines. The single source of truth. |
| Security | `app/security/` | Passwords, tokens, sessions, limits, permissions. |
| Worker | `app/worker/` | Background tasks. Imports services, never routes. |

The domain enumerations generate the database `CHECK` constraints, the Pydantic
validation and the vocabulary the frontend reads from `/reference/vocabulary`. A new
state therefore cannot be introduced in one layer only.

## Evidence pipeline

```
upload ─▶ size check ─▶ magic-byte sniff ─▶ declared-type match ─▶ structural verify
       ─▶ decode ─▶ quality analysis (9 signals) ─▶ blocking? refuse with reasons
       ─▶ SHA-256 ─▶ duplicate check ─▶ store original (never modified)
       ─▶ enqueue job ─▶ 202 Accepted
                          │
worker ◀──────────────────┘
       ─▶ read original ─▶ derivatives (thumbnail, OCR input) ─▶ store
       ─▶ OCR: try eng+hin and eng, both orientations, keep the best measured read
       ─▶ barcode read ─▶ extraction: locate declarations, map regions to original
       ─▶ write candidates as PENDING review
       ─▶ advance the inspection when nothing is left to analyse
```

Quality is measured in the request, not the worker, because the officer is standing in
front of the package and needs to know immediately whether to photograph it again.

## Rule evaluation

```
reviewed candidates ─┐
product record ──────┼─▶ selection: status → dates → commodity → package
inspection context ──┘             → quantity band → import → e-commerce
                                   → multipiece → exceptions
                                        │
                                        ▼
                        one applicable rule version per rule code
                                        │
                                        ▼
                        deterministic check (Decimal arithmetic)
                                        │
                                        ▼
                  Finding: outcome + citation + selection reason
                           + inputs + calculation steps
```

Only candidates an officer confirmed or corrected are inputs. Pending and rejected
readings contribute nothing, which is why running the checks before review produces
"unable to determine" rather than violations.

Rule versions reach `approved` only through maker-checker: the author cannot approve
their own version, and a simulator run covering every mandatory scenario must pass
first.

## Report integrity

At issue time the entire inspection state is serialised into one snapshot and hashed
with a canonical JSON encoding: sorted keys, no insignificant whitespace, UTF-8 rather
than escaped ASCII, `Decimal` as its exact string form, datetimes as UTC ISO-8601.
The same logical content therefore always produces the same digest.

PDF and DOCX are rendered from that snapshot, never from live tables, and each document
carries its own hash. A database trigger refuses any change to a stored snapshot, and
the download endpoint re-checks the document hash before serving and refuses on
mismatch.

## Audit chain

Each event stores the hash of its predecessor. Appends are serialised with a
transaction-level advisory lock so two concurrent requests cannot fork the chain.
`audit_events` rejects `UPDATE` and `DELETE` through a trigger, so the application
cannot rewrite history even if asked to. `GET /audit/verify` replays the chain and
detects an altered field, a removed row and a rewritten link, naming the sequence where
the break begins.

## Deliberate omissions

- **No offline-first client.** The browser application detects and reports offline
  state, but a full local-draft-and-sync store is not implemented. Recording it as
  missing is more useful than shipping a half-working sync that loses evidence.
- **No e-commerce listing fetcher in this build.** The SSRF-hardened design is
  described in `docs/KNOWN_LIMITS.md`; the code is not present, so nothing claims to
  fetch listings.
- **No real digital signature.** The signer is an adapter. It returns `none` or a
  clearly labelled `development` HMAC. Fabricating a signature would be worse than
  having none.
