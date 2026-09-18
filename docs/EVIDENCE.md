# Evidence and verification

## The rule

The original bytes and the `evidence` row that describes them are never modified after
creation. Everything the pipeline produces is a derivative or a separate row.

This is enforced in three places, so a bug in one does not defeat it:

1. `app/services/evidence.py` writes the original with `overwrite=False`.
2. A database trigger refuses any `UPDATE` that changes an evidence row's `sha256`,
   `size_bytes`, `storage_bucket`, `storage_key`, `inspection_id` or `server_received_at`.
3. The originals bucket has versioning enabled, so an accidental overwrite is recoverable.

Analysis results, quality findings and retention flags are expected to change and are not
protected.

## What is recorded for every file

| Field | Source |
| --- | --- |
| `original_filename` | As supplied |
| `supplied_mime_type` | As claimed by the client |
| `detected_mime_type` | **Read from the file's own bytes**, never trusted from the client |
| `size_bytes` | Measured |
| `hash_algorithm`, `sha256` | Computed on receipt |
| `original_width`, `original_height` | From the decoded image |
| `storage_bucket`, `storage_key`, `storage_version_id`, `storage_encryption` | From the storage response |
| `uploaded_by_id`, `uploaded_by_public` | The account, or a flag for an anonymous complaint |
| `upload_client_time` | Device time, if supplied |
| `server_received_at` | Server time, authoritative |
| `upload_ip` | Source address |
| `device_make`, `device_model` | From EXIF, when present |
| `capture_latitude`, `capture_longitude`, `capture_accuracy_m` | Only when the officer explicitly permits location capture |
| `exif_handling`, `exif_summary` | `preserved_in_record`: EXIF is read into the record, the original bytes keep theirs, derivatives are stripped |
| `quality`, `quality_override_reason`, `quality_accepted_by_id` | The measured signals, and who accepted a poor image and why |
| `malware_scan_state` | `not_scanned` — see `docs/KNOWN_LIMITS.md` |
| `retention_state`, `retain_until`, `legal_hold` | Retention controls |

GPS is deliberately **not** taken from EXIF. Location is recorded from an explicit officer
action or not at all, so a photograph's metadata cannot silently place an officer
somewhere.

## Intake validation order

Cheapest and most decisive checks first, so a hostile file never reaches a decoder for
the type it claimed:

```
size limit ─▶ magic-byte sniff ─▶ declared type must match detected type
           ─▶ structural verify on a throwaway handle
           ─▶ pixel-count limit (decompression guard)
           ─▶ full decode
           ─▶ quality analysis
           ─▶ SHA-256 ─▶ duplicate check ─▶ store
```

A file claiming `image/jpeg` whose bytes are a Windows executable is rejected with 415
before any decoder runs. `Image.MAX_IMAGE_PIXELS` is set explicitly rather than left at
the library default.

## Storage separation

| Bucket | Contents | Lifecycle |
| --- | --- | --- |
| `maanak-originals` | Evidence exactly as received, complaint attachments | Never modified. Versioned. |
| `maanak-derivatives` | Thumbnails, OCR-input images | Regenerable; safe to purge and rebuild |
| `maanak-reports` | Issued PDF and DOCX | Written once per format per report |

Every object is written with server-side encryption requested. Keys are built from
server-controlled parts only and validated against a pattern that refuses traversal.

## How evidence is served

| Route | Behaviour |
| --- | --- |
| Thumbnails in lists | Short-lived signed URL to the derivative |
| Full image in the review screen | Short-lived signed URL to the original |
| `GET .../evidence/{id}/download` | Streamed **through the API**, so every access appends an `evidence.downloaded` audit event. Responds with the recorded hash in `X-Evidence-SHA256` |

Signed URLs are signed against the browser-reachable endpoint
(`S3_PUBLIC_ENDPOINT_URL`), not the internal one, because a URL signed for the wrong host
fails signature validation.

## Re-verifying evidence at any time

```
GET /api/v1/inspections/{id}/evidence/{evidence_id}/integrity
```

Re-reads the object from storage, recomputes the SHA-256 and compares it with the value
recorded at upload:

```json
{"verified": true,
 "detail": "The stored file matches the hash recorded at upload.",
 "recorded_sha256": "7234...", "algorithm": "sha256"}
```

A mismatch says plainly that the evidence should be treated as unreliable and
investigated. It is not silently repaired.

## Report snapshots

At issue time the whole inspection is serialised into one structure and hashed:

- the inspection, its context and its recorded decision;
- the product, its identifiers and responsible parties;
- the package-face checklist, including faces **not** captured and why;
- every evidence file with its hash, its quality verdict and any override reason;
- every reading, with **both** the machine value and the officer's correction;
- every finding, with its inputs, its calculation steps and its cited rule version;
- each cited rule version's citation, effective window and confirmation status;
- the people involved and the full timeline;
- the report's own stated limits.

### The hash

Canonical JSON: sorted keys, no insignificant whitespace, UTF-8 rather than escaped
ASCII, `Decimal` written as its exact string form, datetimes as UTC ISO-8601 with an
explicit offset. The same logical content always produces the same digest, on any machine.

`Decimal("45.00")` and `Decimal("45.0")` hash **differently**, because they are different
printed prices.

The snapshot is protected by a database trigger: the stored content, its hash, the issue
time, the reference and the verification code cannot change. Withdrawal and supersession
are separate columns.

### Documents

PDF and DOCX are rendered from the snapshot, never from live tables, and each carries its
own SHA-256. The download endpoint recomputes the document hash before serving and
**refuses on mismatch** rather than delivering a file it cannot vouch for.

### Signature

`sign_snapshot()` returns one of:

| Status | Meaning |
| --- | --- |
| `none` | Not signed. The document carries a content hash only. |
| `development` | An HMAC over the snapshot hash using a local key, labelled a development signature everywhere it appears. |

There is no third option in this build. A fabricated signature would be worse than none.

## Public verification

```
GET /api/v1/public/reports/verify?reference=RPT-2026-000001
GET /api/v1/public/reports/verify?code=T6Q9-DGC3-UM7E
```

Returns only: the reference, the verification code, when it was issued, the issuing
workspace, the jurisdiction name, the state, the revision, the content hash and whether
the content is intact, the signature status, and whether it was withdrawn or superseded.

It does **not** return the product, the brand, the premises, the officer, the decision or
any finding. A reference printed on a served document is not authorisation to read the
case.

An unknown reference and a wrong verification code produce the **same** answer, so the
endpoint cannot be used to enumerate references. Verification codes avoid characters that
are confusable in print: `0`/`O`, `1`/`I`/`L`, `5`/`S`, `B`, `Z`.

## Audit chain

Each event stores the hash of its predecessor. Appends are serialised with a
transaction-level advisory lock so two concurrent requests cannot fork the chain, and the
audit write happens in the same transaction as the change it records.

`GET /api/v1/audit/verify` replays the chain and distinguishes three kinds of tampering:

| Detected | How |
| --- | --- |
| An altered field | The recomputed event hash no longer matches the stored one |
| A removed row | A gap in the sequence |
| A rewritten link | `previous_hash` does not match the preceding event |

The response names the sequence where the break begins and says the trail should be
treated as compromised from that point.

The hash deliberately excludes `request_id`, `ip_address` and `user_agent`: those are
useful for investigation but are transport details a proxy can change, and including them
would make the chain depend on them.

`audit_events` rejects `UPDATE` and `DELETE` through a trigger. See
`docs/KNOWN_LIMITS.md` for why a restricted database role would be stronger still.
