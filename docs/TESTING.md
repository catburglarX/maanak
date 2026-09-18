# Testing

Every command below has been run. Each suite prints one line per check with the
measured value, so a passing run reads as a statement of what was verified rather than a
count.

## The commands

```bash
# 1. Formatting, lint and types
docker compose run --rm --no-deps --entrypoint sh api scripts/quality.sh

# 2. Fast unit tests, then the live-stack suites
docker compose run --rm --no-deps --entrypoint sh api scripts/run_tests.sh

# Unit tests only, no services needed
docker compose run --rm --no-deps \
  -e MAANAK_TEST_SCOPE=fast --entrypoint sh api scripts/run_tests.sh

# 3. Browser and accessibility
docker build -f api/Dockerfile.browser -t maanak-browser:dev api
docker run --rm --network maanak_default -v "$PWD/api:/w" -w /w \
  -e BASE_URL=http://web:8080 maanak-browser:dev

# 4. Archive integrity
bash scripts/package.sh && bash scripts/verify_package.sh
```

Reset application data between runs when driving the suites directly:

```bash
docker compose run --rm --no-deps migrate python scripts/reset_data.py
```

It truncates every table, restarts the reference sequences and clears the Redis
rate-limit counters. The counters matter: sign-in is limited per address and fails
closed, so repeated runs from one container would otherwise start returning 429.

## What each suite proves

| Suite | Checks | Needs | Proves |
| --- | --- | --- | --- |
| `verify_schema.py` | 14 | PostgreSQL | 30 tables, 184 indexes, 29 `CHECK` constraints, 74 foreign keys exist; `lower(email)` uniqueness is a functional index; `audit_events` rejects `UPDATE` and `DELETE` at the database level |
| `verify_security.py` | 51 | nothing | Argon2id hashing and salting; timing equalisation for unknown accounts; password policy; token signing, tampering rejection and key rotation; opaque refresh tokens stored only as hashes; the full permission matrix; hierarchical jurisdiction scope including prefix-confusion rejection |
| `verify_extraction.py` | 62 | nothing | Quantity parsing and exact unit conversion; Devanagari digits; multipiece totals; price parsing with Indian digit grouping; half-up rounding to paise; date precision and preserved ambiguity; declaration detection with image regions |
| `verify_rules.py` | 71 | nothing | Absent evidence never becomes a violation; unit-price arithmetic with recorded steps; character height refuses to guess from pixels; quantity band boundaries; effective-date windows; exceptions; only approved versions apply; the simulator gates approval |
| `verify_auth_flow.py` | 53 | API | Bootstrap is one-time; identical errors for unknown account and wrong password; `HttpOnly` cookies with a path-restricted refresh cookie; CSRF enforcement; refresh rotation; reuse of a rotated token revokes the family; immediate session revocation; mass-assignment rejection |
| `verify_pipeline.py` | 56 | API, worker, storage | A blurred, glared image is refused with specific instructions; SHA-256 matches the uploaded bytes; duplicates and non-images are refused; the worker reports real progress; candidates carry image regions inside the original bounds; raw OCR is retained; the stored file still matches its hash; the downloaded original is byte-identical |
| `verify_workflow.py` | 117 | full stack | Rule seeding as drafts; approval refused before simulation; the author cannot self-approve; checks before review produce no violations; a correction preserves the machine reading; stale writes are refused; every finding cites an approved rule version; an inspector cannot decide; cross-jurisdiction access returns 404; report snapshot, PDF and DOCX hashes; public verification leaks nothing |
| `verify_matters.py` | 86 | full stack | A public complaint with no account; the published triage priority; status lookup needs reference *and* contact; conversion to an inspection carrying the consumer's photograph; the controller hierarchy; case opening requires an issued report; a served notice is frozen; the audit chain verifies, and a forged event is detected with its sequence named |
| `verify_browser.py` | 59 | full stack + Chromium | Zero serious or critical axe violations on eight pages; one `h1` per page; the skip link is the first tab stop; every complaint field is labelled; no horizontal overflow at 320px or 390px; sign-in through the interface; `maanak_access` is `HttpOnly` and `maanak_csrf` is not; registers render real data; an inspector sees a permission message; sign-out and the unauthenticated redirect work |
| `tests/test_extraction_values.py` | 43 | nothing | The arithmetic a finding rests on, asserted as exact `Decimal` values |
| `tests/test_domain_units.py` | 51 | nothing | GS1 check digits worked through by hand; permission matrix; jurisdiction isolation; state machine edges, guards and reachability; canonical hashing determinism |

Total: **510 assertions** across the nine verification suites, plus **94 pytest items**
in the fast set (which includes three of the suites, since they need no services).

## Running one suite

```bash
docker compose run --rm --no-deps -e API_URL=http://api:8000 \
  --entrypoint python api scripts/verify_workflow.py
```

## Interpreting a failure

Each suite prints `ok` or `FAIL` per check with the measured value, then a summary. A
failure names the check and shows what was measured against what was expected, for
example:

```
  FAIL  exactly at the threshold with ±0.2 mm is undetermined, not decided
```

The pytest wrapper (`tests/test_verification_suites.py`) reproduces the suite's own
output on failure rather than an opaque assertion error, and asserts a minimum number of
checks per suite so a suite cannot silently shrink.

## What CI runs

`.github/workflows/verify.yml`, with every job required:

| Job | Contents |
| --- | --- |
| `quality` | `ruff format --check`, `ruff check`, `mypy app` |
| `unit` | Fast tests with coverage, uploaded as an artefact |
| `migrations` | Upgrade to head, downgrade, upgrade again, then **fail on model drift** if an autogenerate produces any table or column change |
| `integration` | Full stack, the live-stack suites, then the browser and accessibility suite |
| `supply-chain` | `pip-audit --strict`, CycloneDX SBOM, gitleaks secret scan, Trivy container scan failing on HIGH or CRITICAL |
| `release-archive` | Builds the archive and verifies it extracts complete |

Nothing is marked `continue-on-error`.

## Not tested

Stated plainly, with the reasoning in `docs/KNOWN_LIMITS.md`:

- **OCR accuracy.** No labelled corpus of real package photographs was available, so no
  character or word error rate is claimed. The pipeline is proven to work; its field
  accuracy is unmeasured.
- **Performance under load.** No response-time budget, no concurrency test beyond
  correctness, no storage-growth projection.
- **Manual screen-reader testing.** axe-core covers what a tool can; it does not cover
  what a NVDA or VoiceOver user experiences.
- **A full backup and restore drill.** The procedure is documented and the integrity
  re-verification endpoint exists, but the drill was not executed in this build.
- **Every route swept for object-level authorisation.** Jurisdiction isolation is proven
  on inspections, complaints, cases, reports and the audit trail; it was not
  independently probed on all 97 routes.
