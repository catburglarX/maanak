# Backup and restore

Three things must be backed up together, and a restore is only correct when all three
agree:

| What | Where | Why it matters |
| --- | --- | --- |
| PostgreSQL | `db` service | Records, the audit chain, report snapshots |
| Object storage | `minio` service | Evidence originals, derivatives, report documents |
| `.env` | The host | Without `JWT_SECRET` every session ends; without `MINIO_KMS_SECRET_KEY` encrypted objects cannot be read |

A database backup without the matching object storage produces records that point at
evidence which no longer exists. The hashes will detect it, which is the point, but the
evidence is gone.

`MINIO_KMS_SECRET_KEY` deserves emphasis: objects are written with server-side
encryption. Losing that key loses the evidence, whatever the object backup contains.

## Backup

```bash
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "backup/$STAMP"

# 1. Database. Custom format so it can be restored selectively.
docker compose exec -T db pg_dump -U maanak -d maanak -Fc \
  > "backup/$STAMP/maanak.dump"

# 2. Object storage. mc is present in the MinIO image.
docker compose exec -T minio mc alias set local http://127.0.0.1:9000 \
  "$S3_ACCESS_KEY" "$S3_SECRET_KEY" >/dev/null
for bucket in maanak-originals maanak-derivatives maanak-reports; do
  docker compose exec -T minio mc mirror --overwrite "local/$bucket" "/data-backup/$bucket"
done

# 3. Configuration. Store this where secrets belong, not beside the dumps.
cp .env "backup/$STAMP/env.backup"

# 4. Record what the audit chain looked like at backup time.
curl -fsS -b cookies.txt http://localhost:8080/api/v1/audit/verify \
  > "backup/$STAMP/audit-chain-at-backup.json"
```

Step 4 is the one people skip. Recording the chain head at backup time is what lets you
prove later that the restored trail is the same one, not a truncated copy.

Mount a host directory at `/data-backup` on the `minio` service, or run `mc mirror`
against an off-host target.

### What can be skipped

`maanak-derivatives` is regenerable: thumbnails and OCR-input images are rebuilt from
originals by re-running analysis. Skipping it makes backups smaller and restores slower.

`maanak-originals` and `maanak-reports` cannot be skipped.

## Restore into a clean environment

```bash
# 1. Start only the infrastructure.
docker compose up -d --wait db redis minio

# 2. Restore the configuration first: the KMS key is needed before any object read.
cp backup/<stamp>/env.backup .env

# 3. Database. --clean --if-exists so a partially initialised database is replaced.
docker compose exec -T db psql -U maanak -d postgres \
  -c "DROP DATABASE IF EXISTS maanak;" -c "CREATE DATABASE maanak OWNER maanak;"
docker compose exec -T db pg_restore -U maanak -d maanak --no-owner \
  < backup/<stamp>/maanak.dump

# 4. Object storage.
docker compose exec -T minio mc alias set local http://127.0.0.1:9000 \
  "$S3_ACCESS_KEY" "$S3_SECRET_KEY" >/dev/null
for bucket in maanak-originals maanak-derivatives maanak-reports; do
  docker compose exec -T minio mc mb --ignore-existing "local/$bucket"
  docker compose exec -T minio mc mirror --overwrite "/data-backup/$bucket" "local/$bucket"
done

# 5. Bring up the application.
docker compose up -d api worker web
curl -fsS http://localhost:8000/health/ready
```

The audit triggers, the report and notice immutability triggers and the reference
sequences are all created by migration `0002`, so `pg_restore` of a dump taken from a
migrated database brings them back. Confirm with `alembic current`.

## Verify the restore

A restore is not finished until these four pass. Nothing here is optional.

### 1. Schema and protections

```bash
docker compose run --rm --no-deps migrate python scripts/verify_schema.py
```

14 checks: the tables, the indexes, the constraints, and, most importantly, that
`audit_events` still refuses `UPDATE` and `DELETE`. A restore that lost the triggers
would pass a naive row count and fail here.

### 2. The audit chain

```bash
curl -fsS -b cookies.txt http://localhost:8080/api/v1/audit/verify
```

Compare `head_sequence` and `head_hash` with
`backup/<stamp>/audit-chain-at-backup.json`. Equal values mean the restored trail is the
same trail. `intact: false` means the restore is not trustworthy. Investigate rather
than proceed.

### 3. Evidence hashes

Every evidence file can be re-verified against the hash recorded at upload:

```bash
GET /api/v1/inspections/{inspection_id}/evidence/{evidence_id}/integrity
```

To sweep everything, iterate the register. A file whose object is missing or whose bytes
changed reports `verified: false` with a plain explanation.

### 4. Report snapshots and documents

```bash
GET /api/v1/reports/{report_id}                 # integrity.intact must be true
GET /api/v1/reports/{report_id}/document.pdf    # refuses on hash mismatch
```

The snapshot hash is recomputed from the stored content, so a report that survived the
restore intact verifies exactly as it did before. The document endpoint re-checks the
file hash before serving and refuses rather than delivering a file it cannot vouch for.

A public verification of a known reference is the end-to-end check:

```bash
curl -fsS "http://localhost:8080/api/v1/public/reports/verify?reference=RPT-2026-000001"
```

`content_intact: true` after a restore means the record, its hash and the stored bytes all
still agree.

## Retention and legal hold

| Field | Effect |
| --- | --- |
| `Evidence.retention_state` | `active`, `scheduled_for_deletion`, `deleted` |
| `Evidence.retain_until` | When retention expires |
| `Evidence.legal_hold` | Set true to prevent deletion regardless of retention |
| `Inspection.legal_hold`, `Inspection.retention_state` | The same at inspection level |

The fields are recorded and honoured by the model. **No automatic deletion job runs in
this build**: nothing is deleted until an operator acts. That is deliberate. An
automatic deleter that removes evidence under legal hold because of a date-arithmetic bug
is a worse failure than manual work.

`audit_events` is append-only and is not subject to retention deletion. Archive it by
sequence range rather than deleting rows.

## Incident response

If a hash mismatch or a broken chain is found:

1. **Do not repair it.** The mismatch is the evidence.
2. Record the finding: which record, which hash, when it was noticed, by whom.
3. `GET /api/v1/audit/verify` and note `first_broken_sequence`. Everything before it is
   still verifiable.
4. `GET /api/v1/audit/entity/{type}/{id}` for the affected records, and the API logs for
   the matching `request_id`.
5. Compare with the last backup whose verification passed. The difference bounds when the
   problem began.
6. Treat any report issued after the break as unverified until its snapshot hash is
   checked against a known-good backup.

Every audit event carries the `request_id` of the request that caused it, and that id is
in the API log and was returned to whoever made the request, so the three can be joined.
