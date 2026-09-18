"""Verify the evidence pipeline end to end against the running stack.

Generates label images with PIL, uploads them, waits for the worker to read them,
and checks that candidates were created with image regions.

    docker compose run --rm --no-deps -e API_URL=http://api:8000 \
        --entrypoint python api scripts/verify_pipeline.py
"""

from __future__ import annotations

import io
import os
import sys
import time
import uuid
from datetime import date

import httpx
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from session_client import RefreshingClient

BASE_URL = os.environ.get("API_URL", "http://api:8000")
PASSWORD = "Ganga-Yamuna-River-88"

failures: list[str] = []
count = 0


def check(condition: bool, description: str) -> None:
    global count
    count += 1
    if condition:
        print(f"  ok    {description}")
    else:
        failures.append(description)
        print(f"  FAIL  {description}")


def font(size: int, *, devanagari: bool = False) -> ImageFont.FreeTypeFont:
    """Pick a font appropriate to the script.

    Real packaging sets Latin text in a Latin face and Devanagari in a Devanagari
    face. Rendering Latin through a Devanagari font produces glyph shapes no camera
    would ever see, which would make this a test of the wrong thing.
    """
    candidates = (
        [
            "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
            "/usr/share/fonts/truetype/noto/NotoSerifDevanagari-Regular.ttf",
        ]
        if devanagari
        else [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        ]
    )
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def has_devanagari(text: str) -> bool:
    return any("\u0900" <= character <= "\u097f" for character in text)


def make_label(*, good: bool = True) -> bytes:
    """Render a synthetic declaration panel.

    Clearly labelled as synthetic test data. It exists to exercise the pipeline
    deterministically; it is not a claim about accuracy on real packaging, which is
    what scripts/ocr_benchmark.py measures.
    """
    width, height = (1700, 1150) if good else (700, 470)
    image = Image.new("RGB", (width, height), (252, 251, 248))
    draw = ImageDraw.Draw(image)
    scale = width / 1700

    lines = [
        ("SUNRISE ATTA  (SAMPLE DATA)", 62),
        ("Common Name: Whole Wheat Flour", 40),
        ("शुद्ध मात्रा / Net Quantity: 1 kg", 46),
        ("M.R.P. Rs. 58.00 (incl. of all taxes)", 50),
        ("Unit Sale Price: Rs 5.80 per 100 g", 38),
        ("Mfd. by: Sunrise Foods Private Limited", 36),
        ("Plot 12, Industrial Area, Sample City 122001", 32),
        ("Consumer care: care@example.org  1800 200 1234", 32),
        ("Country of Origin: India", 36),
        ("Batch No: SA2026C     Mfg Date: 03/2026", 34),
    ]

    y = int(40 * scale)
    for text, size in lines:
        # Mixed-script lines are split so each run uses its own face, which is how
        # a real bilingual panel is typeset.
        cursor_x = int(40 * scale)
        for run in text.split(" / "):
            chosen = font(int(size * scale), devanagari=has_devanagari(run))
            draw.text((cursor_x, y), run, fill=(18, 18, 20), font=chosen)
            cursor_x += int(draw.textlength(run, font=chosen)) + int(18 * scale)
            if run is not text.split(" / ")[-1]:
                separator = font(int(size * scale))
                draw.text((cursor_x, y), "/", fill=(18, 18, 20), font=separator)
                cursor_x += int(draw.textlength("/", font=separator)) + int(18 * scale)
        y += int((size + 26) * scale)

    buffer = io.BytesIO()
    if good:
        image.save(buffer, format="JPEG", quality=95)
    else:
        # Deliberately unusable: heavy blur plus a large specular highlight.
        image = image.filter(ImageFilter.GaussianBlur(radius=7))
        overlay = ImageDraw.Draw(image)
        overlay.ellipse([90, 90, 520, 330], fill=(255, 255, 255))
        image.save(buffer, format="JPEG", quality=55)
    return buffer.getvalue()


def csrf(client: httpx.Client) -> dict[str, str]:
    token = client.cookies.get("maanak_csrf")
    return {"X-CSRF-Token": token} if token else {}


def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    admin_email = f"admin.{suffix}@example.org"

    with RefreshingClient(base_url=BASE_URL, timeout=120) as client:
        print("setup")
        status = client.get("/api/v1/auth/status").json()
        if not status.get("initialised"):
            response = client.post(
                "/api/v1/auth/bootstrap",
                json={
                    "email": admin_email,
                    "name": "Workspace Administrator",
                    "password": PASSWORD,
                    "jurisdiction_code": "IN",
                    "jurisdiction_name": "National workspace",
                },
            )
            check(response.status_code == 201, f"administrator created ({response.status_code})")
        else:
            admin_email = os.environ.get("ADMIN_EMAIL", admin_email)
            print(f"  info  workspace already initialised, using {admin_email}")

        signed = client.post(
            "/api/v1/auth/sign-in", json={"email": admin_email, "password": PASSWORD}
        )
        if signed.status_code != 200:
            print(f"  FAIL  could not sign in as {admin_email}: {signed.text[:200]}")
            return 1

        officer_email = f"officer.{suffix}@example.org"
        created = client.post(
            "/api/v1/users",
            headers=csrf(client),
            json={
                "email": officer_email,
                "name": "District Inspector",
                "password": PASSWORD,
                "role": "inspector",
                "jurisdiction_code": "IN-HR-GURUGRAM",
                "jurisdiction_name": "Gurugram district",
                "must_change_password": False,
            },
        )
        check(created.status_code == 201, f"inspector account created ({created.status_code})")

    with RefreshingClient(base_url=BASE_URL, timeout=180) as officer:
        officer.post("/api/v1/auth/sign-in", json={"email": officer_email, "password": PASSWORD})
        headers = csrf(officer)

        print("product and inspection")
        response = officer.post(
            "/api/v1/inspections",
            headers=headers,
            json={
                "inspection_date": date.today().isoformat(),
                "source": "retail_store",
                "premises_name": "Sample Retail Store",
                "product": {
                    "brand": "Sunrise",
                    "name": "Atta 1 kg pouch (sample data)",
                    "common_generic_name": "Whole wheat flour",
                    "commodity_category": "cereals_and_pulses",
                    "is_food": True,
                    "package_type": "pouch",
                    "quantity_kind": "weight",
                    "declared_net_quantity": "1",
                    "declared_net_quantity_unit": "kg",
                    "identifiers": [{"scheme": "gtin", "value": "8901030865278"}],
                },
            },
        )
        check(
            response.status_code == 201,
            f"inspection opened ({response.status_code}: {response.text[:200]})",
        )
        if response.status_code != 201:
            return 1
        inspection = response.json()
        inspection_id = inspection["id"]
        check(
            inspection["reference"].startswith("INSP-"),
            f"reference allocated ({inspection['reference']})",
        )
        check(inspection["state"] == "draft", f"opens in draft ({inspection['state']})")
        check(
            inspection["product"]["net_quantity_base"] is not None,
            f"net quantity converted to base units ({inspection['product']['net_quantity_base']})",
        )
        check(
            len(inspection["coverage"]["faces"]) >= 2,
            f"face checklist created ({len(inspection['coverage']['faces'])} faces)",
        )

        print("poor evidence is refused with a specific reason")
        poor = make_label(good=False)
        response = officer.post(
            f"/api/v1/inspections/{inspection_id}/evidence",
            headers=headers,
            data={"face": "declaration_panel"},
            files={"file": ("poor.jpg", poor, "image/jpeg")},
        )
        check(
            response.status_code == 422, f"blurred, glared image refused ({response.status_code})"
        )
        body = response.json()["error"]
        check(
            body["code"] == "evidence_quality_insufficient",
            f"stable quality error code ({body['code']})",
        )
        signals = {item["name"]: item for item in body["details"]["quality"]["signals"]}
        blocking = [name for name, item in signals.items() if item["severity"] == "blocking"]
        check(bool(blocking), f"at least one blocking signal named ({blocking})")
        check(
            any(
                "photograph" in item["message"].lower() or "camera" in item["message"].lower()
                for item in signals.values()
                if item["severity"] == "blocking"
            ),
            "the message tells the officer what to do",
        )
        # skew, text_size and capture_source are legitimately non-numeric when the
        # underlying measurement could not be taken.
        non_numeric = {"text_size", "capture_source", "skew"}
        check(
            all(
                item.get("value") is not None
                for name, item in signals.items()
                if item["severity"] != "ok" and name not in non_numeric
            ),
            "each numeric signal reports the measured value",
        )

        print("override requires a reason")
        response = officer.post(
            f"/api/v1/inspections/{inspection_id}/evidence",
            headers=headers,
            data={"face": "declaration_panel", "quality_override_reason": "too short"},
            files={"file": ("poor.jpg", poor, "image/jpeg")},
        )
        check(response.status_code == 422, f"a token reason is refused ({response.status_code})")

        print("acceptable evidence is stored and queued")
        good = make_label(good=True)
        response = officer.post(
            f"/api/v1/inspections/{inspection_id}/evidence",
            headers=headers,
            data={"face": "declaration_panel"},
            files={"file": ("panel.jpg", good, "image/jpeg")},
        )
        check(
            response.status_code == 202,
            f"accepted for analysis ({response.status_code}: {response.text[:200]})",
        )
        if response.status_code != 202:
            return 1
        upload = response.json()
        evidence_id = upload["evidence"]["id"]
        job_id = upload["job_id"]

        import hashlib

        expected_digest = hashlib.sha256(good).hexdigest()
        check(
            upload["evidence"]["sha256"] == expected_digest,
            f"SHA-256 recorded matches the uploaded bytes ({upload['evidence']['sha256'][:16]}...)",
        )
        check(
            upload["quality"]["verdict"] != "recapture_recommended",
            f"quality accepted ({upload['quality']['verdict']})",
        )
        check(job_id is not None, "an analysis job was created")
        check(upload["queued"], "the job reached the queue")

        print("duplicate upload is refused")
        response = officer.post(
            f"/api/v1/inspections/{inspection_id}/evidence",
            headers=headers,
            data={"face": "back_panel"},
            files={"file": ("panel.jpg", good, "image/jpeg")},
        )
        check(response.status_code == 409, f"identical bytes refused ({response.status_code})")

        print("non-image upload is refused")
        response = officer.post(
            f"/api/v1/inspections/{inspection_id}/evidence",
            headers=headers,
            data={"face": "back_panel"},
            files={"file": ("payload.jpg", b"MZ\x90\x00" + b"A" * 4000, "image/jpeg")},
        )
        check(
            response.status_code == 415,
            f"a file that is not an image is refused despite its declared type ({response.status_code})",
        )

        print("worker progress")
        final = None
        for _ in range(90):
            time.sleep(2)
            job = officer.get(f"/api/v1/jobs/{job_id}").json()
            if job["state"] in {"succeeded", "failed"}:
                final = job
                break
        check(final is not None, "the job finished within 180 seconds")
        if final is None:
            return 1
        check(
            final["state"] == "succeeded",
            f"job succeeded ({final['state']}: {final.get('failure_reason')})",
        )
        check(final["progress"] == 100, f"progress reached 100 ({final['progress']})")
        result = final.get("result") or {}
        check(
            result.get("ocr", {}).get("word_count", 0) > 15,
            f"OCR read the panel ({result.get('ocr', {}).get('word_count')} words)",
        )
        check(
            result.get("candidates_created", 0) > 0,
            f"candidates created ({result.get('candidates_created')})",
        )

        print("candidates and regions")
        detail = officer.get(f"/api/v1/inspections/{inspection_id}").json()
        candidates = {item["declaration_type"]: item for item in detail["candidates"]}
        for declaration in ("mrp", "net_quantity", "country_of_origin", "batch_number"):
            item = candidates.get(declaration)
            check(item is not None, f"{declaration} located")
            if item:
                check(
                    len(item["region"]) == 4 and item["region"][2] > item["region"][0],
                    f"{declaration} carries an image region {item['region']}",
                )
                check(
                    item["region"][2] <= (detail["evidence"][0]["original_width"] or 0),
                    f"{declaration} region is inside the original image bounds",
                )

        mrp = candidates.get("mrp")
        if mrp:
            check(
                mrp["normalised_value"].get("amount") == "58.00",
                f"MRP normalised to 58.00 ({mrp['normalised_value'].get('amount')})",
            )
            check(mrp["review_state"] == "pending", "readings start unreviewed")
            check(
                mrp["machine_state"] == "located",
                f"machine state is located ({mrp['machine_state']})",
            )
            check(mrp["machine_confidence"] is not None, "machine confidence recorded")
            check(bool(mrp["matched_text"]), f"verbatim text kept ({mrp['matched_text']!r})")

        quantity = candidates.get("net_quantity")
        if quantity:
            check(
                quantity["normalised_value"].get("base_amount") == "1000",
                f"1 kg converted to 1000 g ({quantity['normalised_value'].get('base_amount')})",
            )

        print("raw OCR is retained and inspectable")
        ocr = officer.get(f"/api/v1/inspections/{inspection_id}/evidence/{evidence_id}/ocr").json()
        check(ocr["available"], "OCR output is available")
        check(len(ocr["words"]) > 15, f"word boxes retained ({len(ocr['words'])})")
        check(len(ocr["lines"]) > 5, f"lines grouped ({len(ocr['lines'])})")
        check(
            "58.00" in ocr["raw_text"] or "58" in ocr["raw_text"],
            "raw text contains the printed price",
        )
        check(all(len(word["box"]) == 4 for word in ocr["words"]), "every word has a box")

        print("evidence integrity")
        integrity = officer.get(
            f"/api/v1/inspections/{inspection_id}/evidence/{evidence_id}/integrity"
        ).json()
        check(
            integrity["verified"],
            f"stored bytes still match the recorded hash ({integrity['detail']})",
        )

        print("controlled download")
        download = officer.get(
            f"/api/v1/inspections/{inspection_id}/evidence/{evidence_id}/download"
        )
        check(download.status_code == 200, f"original downloadable ({download.status_code})")
        check(
            hashlib.sha256(download.content).hexdigest() == expected_digest,
            "downloaded bytes are identical to what was uploaded",
        )
        check(
            download.headers.get("X-Evidence-SHA256") == expected_digest,
            "the response states the hash",
        )

        print("inspection advanced automatically")
        check(
            detail["state"] == "officer_review",
            f"state moved to officer review once analysis finished ({detail['state']})",
        )

        print("decision is blocked before review is complete")
        response = officer.post(
            f"/api/v1/inspections/{inspection_id}/transitions",
            headers=headers,
            json={
                "target_state": "reviewer_review",
                "expected_version": detail["version"],
            },
        )
        check(
            response.status_code == 409,
            f"cannot advance while readings are unreviewed ({response.status_code})",
        )
        if response.status_code == 409:
            check(
                response.json()["error"]["code"] == "precondition_not_met",
                "stable precondition error code",
            )
            check(
                "confirmed" in response.json()["error"]["message"].lower()
                or "reading" in response.json()["error"]["message"].lower(),
                f"the message says what is outstanding ({response.json()['error']['message'][:80]})",
            )

    print()
    if failures:
        print(f"{len(failures)} of {count} checks FAILED")
        for item in failures:
            print(f"  - {item}")
        return 1
    print(f"all {count} pipeline checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
