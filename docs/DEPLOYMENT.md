# Deployment

## Clean install

Requires Docker with Compose v2. Nothing else on the host.

```bash
# 1. Environment
cp .env.example .env
```

Replace every `CHANGE-ME` value in `.env`:

```bash
# 48-byte URL-safe secrets, for POSTGRES_PASSWORD, JWT_SECRET, S3_SECRET_KEY
python -c "import secrets; print(secrets.token_urlsafe(48))"

# base64 of 32 random bytes, for the key part of MINIO_KMS_SECRET_KEY
python -c "import base64,os; print(base64.b64encode(os.urandom(32)).decode())"
```

`MINIO_KMS_SECRET_KEY` takes the form `<key-name>:<base64-of-32-bytes>`, for example
`maanak-evidence-key:Xy9...=`. Without it MinIO rejects every write, because Maanak asks
for server-side encryption on every object.

```bash
# 2. Build and start
docker compose up --build -d

# 3. Confirm readiness
curl -fsS http://localhost:8000/health/ready
```

Readiness reports each dependency separately and returns 503 while any is unavailable:

```json
{"status":"ok","version":"3.0.0",
 "components":{"database":"ok","queue":"ok","object_storage":"ok","ocr":"ok"}}
```

Migrations run in a one-shot `migrate` service before the API starts, so the API never
serves traffic against an unmigrated schema. Object-storage buckets are created on first
use; there is no manual setup step.

```bash
# 4. Create the first administrator (once only)
curl -X POST http://localhost:8080/api/v1/auth/bootstrap \
  -H 'Content-Type: application/json' \
  -d '{"email":"you@example.org","name":"Workspace Administrator",
       "password":"choose-a-long-passphrase",
       "jurisdiction_code":"IN","jurisdiction_name":"National workspace"}'
```

Then sign in at http://localhost:8080/login.html.

## Services

| Service | Image | Port | Purpose |
| --- | --- | --- | --- |
| `db` | `postgres:16.6-alpine` | internal | The only relational database |
| `redis` | `redis:7.4-alpine` | internal | Job queue, rate-limit counters |
| `minio` | `quay.io/minio/minio` | 9000, 9001 | S3-compatible object storage |
| `migrate` | built from `api/` | — | One-shot `alembic upgrade head`, then exits |
| `api` | built from `api/` | 8000 | FastAPI application |
| `worker` | built from `api/` | — | arq worker: imaging, OCR, extraction |
| `web` | built from `web/` | 8080 | nginx: static files and `/api` proxy |

`db` and `redis` are not published to the host. Use `docker compose exec` to reach them.

The MinIO image comes from `quay.io`: the `docker.io/minio/minio` mirror is not publicly
pullable.

## Environment variables

### Required

| Variable | Notes |
| --- | --- |
| `POSTGRES_PASSWORD` | Database password |
| `JWT_SECRET` | At least 32 characters. Refuses placeholder values containing `change-this`, `replace-with` and similar |
| `S3_SECRET_KEY` | Object storage secret |
| `MINIO_KMS_SECRET_KEY` | `<name>:<base64 32 bytes>`. Enables encryption at rest |

### Commonly changed

| Variable | Default | Notes |
| --- | --- | --- |
| `MAANAK_ENV` | `development` | `production` enables the startup safety checks |
| `COOKIE_SECURE` | `false` | **Must be `true` behind HTTPS** |
| `TRUSTED_HOSTS` | `*` | Must be explicit hostnames in production |
| `DOCS_ENABLED` | `true` | Set `false` in production |
| `MAX_UPLOAD_MB` | `20` | Also raise `client_max_body_size` in `web/nginx.conf` |
| `PUBLIC_BASE_URL` | `http://localhost:8080` | Used to build report verification URLs |
| `S3_PUBLIC_ENDPOINT_URL` | `http://localhost:9000` | Browser-reachable storage endpoint for signed URLs |

### Sessions and passwords

| Variable | Default | Notes |
| --- | --- | --- |
| `ACCESS_TOKEN_TTL_SECONDS` | 900 | Short-lived access cookie |
| `REFRESH_TOKEN_TTL_SECONDS` | 1209600 | 14 days |
| `SESSION_IDLE_TIMEOUT_SECONDS` | 28800 | 8 hours of inactivity |
| `PASSWORD_MIN_LENGTH` | 12 | Minimum 12, enforced |
| `LOGIN_MAX_ATTEMPTS` | 5 | Then a temporary lock |
| `LOGIN_LOCKOUT_SECONDS` | 900 | Lock duration |
| `ARGON2_TIME_COST` / `ARGON2_MEMORY_COST_KIB` / `ARGON2_PARALLELISM` | 3 / 65536 / 2 | Raise cost as hardware allows |

### Signing-key rotation

`JWT_SECRET_PREVIOUS` holds the outgoing key. Tokens are signed with `JWT_SECRET` and
verified against both, so rotation does not end active sessions:

1. Move the current value to `JWT_SECRET_PREVIOUS`.
2. Put a new value in `JWT_SECRET`.
3. Restart the API.
4. After the access-token TTL has elapsed, clear `JWT_SECRET_PREVIOUS` and restart again.

## Production readiness gate

With `MAANAK_ENV=production` the API refuses to start when any of these hold:

- `COOKIE_SECURE` is false
- `ALLOW_SELF_APPROVAL` is true
- `REPORT_SIGNER` is `development`
- `S3_SECRET_KEY` is still the development default
- `TRUSTED_HOSTS` contains `*`
- `DOCS_ENABLED` is true

The error names every problem at once rather than one at a time. It cannot check the
non-technical prerequisites in `docs/KNOWN_LIMITS.md`.

## Behind a reverse proxy

The API is started with `--proxy-headers --forwarded-allow-ips "*"`, so it trusts
`X-Forwarded-For` and `X-Forwarded-Proto` from whatever sits in front of it. In
production restrict `--forwarded-allow-ips` to your proxy's address, otherwise a client
can spoof its own address in the audit trail.

`web/nginx.conf` already forwards those headers, preserves cookies, and sets the
`Content-Security-Policy`. An outer proxy should terminate TLS and add HSTS.

## Operations

```bash
# Health
curl -fsS http://localhost:8000/health/live      # process only
curl -fsS http://localhost:8000/health/ready     # dependencies
curl -fsS http://localhost:8000/metrics          # Prometheus exposition

# Logs (structured JSON, one object per line)
docker compose logs -f api
docker compose logs -f worker

# Queue depth and stalled work
docker compose exec db psql -U maanak -d maanak \
  -c "SELECT state, count(*) FROM analysis_jobs GROUP BY state;"

# Verify the audit chain
curl -fsS http://localhost:8080/api/v1/audit/verify -b cookies.txt
```

Every log line carries `request_id`, which is also returned in the `X-Request-ID`
response header and stored on the matching audit event, so a user-reported problem can
be traced from the response they saw to the database change it caused.

### Metrics exposed

`maanak_http_requests_total`, `maanak_http_request_seconds`,
`maanak_jobs_enqueued_total`, `maanak_jobs_completed_total`, `maanak_job_seconds`,
`maanak_ocr_seconds`, `maanak_queue_depth`, `maanak_storage_operations_total`,
`maanak_reports_issued_total`, `maanak_login_attempts_total`.

`/metrics` is unauthenticated and must not be published beyond the deployment network.

## Troubleshooting a first install

These are the failure modes actually encountered while installing this from a clean
archive, with the message you will see.

**`minio` restarts with `FATAL Failed to connect to KMS: illegal base64 data at input byte 6`**

`MINIO_KMS_SECRET_KEY` still contains the placeholder. The value must be
`<key-name>:<base64 of exactly 32 bytes>`; MinIO tries to base64-decode the part after
the first colon and fails on the placeholder text. Generate it with:

```bash
python -c "import base64,os; print(base64.b64encode(os.urandom(32)).decode())"
```

Nothing else will start, because `api` and `worker` depend on `minio` being healthy.

**`api` exits with `JWT_SECRET still contains a placeholder value`**

Deliberate. The configuration refuses values containing `change-this`, `replace-with`,
`changeme` and similar rather than starting with a guessable signing key.

**`alembic` reports `Can't locate revision`**

The `migrate` image is older than the migrations on disk. Rebuild all services together:
`docker compose build migrate api worker`.

**Readiness returns 503 with `"ocr": "missing_language_data"`**

The image was built without the Tesseract language packages. Rebuild without cache:
`docker compose build --no-cache api`.

**The browser application returns 502 on every `/api` request, but `api` is healthy**

nginx resolves the `api` hostname once at startup and caches the address. Recreating the
`api` container gives it a new address, and `web` keeps proxying to the old one, so
`http://localhost:8000/health/ready` succeeds while `http://localhost:8080/api/...`
returns 502.

Restart the proxy so it resolves again:

```bash
docker compose restart web
```

This bites whenever `api` is recreated on its own, which happens on every code change:

```bash
docker compose build api worker && docker compose up -d --force-recreate api worker
docker compose restart web        # do not skip this
```

**Sign-in returns 429 or 503 `rate_limit_backend_unavailable`**

Per-address sign-in limits are deliberately fail-closed. Either you have exceeded 20
attempts in five minutes, or Redis is unreachable. Clear the counters with
`docker compose run --rm --no-deps migrate python scripts/reset_data.py`, which also
truncates application data.

**Evidence images do not display in the browser**

`S3_PUBLIC_ENDPOINT_URL` must be the address the *browser* can reach, not the internal
service name. A URL signed for `http://minio:9000` fails signature validation when the
browser requests it from `http://localhost:9000`.

**A tool reports `pyproject.toml: Invalid statement (at line 1, column 1)`**

The file has a UTF-8 byte-order mark. TOML parsers reject it. Editors on Windows add one
silently; write the file without a BOM.

## Migrations

```bash
docker compose run --rm migrate alembic upgrade head        # apply
docker compose run --rm migrate alembic current             # current revision
docker compose run --rm migrate alembic downgrade 0001_initial_schema
```

To add a migration after changing the models:

```bash
docker compose run --rm --no-deps -v "$PWD/api:/app" migrate \
  sh scripts/makemigration.sh "what changed"
```

Review the generated file. Released migrations contain explicit DDL and must never
import `app.models`, so a historical migration keeps working after the models move on.
CI fails the build if an autogenerate against the migrated schema produces any table or
column change, which is how model drift is caught.

## Rollback

The application is stateless; rolling back means running the previous image.

1. Deploy the previous image tag.
2. If the newer version added a migration, `alembic downgrade <previous revision>`.
3. Confirm `alembic current` matches what the older image expects.
4. Check readiness.

Migrations are written so that a downgrade is possible, but a downgrade that drops a
column loses data. Take a backup first — see `docs/BACKUP_RESTORE.md`.

## Scaling

- **API**: stateless, scale horizontally. Sessions are in PostgreSQL, not in memory.
- **Worker**: `max_jobs = 2` per worker because OCR is CPU-bound. Add worker containers
  rather than raising concurrency on one.
- **PostgreSQL**: the first thing to watch. `analysis_jobs` and `audit_events` grow
  fastest; the audit table is append-only by design and should be archived by range
  rather than deleted.
- **Object storage**: growth is dominated by evidence originals. Derivatives are
  regenerable and can be purged.
