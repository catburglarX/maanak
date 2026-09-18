# Running Maanak on localhost

Docker with Compose v2 is the only thing you install. No Python, no Node, no database,
no Tesseract on the host. Everything runs in containers, and the host only needs to be
able to start them.

## What the machine needs

Docker Desktop on Windows or macOS, or Docker Engine with the Compose plugin on Linux.
On Windows that means Windows 10 or 11 with the WSL2 backend, which needs hardware
virtualisation enabled in the BIOS. If Docker Desktop refuses to start and complains
about virtualisation, that switch is the reason, and no amount of reinstalling will fix
it.

About 4 GB of free disk. The images measure roughly 2 GB once built: one 1.18 GB Python
image shared by `api`, `worker` and `migrate`, then 394 MB for PostgreSQL, 244 MB for
MinIO, 78 MB for the web image and 58 MB for Redis. Build cache takes the rest.

8 GB of RAM is comfortable. The six containers measured 693 MiB at peak while the worker
was reading an image, so 4 GB works, but it leaves little room for Docker Desktop itself
and a browser.

You do not need git. GitHub's Code button offers a ZIP download, and the project builds
from an extracted folder exactly as it does from a clone.

## Starting it

Open a terminal in the project folder, the one holding `compose.yaml`.

### 1. Create the environment file

`.env` is deliberately absent from the repository, so a fresh copy has no secrets in it.

```powershell
Copy-Item .env.example .env
```

On macOS or Linux:

```bash
cp .env.example .env
```

### 2. Generate the four secrets

`.env.example` marks four values `CHANGE-ME`. This command prints all four, filled in and
ready to paste, and it needs nothing on the host but Docker:

```powershell
docker run --rm python:3.12-slim python -c "import secrets,base64,os; print('POSTGRES_PASSWORD='+secrets.token_urlsafe(36)); print('JWT_SECRET='+secrets.token_urlsafe(48)); print('S3_SECRET_KEY='+secrets.token_urlsafe(36)); print('MINIO_KMS_SECRET_KEY=maanak-evidence-key:'+base64.b64encode(os.urandom(32)).decode())"
```

Open `.env` in any text editor and replace the four `CHANGE-ME` lines with the four lines
it printed. Leave everything else as it is. The remaining defaults are already correct
for localhost.

`MINIO_KMS_SECRET_KEY` has to keep the `maanak-evidence-key:` prefix. MinIO reads it as
a key name and a base64 key separated by a colon, and it will refuse to start if the
format is wrong.

### 3. Build and start

```powershell
docker compose up --build -d
```

The first run takes 10 to 20 minutes on a reasonable connection. It is downloading base
images and installing OpenCV, Tesseract with English and Hindi language data, ReportLab
and python-docx. Later starts take a few seconds.

Migrations run on their own. The `api` service waits for the `migrate` container to exit
successfully, so there is no separate migration step.

### 4. Confirm it is ready

```powershell
(Invoke-WebRequest -Uri http://localhost:8000/health/ready -UseBasicParsing).Content
```

On macOS or Linux, `curl -s http://localhost:8000/health/ready`.

All four components must report `ok`:

```json
{"status":"ok","version":"3.0.0","components":{"database":"ok","queue":"ok","object_storage":"ok","ocr":"ok"}}
```

The endpoint returns 503 while any of the four is still coming up, which on a cold start
is normal for the first 20 seconds or so. If one stays down, `docker compose logs api`
and `docker compose logs worker` say why.

### 5. Load the demo workspace

Without this the application works but every register is empty, which makes it hard to
tell a working install from a broken one.

```powershell
docker compose run --rm --no-deps -e API_URL=http://api:8000 --entrypoint python api scripts/seed_demo.py
docker compose run --rm --no-deps -e API_URL=http://api:8000 --entrypoint python api scripts/seed_example.py
```

The first creates one account per role, loads the eleven starter rules, and takes each
one through simulation and approval using the second rule administrator, because an
author cannot approve their own version. It prints the password every account shares.

The second walks a single case through the whole path and prints each step: complaint
`CMP-2026-000001`, inspection `INSP-2026-000001`, eleven readings confirmed, checks,
decision, report `RPT-2026-000001`, case `CASE-2026-000001`, notice `NOT-2026-000001`
served.

### 6. Open it

| What | Where |
| --- | --- |
| Browser application | http://localhost:8080 |
| Officer sign-in | http://localhost:8080/login.html |
| API documentation | http://localhost:8000/docs |
| Readiness | http://localhost:8000/health/ready |
| Object storage console | http://localhost:9001 |

Sign in as `reviewer@example.org` to see a decided inspection with its report, or
`inspector@example.org` to capture evidence and run the checks. The seeder prints the
password. Those are throwaway local credentials and the README says so.

## Four things that will confuse you

Every one of these cost real time to work out, so they are written down rather than left
to be rediscovered.

**Editing anything under `web/` appears to do nothing.** The web image copies those files
in at build time, so `docker compose restart web` serves the old ones. You need a
rebuild:

```powershell
docker compose build web
docker compose up -d --force-recreate web
```

The same applies to `api/`: rebuild `api`, `worker` and `migrate` together, or alembic
reports that it cannot locate a revision.

**Evidence thumbnails only load in a browser on the same machine.** Signed storage URLs
point at `S3_PUBLIC_ENDPOINT_URL`, which is `http://localhost:9000` by default. A browser
on another device on your network resolves that to itself and gets nothing. The image
bytes are fine; only the URL is local.

**Repeated sign-ins start failing with `invalid_credentials`.** Sign-in is rate limited
per address and per account, and it fails closed. After a long session of test runs you
will hit the limit and it looks exactly like a wrong password. Clear the counters without
touching any data:

```powershell
docker compose exec redis sh -c "redis-cli --scan --pattern 'maanak:rl:*' | xargs -r -n1 redis-cli DEL"
```

**If `migrate` fails with `password authentication failed for user "maanak"`** after you
have changed `.env`, the database volume still holds the password it was initialised
with. `POSTGRES_PASSWORD` only takes effect when the data directory is first created.
Worse, `pg_hba.conf` trusts connections from `127.0.0.1`, so `docker compose exec db
psql` keeps working and hides the problem. Set the role password to match `.env`:

```powershell
docker compose exec -T db psql -U maanak -d postgres -c "ALTER ROLE maanak WITH PASSWORD 'the-value-from-your-env';"
```

Or start clean with `docker compose down -v`, which deletes the volumes and everything
in them.

## Everyday commands

Stop the stack but keep the data:

```powershell
docker compose down
```

Stop it and delete the database, the object storage and every uploaded image:

```powershell
docker compose down -v
```

Empty the application tables and restart the reference sequences, keeping the schema.
This also flushes the Redis rate-limit counters:

```powershell
docker compose run --rm --no-deps -v "${PWD}/api:/app" migrate python scripts/reset_data.py
```

Follow the logs of one service:

```powershell
docker compose logs -f worker
```

See what each container is using, which is worth knowing before you put this on a small
server:

```powershell
docker stats --no-stream
```

## Running the checks

The suites print every check with the value measured, so a passing run reads as a
statement of what was verified.

Formatting, lint and types:

```powershell
docker compose run --rm --no-deps --entrypoint sh api scripts/quality.sh
```

Unit tests, the prose gate and the live-stack suites. Mount the repository root rather
than `api/` alone, because the prose gate reads `web/` and `docs/`, and one test compares
the label table in `web/js/util.js` against the Python one:

```powershell
docker compose run --rm --no-deps -v "${PWD}:/repo" -w /repo/api -e PYTHONPATH=/repo/api --entrypoint sh api scripts/run_tests.sh
```

The browser suites need a second image, built once:

```powershell
docker build -f api/Dockerfile.browser -t maanak-browser:dev api
docker run --rm --network maanak_default -v "${PWD}/api:/w" -w /w -e BASE_URL=http://web:8080 maanak-browser:dev
```

Ordering matters and getting it wrong produces a confusing failure. Each of the eight
`verify_*` suites bootstraps its own workspace, so they reset the data as they run. Run
`scripts/reset_data.py` between them. Anything that needs the worked example, which means
`audit_app.py`, `check_report_text.py`, `check_workspace_chrome.py` and
`verify_officer_flow.py`, has to run after the two seeders rather than before.

`docs/TESTING.md` lists every suite, what it needs and what it proves.

## If it still will not start

Check the ports first. The stack binds 8080, 8000, 9000 and 9001 on the host, and
`docker compose up` fails with a bind error if something else already holds one of them.

`docker compose ps` shows each service with its health state. A service stuck in
`starting` for more than a minute has a real problem, and its logs will name it. The
usual causes are a `CHANGE-ME` value left in `.env`, a malformed
`MINIO_KMS_SECRET_KEY`, or a stale database volume as described above.

`docs/DEPLOYMENT.md` covers the same ground for a server, along with what has to change
before the application is exposed to the internet.
