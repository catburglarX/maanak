# Legal sources

## Read this first

**No citation in this system has been verified against a gazette notification.**

Every rule version shipped in the starter set is created as a `draft` with
`legal_authority_confirmed = false` and an `uncertainty_note` recording that its
citation, its quoted requirement and its effective date all require confirmation by a
qualified authority before the version is used in enforcement.

This is not a formality. It is the actual state of the data.

### Why

While building this system the primary rule text could not be retrieved from an
authoritative source programmatically. Three attempts were made:

| Attempt | Result |
| --- | --- |
| Web search for the Rule 6 declaration text | Returned commentary and state-level guidance, not the rule text |
| Web search for the Rule 9 character-height table | Returned unrelated foreign regulations |
| Direct fetch of a state Legal Metrology guideline PDF | The tool could not read `application/pdf` |

Rather than transcribe a citation from memory and present it as verified, every seeded
rule records the citation as *supplied text* and flags it as unconfirmed. The interface
shows that flag on every finding, and the report prints it.

## What *is* verified

The **mechanics** are implemented and tested. What the code does, and what the tests
prove, is independent of whether a citation string is correct:

| Verified | How |
| --- | --- |
| A declaration that is absent from fully captured evidence is non-compliant | `scripts/verify_rules.py` |
| A declaration that could not be read is *not* a violation | `scripts/verify_rules.py`, `scripts/verify_workflow.py` |
| Unit sale price arithmetic is exact and half-up to paise | `tests/test_extraction_values.py` |
| Quantity band boundaries do not overlap (lower inclusive, upper exclusive) | `scripts/verify_rules.py` |
| A rule not yet in force on the inspection date does not apply | `scripts/verify_rules.py` |
| A rule that ended before the inspection date does not apply | `scripts/verify_rules.py` |
| A recorded exception removes the requirement | `scripts/verify_rules.py` |
| Character height is never inferred from pixels | `scripts/verify_rules.py` |
| Only approved or active versions can affect an inspection | `scripts/verify_rules.py`, `scripts/verify_workflow.py` |
| The author of a version cannot approve it | `scripts/verify_workflow.py` |
| Approval is refused until the simulator covers every mandatory case | `scripts/verify_workflow.py` |

So the engine is sound and the interpretations are unconfirmed. Those are separate
claims and this document keeps them separate.

## Sources that should be used

Primary, in order of authority:

| Source | URL | Status |
| --- | --- | --- |
| Department of Consumer Affairs, Legal Metrology Act | https://consumeraffairs.gov.in/pages/legal-metrology-act | Recorded as `source_url` on every seeded rule; **content not transcribed from it** |
| Legal Metrology overview | https://consumeraffairs.gov.in/pages/legal-metrology-overview | Not used |
| DoCA Legal Metrology e-book | https://doca.gov.in/lm-ebook/ | Not used |
| The gazette notification for each amendment | Per amendment | **Not consulted** |
| FSSAI labelling regulations (food articles) | https://fssai.gov.in | **Not consulted**; no FSSAI rule is implemented |
| GS1 General Specifications (check digit) | https://www.gs1.org | The modulo-10 algorithm is implemented from its published definition and verified by hand against known GTINs |

The GS1 check-digit algorithm is the one item here implemented from a specification and
confirmed arithmetically: `tests/test_domain_units.py` works the calculation through by
hand for `890103086527` and asserts the result.

## The starter rule set

Eleven versions, all drafts:

| Code | Subject | Test kind |
| --- | --- | --- |
| `LMPC-DECL-RESPONSIBLE-PARTY` | Manufacturer, packer or importer name and address | `any_declaration_present` |
| `LMPC-DECL-COMMON-NAME` | Common or generic name | `declaration_present` |
| `LMPC-DECL-NET-QUANTITY` | Net quantity declared | `declaration_present` |
| `LMPC-DECL-NET-QUANTITY-UNITS` | Net quantity uses a permitted unit | `net_quantity_unit_permitted` |
| `LMPC-DECL-DATE-MARKING` | Month and year of manufacture, packing or import | `date_marking_completeness` |
| `LMPC-DECL-RETAIL-SALE-PRICE` | Retail sale price declared | `declaration_present` |
| `LMPC-DECL-RETAIL-PRICE-WORDING` | Price carries the prescribed wording | `retail_price_wording` |
| `LMPC-DECL-CONSUMER-CARE` | Consumer care contact | `consumer_care_completeness` |
| `LMPC-DECL-COUNTRY-OF-ORIGIN-IMPORTED` | Country of origin on imported packages | `country_of_origin_required` |
| `LMPC-UNIT-SALE-PRICE-CONSISTENCY` | Unit price agrees with price ÷ quantity | `unit_sale_price_consistency` |
| `LMPC-CHAR-HEIGHT-NET-QUANTITY` | Minimum character height | `character_height_minimum` |

### Values that are explicitly placeholders

| Rule | Placeholder | What must be done |
| --- | --- | --- |
| `LMPC-CHAR-HEIGHT-NET-QUANTITY` | `minimum_height_mm = 1.0` | The prescribed minimum varies with the area of the principal display panel. The applicable table must be transcribed from Rule 9 and entered before approval. |
| `LMPC-DECL-NET-QUANTITY-UNITS` | Permitted unit lists | Drawn from ordinary retail practice, not from the Second Schedule. Must be confirmed and corrected. |
| `LMPC-DECL-RETAIL-PRICE-WORDING` | Accepted phrase list | Wording that is legally acceptable but absent from the list would produce a wrong finding. |
| `LMPC-UNIT-SALE-PRICE-CONSISTENCY` | `per_units = 100`, `tolerance_paise = 1` | The reference quantity and whether a unit price is required at all must be confirmed. The tolerance is an engineering choice and is printed on every finding. |
| All | `effective_from = 2011-04-01` | The commencement date as commonly stated. Must be confirmed, and per-amendment dates entered where a rule was amended. |

### The character-height rule deserves particular attention

A minimum height in millimetres cannot be derived from a photograph. Pixels only become
millimetres once the scale is known, and the same pixel height means different
millimetres at different camera distances.

The check therefore refuses to guess. Without an officer measurement it returns
`additional_evidence_required` and explains why. When a measurement is supplied it is
compared against the threshold *with the stated uncertainty*, and a measurement that
straddles the threshold returns `unable_to_determine` rather than a decision that could
not be defended.

The consequence: an unconfirmed threshold cannot silently produce a violation.

## How to confirm a rule version

1. Retrieve the gazette notification in force on the relevant date.
2. Compare the citation text, the requirement and the effective window with the rule
   version's `citation`, `plain_explanation` and `effective_from`/`effective_to`.
3. Correct anything that differs. A version in `draft` or `changes_required` is
   editable; an approved version is not, and a corrected interpretation becomes a new
   version so the old one remains inspectable.
4. Run the simulator and confirm every mandatory scenario passes.
5. Have a second rule administrator approve it.
6. Record the statutory confirmation separately:
   `POST /api/v1/rules/{id}/legal-authority` with `confirmed: true` and a note naming
   who confirmed it and on what basis.

Steps 4 and 5 are enforced by the software. Steps 1 to 3 and step 6 are human work that
software cannot do, and the system is explicit about not having done them.

## What Maanak does not decide

- It does not determine any penalty, fine or prosecution.
- It does not assert that a package is genuine. A readable barcode is evidence that an
  identifier is printed on a package, nothing more.
- It does not verify net quantity. That requires weighing or measuring the contents, and
  every report says so in its limits section.
- It does not give medical, nutritional or legal advice.
