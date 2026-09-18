# Testing

Every command below has been run. Each suite prints one line per check with the
measured value, so a passing run reads as a statement of what was verified rather than a
count.

## The commands

```bash
# 1. Formatting, lint and types
docker compose run --rm --no-deps --entrypoint sh api scripts/quality.sh

# 2. Fast unit tests, the prose gate, then the live-stack suites. Mount the repository
#    root rather than api/ alone: the prose gate reads web/ and docs/, and the label
#    parity test reads web/js/util.js.
docker compose run --rm --no-deps -v "$PWD:/repo" -w /repo/api \
  -e PYTHONPATH=/repo/api --entrypoint sh api scripts/run_tests.sh

# Unit tests and the prose gate only, no services needed
docker compose run --rm --no-deps -v "$PWD:/repo" -w /repo/api \
  -e PYTHONPATH=/repo/api -e MAANAK_TEST_SCOPE=fast \
  --entrypoint sh api scripts/run_tests.sh

# 3. Browser and accessibility
docker build -f api/Dockerfile.browser -t maanak-browser:dev api
docker run --rm --network maanak_default -v "$PWD/api:/w" -w /w \
  -e BASE_URL=http://web:8080 maanak-browser:dev
docker run --rm --network maanak_default -v "$PWD/api:/w" -w /w \
  -e BASE_URL=http://web:8080 -e API_URL=http://api:8000 \
  maanak-browser:dev python scripts/audit_app.py
docker run --rm --network maanak_default -v "$PWD/api:/w" -w /w \
  -e BASE_URL=http://web:8080 maanak-browser:dev python scripts/measure_a11y.py
docker run --rm --network maanak_default -v "$PWD/api:/w" -w /w \
  -e BASE_URL=http://web:8080 maanak-browser:dev python scripts/scan_tints.py --app
docker run --rm --network maanak_default -v "$PWD/api:/w" -w /w \
  -e BASE_URL=http://web:8080 maanak-browser:dev python scripts/check_links.py

# 4. Archive integrity
bash scripts/package.sh && bash scripts/verify_package.sh

# 5. The generated documents and the shared workspace chrome. Both need a seeded
#    workspace, so run these after scripts/seed_example.py rather than after a reset.
docker compose run --rm --no-deps -e API_URL=http://api:8000 \
  --entrypoint python api scripts/check_report_text.py
docker run --rm --network maanak_default -v "$PWD/api:/w" -w /w \
  -e BASE_URL=http://web:8080 maanak-browser:dev python scripts/check_workspace_chrome.py

# 6. The officer workflow, driven through the interface from an empty inspection to an
#    opened case. Needs the demo accounts and a generated label to upload.
docker compose run --rm --no-deps --user root -v "$PWD/api:/app" \
  --entrypoint python api scripts/make_sample_label.py
docker run --rm --network maanak_default -v "$PWD:/repo" -v "$PWD/api:/w" -w /w \
  -e BASE_URL=http://web:8080 maanak-browser:dev python scripts/verify_officer_flow.py

# 7. Static checks that need no stack. Each one exists because it caught a real defect.
docker run --rm -v "$PWD:/repo" -w /repo maanak-api:latest \
  python api/scripts/check_js_bindings.py web/js
docker run --rm -v "$PWD:/repo" -v "$PWD/api:/api" -w /repo maanak-api:latest \
  sh -c "PYTHONPATH=/api python api/scripts/check_permission_names.py"
docker run --rm -v "$PWD/api:/w" -w /w maanak-api:latest \
  python scripts/check_error_messages.py
docker run --rm --network maanak_default -v "$PWD:/repo" -w /repo maanak-api:latest \
  python api/scripts/check_api_reach.py --base http://api:8000
```

Two ordering rules matter, and ignoring either produces a confusing failure.

The eight `verify_*` suites and the integration half of `run_tests.sh` each bootstrap
their own workspace, so they reset the data as they go. Anything that needs the worked
example, which is `audit_app.py`, `check_report_text.py` and `check_workspace_chrome.py`,
has to run after `seed_demo.py` and `seed_example.py`, not before.

Sign-in is rate limited per address and per account and fails closed, so a long session
of repeated runs eventually gets `invalid_credentials` rather than a session. Clear the
counters without touching the data:

```bash
docker compose exec redis sh -c \
  "redis-cli --scan --pattern 'maanak:rl:*' | xargs -r -n1 redis-cli DEL"
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
| `tests/test_presentation.py` | 24 | nothing | Every enum label reads as English rather than as a raw value; the JavaScript override table matches the Python one; counted nouns never fall back to "(s)"; the decision choices offered are exactly the ones the service accepts; no em dash survives in any source file |
| `tests/test_imaging_quality.py` | 11 | nothing | A plain studio backdrop is not counted as glare and not counted again as bright clipping, and a genuine highlight burnt into the panel still blocks. Both directions, because a fix that silenced glare everywhere would be worse than the false positive it replaced |
| `check_prose.py` | 41 files | nothing | No machine-writing tells across 28 pages and 13 documents: no tool leak markers, no scaffold headings, no participial tails, no copula avoidance, no em dash anywhere |
| `audit_app.py` | 106 | full stack + Chromium | Every route in the OpenAPI document is declared; all 28 pages load and reach their data; no stringified object or undefined value reaches the screen; the inspection screen passes axe as a reviewer, with the decision form rendered |
| `measure_a11y.py` | 14 pages | Chromium | Zero axe violations at WCAG 2.0, 2.1 and 2.2 level A and AA; zero targets under 24 by 24 CSS pixels, which axe has no rule for; zero sticky or fixed positioning on a public page |
| `scan_tints.py --app` | 24 pages | Chromium | No warm-tinted surface at any of three breakpoints, across the public site and the workspace |
| `check_links.py` | 0 broken | Chromium | Every internal link resolves and every in-page anchor has an element to land on |
| `check_workspace_chrome.py` | 14 screens | full stack + Chromium | One header and one footer on every workspace screen, the standing note present in each, and no em dash in the rendered text |
| `check_report_text.py` | 2 documents | full stack | The generated PDF and DOCX contain no em dash, no "(s)" plural, no stringified object and no raw enum value, checked by extracting the text a reader receives |
| `verify_officer_flow.py` | 44 | full stack + Chromium | The whole officer workflow driven through the interface: open an inspection, have a glared photograph refused with a named reason, upload a readable one, wait for the worker, read the OCR output, confirm eleven readings, run the checks, send for reviewer decision, record the decision, issue the report, open the case |
| `check_js_bindings.py` | 26 modules | nothing | No module uses a helper it never imported. A missing import throws only when the line runs, so one referenced in a rarely-taken branch can sit broken indefinitely |
| `check_permission_names.py` | 25 names | nothing | Every permission the interface asks for exists. A name that does not exist is never held, so the control it guards is hidden from everyone with no error anywhere |
| `check_error_messages.py` | 164 messages | nothing | No error an officer reads names a JSON key or a database column |
| `check_api_reach.py` | 92 operations | API | Every endpoint is either reached from a screen or recorded, with a reason, as deliberately not reached |

Total: **510 assertions** across the eight verification suites, plus **130 pytest items**
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
