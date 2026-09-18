# Demonstration

One connected story, in order. Roughly twelve minutes at a normal pace.

The point being demonstrated, in one sentence:

> OCR reads a label. Maanak creates a reviewable inspection record connecting the label,
> the evidence, an approved rule version, the officer's review and a verifiable report.

## Before you start

```bash
docker compose up --build -d
curl -fsS http://localhost:8000/health/ready      # all four components ok
```

Then prepare the workspace in one step. This creates one account per role, loads the
starter rule set, and takes every rule version through simulation and approval so the
checks produce findings immediately:

```bash
docker compose run --rm --no-deps -e API_URL=http://api:8000 \
  --entrypoint python api scripts/seed_demo.py
```

It prints the credentials it created. Sign in at http://localhost:8080/login.html.

| Account | Role | Jurisdiction | Why the story needs it |
| --- | --- | --- | --- |
| `admin@example.org` | `admin` | `IN` | Creates the others |
| `ruleauthor@example.org` | `rule_admin` | `IN` | Authors a rule version |
| `ruleapprover@example.org` | `rule_admin` | `IN` | Approves it — the author cannot |
| `inspector@example.org` | `inspector` | `IN-HR-GURUGRAM` | Captures and reviews evidence |
| `reviewer@example.org` | `reviewer` | `IN-HR-GURUGRAM` | Records the decision, issues the report |
| `controller@example.org` | `controller` | `IN-HR` | Opens the case, reads the audit trail |
| `otherstate@example.org` | `inspector` | `IN-PB-LUDHIANA` | Shows jurisdiction isolation live |

Every account uses the same password, printed by the script (`Ganga-Yamuna-2026` by
default; override with `DEMO_PASSWORD`). These are local development credentials and
must not be used in a real deployment.

To start over at any point:

```bash
docker compose run --rm --no-deps migrate python scripts/reset_data.py
docker compose run --rm --no-deps -e API_URL=http://api:8000 \
  --entrypoint python api scripts/seed_demo.py
```

If you would rather show rule approval happening live, seed the accounts only and
approve during the demonstration:

```bash
docker compose run --rm --no-deps -e API_URL=http://api:8000 -e SKIP_RULES=1 \
  --entrypoint python api scripts/seed_demo.py
```

Prepare two photographs of the same package: one deliberately poor (blurred, or with a
bright reflection over the declaration panel) and one clear. A real package works better
than a printout.

## 1. A consumer complains (2 minutes)

Open http://localhost:8080 and go to **Report a problem**. Submit a complaint without
signing in: product name, brand, category "Charged more than the printed price",
a description, and the poor photograph. Tick the privacy acknowledgement.

Show:

- the reference returned, `CMP-2026-000001`;
- the priority and, next to it, **the published reason it was given that priority**. It
  comes from a documented table, not a model.

Then go to **Check a complaint** and enter the reference *without* the email address.
Nothing is revealed. Add the email and the progress appears.

> Say: a reference printed on a receipt is guessable. The contact detail is required as
> well, so a guessed reference alone gives nothing away.

## 2. The officer takes it on (2 minutes)

Sign in as the district inspector. **Complaints** shows the queue ordered by priority
with the reason visible.

Open the complaint, record a triage note, then **Open inspection**.

Show:

- `INSP-2026-000001` created and linked to the complaint;
- the consumer's photograph carried across as evidence, with a recorded reason for
  accepting its quality — a consumer photograph cannot be retaken to order;
- the package-face checklist.

## 3. Poor evidence is refused, with instructions (2 minutes)

On the inspection, upload the poor photograph as the declaration panel.

It is refused. Show the measured signals: the sharpness number, the glare percentage,
the threshold each was compared against, and the sentence telling the officer what to
do — "strong glare is covering part of the panel, move the light source or tilt the
package".

> Say: this is not a quality score. Each signal is a measurement against a documented
> threshold, and the officer is told what to change.

Now upload the clear photograph. Accepted with 202, and the SHA-256 of the exact bytes
is recorded. Watch the job progress: real percentages and stage names from the worker,
not a spinner.

Try uploading the same file again — refused as a duplicate.

## 4. Machine reading and officer review are separate (3 minutes)

When analysis finishes the readings appear. This is the screen to spend time on.

Select the MRP reading. The region highlights **on the photograph**. Show, side by side:

- **machine state**: located, with the confidence and the verbatim text it matched;
- **review state**: pending.

> Say: these are two different records. The machine observed something. Nobody has
> decided anything yet.

Toggle the raw OCR view to show exactly what was read, word boxes included.

Now find the unit sale price, which is wrong on the package. Choose **Correct**, type the
printed value, and give a reason.

Show that the machine reading is still there, beside the correction, and that a revision
entry was written. Nothing was overwritten.

Confirm the remaining readings.

## 5. Rules are governed, not hard-coded (2 minutes)

In another browser profile, sign in as the **rule author**. Go to **Rules**.

Show:

- the starter set, all drafts;
- one rule version's detail: its citation, its interpretation note, and prominently
  **"not confirmed by a statutory authority"**;
- the approval blockers listed explicitly.

Try to approve it as the author. Refused: the author cannot approve their own version.

Run the simulator. Show the mandatory scenarios and their pass or fail, including the
boundary cases and the not-yet-effective date case.

Sign in as the **rule approver** and approve it.

> Say: the engine is tested and sound. The citations are not yet verified against the
> gazette, and the system says so on every finding rather than hiding it.

## 6. The checks run against reviewed values only (1 minute)

Back as the inspector, run the checks.

Show the findings. For the unit sale price:

- outcome non-compliant;
- expected against observed;
- **the arithmetic, step by step**: the printed price, the net quantity in base units,
  the division, the rounding rule, the difference, the tolerance;
- the rule version cited, with its effective date and its unconfirmed status.

If you have time, run the checks *before* reviewing on a second inspection to show that
unreviewed readings produce "unable to determine", never a violation.

## 7. The decision is a person's, and it is blocked until review is done (1 minute)

As the inspector, try to record the decision. Refused: an inspector does not decide.

Send it for reviewer decision. Sign in as the **state controller** — note that a
controller at `IN-HR` can read a district inspection beneath it.

Try a two-word reason. Refused. Give a real one and record the decision.

If you added the Punjab inspector, sign in as them and open the same inspection URL: 404,
and their register is empty.

## 8. The report is frozen and verifiable (1 minute)

Issue the report. Show:

- `RPT-2026-000001` and its content hash;
- the PDF, with the findings, the calculations, the evidence hashes, the citation, the
  unconfirmed-authority warning, page numbers and the content hash in the footer;
- the DOCX, the same content in editable form;
- that the inspection is now frozen — try to change a package face and it is refused.

Open the public verification page and enter the reference. It confirms the report exists,
when it was issued, by which workspace, and that its content is unchanged. It does **not**
reveal the product, the premises, the officer or the findings.

## 9. Case, notice, and the audit chain (1 minute)

Open a case from the report. Show the provisions relied on, collected from the cited
rules.

Prepare a notice. Show the rendered text with no unfilled placeholders. Issue it. It is
now frozen — the served text cannot change even if the template does.

Finally, **Audit**. Show the trail, then press **Verify chain**: every event replayed,
the chain intact.

If you want the strongest single moment, do this:

```bash
docker compose exec db psql -U maanak -d maanak \
  -c "UPDATE audit_events SET reason = 'tampered' WHERE sequence = 1;"
```

The database refuses it: `audit_events is append-only`.

## What to say about limits

Do not skip this. It is the difference between a demonstration and a claim.

- No OCR accuracy figure is claimed, because no labelled corpus of real packages was
  available to measure one.
- No rule citation has been verified against a gazette notification. Every one is flagged.
- Net quantity is never verified — that needs weighing, and every report says so.
- The report signature is a clearly labelled development signature, not a real one.
- `docs/KNOWN_LIMITS.md` lists everything else.

## If something fails live

```bash
curl -fsS http://localhost:8000/health/ready     # which component is down
docker compose logs --tail 50 worker             # OCR problems
docker compose logs --tail 50 api                # request errors, with request_id
docker compose run --rm --no-deps migrate python scripts/reset_data.py   # start over
```

Every error shown to a user carries a `request_id` that appears in the logs and on the
audit event, so you can trace what happened rather than guess.
