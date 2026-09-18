"""PDF rendering.

Rendered from a frozen snapshot, never from live tables.

Devanagari: ReportLab's built-in fonts cannot draw Devanagari, so
NotoSansDevanagari is registered from the system font path. If it is missing the
renderer says so in the document rather than silently printing empty boxes where
Hindi text should be, because a report that quietly loses a declaration is worse
than one that admits it.
"""

from __future__ import annotations

import io
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from ...domain import labels as domain_labels
from ...observability import get_logger
from ..phrasing import counted

logger = get_logger(__name__)

RENDERER_VERSION = "1"

# The same palette the browser application uses, so a report does not look like it
# came from a different product. These were warm (ink #141414, cream panel #F6F4EF,
# beige rule #D8D5CE, brick accent #7A2B2B) and the interface had already moved to
# neutral greys.
INK = colors.HexColor("#101820")
GREY = colors.HexColor("#46463F")
RULE = colors.HexColor("#DCDCDC")
ACCENT = colors.HexColor("#750018")
PANEL = colors.HexColor("#F5F5F5")
RED = colors.HexColor("#9A1F1F")
GREEN = colors.HexColor("#1F6B3A")
# Neutral, not mustard. "Unable to determine" and "additional evidence required" mean
# the system does not know yet, and uncertainty should not be coloured like a warning.
SLATE = colors.HexColor("#44464A")

DEVANAGARI_FONT_PATHS = (
    "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSerifDevanagari-Regular.ttf",
)
UNICODE_FONT_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)

_fonts_ready: bool | None = None
_devanagari_available = False


def _register_fonts() -> tuple[bool, bool]:
    """Register the fonts needed for Indian-language text. Idempotent."""
    global _fonts_ready, _devanagari_available
    if _fonts_ready is not None:
        return _fonts_ready, _devanagari_available

    unicode_ok = False
    for path in UNICODE_FONT_PATHS:
        try:
            name = "Body" if "Bold" not in path else "BodyBold"
            pdfmetrics.registerFont(TTFont(name, path))
            unicode_ok = True
        except Exception as exc:  # noqa: BLE001 - fall back to built-in fonts
            logger.debug("pdf_font_unavailable", path=path, error=type(exc).__name__)

    for path in DEVANAGARI_FONT_PATHS:
        try:
            pdfmetrics.registerFont(TTFont("Devanagari", path))
            _devanagari_available = True
            break
        except Exception as exc:  # noqa: BLE001 - reported in the document
            logger.debug("devanagari_font_unavailable", path=path, error=type(exc).__name__)

    if not _devanagari_available:
        logger.warning("pdf_devanagari_font_missing")

    _fonts_ready = unicode_ok
    return _fonts_ready, _devanagari_available


def _has_devanagari(text: str) -> bool:
    return any("\u0900" <= character <= "\u097f" for character in text)


def _styles() -> dict[str, ParagraphStyle]:
    unicode_ok, _ = _register_fonts()
    body_font = "Body" if unicode_ok else "Helvetica"
    bold_font = "BodyBold" if unicode_ok else "Helvetica-Bold"
    sheet = getSampleStyleSheet()

    return {
        "title": ParagraphStyle(
            "MaanakTitle",
            parent=sheet["Title"],
            fontName=bold_font,
            fontSize=19,
            leading=23,
            textColor=INK,
            alignment=TA_LEFT,
            spaceAfter=2,
        ),
        "subtitle": ParagraphStyle(
            "MaanakSubtitle",
            parent=sheet["Normal"],
            fontName=body_font,
            fontSize=9.5,
            leading=13,
            textColor=GREY,
            spaceAfter=10,
        ),
        "heading": ParagraphStyle(
            "MaanakHeading",
            parent=sheet["Heading2"],
            fontName=bold_font,
            fontSize=11.5,
            leading=15,
            textColor=INK,
            spaceBefore=13,
            spaceAfter=5,
        ),
        "body": ParagraphStyle(
            "MaanakBody",
            parent=sheet["Normal"],
            fontName=body_font,
            fontSize=9,
            leading=12.5,
            textColor=INK,
        ),
        "small": ParagraphStyle(
            "MaanakSmall",
            parent=sheet["Normal"],
            fontName=body_font,
            fontSize=7.6,
            leading=10.5,
            textColor=GREY,
        ),
        "mono": ParagraphStyle(
            "MaanakMono",
            parent=sheet["Normal"],
            fontName="Courier",
            fontSize=7.4,
            leading=9.5,
            textColor=INK,
        ),
        "deva": ParagraphStyle(
            "MaanakDeva",
            parent=sheet["Normal"],
            fontName="Devanagari" if _devanagari_available else body_font,
            fontSize=9.5,
            leading=14,
            textColor=INK,
        ),
    }


def _escape(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _paragraph(value: Any, styles: dict[str, ParagraphStyle], key: str = "body") -> Paragraph:
    """Choose a Devanagari-capable style automatically when needed."""
    text = "" if value is None else str(value)
    style = styles["deva"] if _has_devanagari(text) and key == "body" else styles[key]
    return Paragraph(_escape(text) or "&nbsp;", style)


#: Labels come from the domain so the report, the screen and the API agree on the
#: wording of every state and declaration name.
label_for = domain_labels.label_for

OUTCOME_COLOURS = {
    "compliant": GREEN,
    "non_compliant": RED,
    "unable_to_determine": SLATE,
    "additional_evidence_required": SLATE,
    "not_applicable": GREY,
}


def _table(rows: list[list[Any]], widths: list[float], *, header: bool = True) -> Table:
    table = Table(rows, colWidths=widths, repeatRows=1 if header else 0)
    style = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, RULE),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]
    if header:
        style += [
            ("BACKGROUND", (0, 0), (-1, 0), PANEL),
            ("LINEBELOW", (0, 0), (-1, 0), 0.6, INK),
        ]
    table.setStyle(TableStyle(style))
    return table


def render_pdf(snapshot: dict[str, Any], *, report: dict[str, Any]) -> tuple[bytes, int]:
    """Render a report. Returns ``(pdf_bytes, page_count)``."""
    styles = _styles()
    _, devanagari_ok = _register_fonts()

    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=18 * mm,
        title=f"Maanak inspection report {report['reference']}",
        author=report["issuing_workspace"],
        subject="Packaged commodity inspection record",
        # Tagged PDF improves screen-reader handling of the reading order.
        lang="en-IN",
    )

    inspection = snapshot.get("inspection", {})
    product = snapshot.get("product", {})
    people = snapshot.get("people", {})
    rule_versions = snapshot.get("rule_versions", {})
    content_width = document.width

    story: list[Any] = []

    story.append(_paragraph("Packaged commodity inspection report", styles, "title"))
    story.append(
        _paragraph(
            f"Issued by {report['issuing_workspace']} · Report {report['reference']} · "
            f"Revision {report['revision']}",
            styles,
            "subtitle",
        )
    )

    story.append(
        _table(
            [
                [
                    _paragraph("Report reference", styles, "small"),
                    _paragraph("Inspection reference", styles, "small"),
                    _paragraph("Inspection date", styles, "small"),
                    _paragraph("Jurisdiction", styles, "small"),
                ],
                [
                    _paragraph(report["reference"], styles),
                    _paragraph(inspection.get("reference"), styles),
                    _paragraph(inspection.get("inspection_date"), styles),
                    _paragraph(inspection.get("jurisdiction_name"), styles),
                ],
            ],
            [content_width * 0.25] * 4,
        )
    )
    story.append(Spacer(1, 6))

    # --- Decision -------------------------------------------------------
    decision = str(inspection.get("decision") or "not recorded")
    decision_label = label_for(decision) if inspection.get("decision") else "Not recorded"
    decided_by = people.get(str(inspection.get("decided_by_id")), {})
    designation = decided_by.get("designation")
    designation_suffix = f", {designation}" if designation else ""

    decision_table = _table(
        [
            [_paragraph("Recorded decision", styles, "small")],
            [_paragraph(f"<b>{_escape(decision_label)}</b>", styles)],
            [_paragraph(inspection.get("decision_note"), styles)],
            [
                _paragraph(
                    "Recorded by "
                    f"{decided_by.get('name', 'unknown')}"
                    f"{designation_suffix}"
                    f" ({decided_by.get('role', 'unknown role')}) on "
                    f"{inspection.get('decided_at') or 'unknown date'}",
                    styles,
                    "small",
                )
            ],
        ],
        [content_width],
    )
    decision_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), PANEL),
                ("BOX", (0, 0), (-1, -1), 0.6, RULE),
                (
                    "TEXTCOLOR",
                    (0, 1),
                    (0, 1),
                    RED if decision == "violation_found" else INK,
                ),
            ]
        )
    )
    story.append(KeepTogether([decision_table]))

    # --- Product --------------------------------------------------------
    story.append(_paragraph("Product", styles, "heading"))
    identifiers = (
        ", ".join(
            f"{item['scheme'].upper()} {item['value']}"
            + (" (check digit valid)" if item.get("check_digit_valid") else "")
            for item in product.get("identifiers", [])
        )
        or "None recorded"
    )
    quantity = product.get("declared_net_quantity")
    quantity_text = (
        f"{quantity} {product.get('declared_net_quantity_unit') or ''}".strip()
        if quantity
        else "Not recorded"
    )
    story.append(
        _table(
            [
                [
                    _paragraph("Field", styles, "small"),
                    _paragraph("Recorded value", styles, "small"),
                ],
                [_paragraph("Brand", styles), _paragraph(product.get("brand"), styles)],
                [_paragraph("Product", styles), _paragraph(product.get("name"), styles)],
                [
                    _paragraph("Common or generic name", styles),
                    _paragraph(product.get("common_generic_name") or "Not recorded", styles),
                ],
                [
                    _paragraph("Commodity category", styles),
                    _paragraph(
                        label_for(product["commodity_category"])
                        if product.get("commodity_category")
                        else "Not recorded",
                        styles,
                    ),
                ],
                [
                    _paragraph("Package type", styles),
                    _paragraph(
                        label_for(product["package_type"])
                        if product.get("package_type")
                        else "Not recorded",
                        styles,
                    ),
                ],
                [_paragraph("Declared net quantity", styles), _paragraph(quantity_text, styles)],
                [_paragraph("Identifiers", styles), _paragraph(identifiers, styles)],
                [
                    _paragraph("Imported", styles),
                    _paragraph("Yes" if product.get("is_imported") else "No", styles),
                ],
                [
                    _paragraph("Country of origin", styles),
                    _paragraph(product.get("country_of_origin") or "Not recorded", styles),
                ],
            ],
            [content_width * 0.32, content_width * 0.68],
        )
    )

    responsible = product.get("responsible_parties", [])
    if responsible:
        story.append(_paragraph("Responsible parties", styles, "heading"))
        rows: list[list[Any]] = [
            [
                _paragraph("Role", styles, "small"),
                _paragraph("Name", styles, "small"),
                _paragraph("Address", styles, "small"),
            ]
        ]
        for item in responsible:
            address = ", ".join(
                part
                for part in (
                    item.get("address_line"),
                    item.get("locality"),
                    item.get("state"),
                    item.get("pin_code"),
                    item.get("country"),
                )
                if part
            )
            rows.append(
                [
                    _paragraph(label_for(item["party_role"]), styles),
                    _paragraph(item["legal_name"], styles),
                    _paragraph(address or "Not recorded", styles),
                ]
            )
        story.append(
            _table(rows, [content_width * 0.2, content_width * 0.35, content_width * 0.45])
        )

    # --- Evidence -------------------------------------------------------
    story.append(_paragraph("Evidence and chain of custody", styles, "heading"))
    evidence_rows: list[list[Any]] = [
        [
            _paragraph("Package face", styles, "small"),
            _paragraph("Received", styles, "small"),
            _paragraph("Size", styles, "small"),
            _paragraph("SHA-256 of the original file", styles, "small"),
        ]
    ]
    for item in snapshot.get("evidence", []):
        evidence_rows.append(
            [
                _paragraph(label_for(item["face"]), styles),
                _paragraph(str(item.get("server_received_at"))[:19], styles, "small"),
                _paragraph(f"{item['size_bytes'] // 1024} KB", styles, "small"),
                Paragraph(_escape(item["sha256"]), styles["mono"]),
            ]
        )
    if len(evidence_rows) == 1:
        evidence_rows.append([_paragraph("No evidence recorded", styles), "", "", ""])
    story.append(
        _table(
            evidence_rows,
            [content_width * 0.22, content_width * 0.18, content_width * 0.1, content_width * 0.5],
        )
    )

    overrides = [
        item for item in snapshot.get("evidence", []) if item.get("quality_override_reason")
    ]
    if overrides:
        story.append(Spacer(1, 4))
        story.append(
            _paragraph(
                "Image quality was accepted with a recorded reason for "
                f"{counted(len(overrides), 'file')}. "
                + " ".join(
                    f"{label_for(item['face'])}: {item['quality_override_reason']}"
                    for item in overrides
                ),
                styles,
                "small",
            )
        )

    faces_not_captured = [
        item
        for item in snapshot.get("package_faces", [])
        if item["capture_state"] not in {"captured"}
    ]
    if faces_not_captured:
        story.append(Spacer(1, 4))
        story.append(
            _paragraph(
                "Package faces not captured: "
                + "; ".join(
                    f"{label_for(item['face'])} ({label_for(item['capture_state']).lower()}"
                    + (f", {item['reason']}" if item.get("reason") else "")
                    + ")"
                    for item in faces_not_captured
                ),
                styles,
                "small",
            )
        )

    # --- Readings -------------------------------------------------------
    story.append(PageBreak())
    story.append(_paragraph("Declarations read and reviewed", styles, "heading"))
    story.append(
        _paragraph(
            "The machine reading and the officer's review are recorded separately. A "
            "correction never replaces what the machine read.",
            styles,
            "small",
        )
    )
    story.append(Spacer(1, 4))

    reading_rows: list[list[Any]] = [
        [
            _paragraph("Declaration", styles, "small"),
            _paragraph("Machine read", styles, "small"),
            _paragraph("Officer review", styles, "small"),
            _paragraph("Used in tests", styles, "small"),
        ]
    ]
    for item in snapshot.get("readings", []):
        machine = item.get("machine_reading") or {}
        machine_text = str(machine.get("display") or machine.get("value") or "not read")
        corrected = item.get("officer_corrected_value") or {}
        review = label_for(item["review_state"])
        if corrected:
            review += f" to {corrected.get('display') or corrected.get('value')}"
        if item.get("correction_reason"):
            review += f". Reason: {item['correction_reason']}"
        reading_rows.append(
            [
                _paragraph(label_for(item["declaration_type"]), styles),
                _paragraph(
                    f"{machine_text}"
                    + (
                        f" (confidence {item['machine_confidence']:.2f})"
                        if item.get("machine_confidence") is not None
                        else ""
                    ),
                    styles,
                ),
                _paragraph(review, styles),
                _paragraph("Yes" if item.get("used_for_legal_tests") else "No", styles, "small"),
            ]
        )
    if len(reading_rows) == 1:
        reading_rows.append([_paragraph("No declarations were read", styles), "", "", ""])
    story.append(
        _table(
            reading_rows,
            [
                content_width * 0.22,
                content_width * 0.31,
                content_width * 0.35,
                content_width * 0.12,
            ],
        )
    )

    # --- Findings -------------------------------------------------------
    story.append(_paragraph("Findings", styles, "heading"))
    findings = snapshot.get("findings", [])
    if not findings:
        story.append(_paragraph("No applicable rule produced a finding.", styles))
    for index, finding in enumerate(findings, start=1):
        rule = rule_versions.get(finding["rule_version_id"], {})
        outcome = finding["effective_outcome"]
        block: list[Any] = [
            _paragraph(
                f"{index}. {_escape(rule.get('title', 'Rule'))} "
                f"<font color='{OUTCOME_COLOURS.get(outcome, INK).hexval()[2:]}'>"
                f"[{label_for(outcome)}]</font>",
                styles,
            ),
            _paragraph(finding["explanation"], styles),
        ]
        detail_rows = [
            [
                _paragraph("Rule cited", styles, "small"),
                _paragraph(rule.get("citation"), styles, "small"),
            ],
            [
                _paragraph("Rule version", styles, "small"),
                _paragraph(
                    f"{rule.get('code')} version {rule.get('version')}, in force from "
                    f"{rule.get('effective_from')}"
                    + (f" to {rule.get('effective_to')}" if rule.get("effective_to") else ""),
                    styles,
                    "small",
                ),
            ],
            [
                _paragraph("Legal authority", styles, "small"),
                _paragraph(
                    "Confirmed by a qualified authority"
                    if rule.get("legal_authority_confirmed")
                    else "NOT confirmed by a statutory authority. Approved for use in "
                    "this workspace only.",
                    styles,
                    "small",
                ),
            ],
        ]
        if finding.get("expected_value") or finding.get("observed_value"):
            # Only the side that has a value, so a presence test does not print
            # "Expected: not recorded" against a rule that has nothing to expect.
            if finding.get("expected_value"):
                detail_rows.append(
                    [
                        _paragraph("Expected", styles, "small"),
                        _paragraph(finding["expected_value"], styles, "small"),
                    ]
                )
            if finding.get("observed_value"):
                detail_rows.append(
                    [
                        _paragraph("Observed", styles, "small"),
                        _paragraph(finding["observed_value"], styles, "small"),
                    ]
                )
        if finding.get("calculation"):
            detail_rows.append(
                [
                    _paragraph("Calculation", styles, "small"),
                    Paragraph(
                        "<br/>".join(_escape(step) for step in finding["calculation"]),
                        styles["mono"],
                    ),
                ]
            )
        if finding.get("officer_outcome"):
            detail_rows.append(
                [
                    _paragraph("Officer override", styles, "small"),
                    _paragraph(
                        f"Engine returned {label_for(finding['engine_outcome'])}; "
                        f"officer recorded {label_for(finding['officer_outcome'])}. "
                        f"{finding.get('officer_note') or ''}",
                        styles,
                        "small",
                    ),
                ]
            )
        block.append(
            _table(detail_rows, [content_width * 0.22, content_width * 0.78], header=False)
        )
        block.append(Spacer(1, 8))
        story.append(KeepTogether(block))

    # --- Timeline -------------------------------------------------------
    story.append(_paragraph("Timeline", styles, "heading"))
    timeline_rows: list[list[Any]] = [
        [
            _paragraph("When", styles, "small"),
            _paragraph("Change", styles, "small"),
            _paragraph("By", styles, "small"),
            _paragraph("Reason", styles, "small"),
        ]
    ]
    for item in snapshot.get("timeline", []):
        actor = people.get(str(item.get("actor_id")), {})
        timeline_rows.append(
            [
                _paragraph(str(item.get("occurred_at"))[:19], styles, "small"),
                _paragraph(
                    f"{label_for(item['from_state']) if item.get('from_state') else 'Opened'}"
                    f" \u2192 {label_for(item['to_state'])}",
                    styles,
                    "small",
                ),
                _paragraph(
                    actor.get("name") or item.get("actor_role") or "system", styles, "small"
                ),
                _paragraph(item.get("reason") or "None given", styles, "small"),
            ]
        )
    story.append(
        _table(
            timeline_rows,
            [content_width * 0.17, content_width * 0.28, content_width * 0.2, content_width * 0.35],
        )
    )

    # --- Verification and limits ---------------------------------------
    story.append(_paragraph("Verification", styles, "heading"))
    verification_rows = [
        [
            _paragraph("Report content hash", styles, "small"),
            Paragraph(_escape(report["snapshot_sha256"]), styles["mono"]),
        ],
        [
            _paragraph("Hash algorithm", styles, "small"),
            _paragraph(report["snapshot_algorithm"], styles, "small"),
        ],
        [
            _paragraph("Verification code", styles, "small"),
            _paragraph(report["verification_code"], styles),
        ],
        [
            _paragraph("Verify at", styles, "small"),
            _paragraph(report.get("verification_url", ""), styles, "small"),
        ],
        [
            _paragraph("Digital signature", styles, "small"),
            _paragraph(
                {
                    "none": "Not signed. This document carries a content hash only.",
                    "development": "Signed with a DEVELOPMENT key. Not valid for any "
                    "official purpose.",
                }.get(report["signature_status"], report["signature_status"]),
                styles,
                "small",
            ),
        ],
    ]
    story.append(
        _table(verification_rows, [content_width * 0.25, content_width * 0.75], header=False)
    )

    story.append(_paragraph("Limits of this report", styles, "heading"))
    for limit in snapshot.get("limits", []):
        story.append(_paragraph(f"• {limit}", styles, "small"))
        story.append(Spacer(1, 2))

    if not devanagari_ok:
        story.append(Spacer(1, 4))
        story.append(
            _paragraph(
                "Note: the Devanagari font was unavailable when this document was "
                "produced, so Hindi text may not appear correctly. The stored record is "
                "unaffected.",
                styles,
                "small",
            )
        )

    story.append(Spacer(1, 6))
    story.append(
        _paragraph(
            "Maanak is not a government service and this report is not a government "
            "certification. It records an inspection carried out by the workspace named "
            "above.",
            styles,
            "small",
        )
    )

    page_counter = {"count": 0}

    def decorate(canvas: Any, doc: Any) -> None:
        page_counter["count"] = max(page_counter["count"], doc.page)
        canvas.saveState()
        canvas.setStrokeColor(RULE)
        canvas.setLineWidth(0.5)
        canvas.line(
            doc.leftMargin,
            A4[1] - doc.topMargin + 6 * mm,
            A4[0] - doc.rightMargin,
            A4[1] - doc.topMargin + 6 * mm,
        )
        canvas.setFont("Helvetica", 7.2)
        canvas.setFillColor(GREY)
        canvas.drawString(
            doc.leftMargin,
            A4[1] - doc.topMargin + 8 * mm,
            f"Maanak · {report['reference']} · {report['issuing_workspace']}",
        )
        canvas.drawRightString(
            A4[0] - doc.rightMargin,
            12 * mm,
            f"Page {doc.page}",
        )
        canvas.drawString(
            doc.leftMargin,
            12 * mm,
            f"Content hash {report['snapshot_sha256'][:32]}…",
        )
        canvas.restoreState()

    document.build(story, onFirstPage=decorate, onLaterPages=decorate)
    return buffer.getvalue(), page_counter["count"]


def render_verification_qr(url: str) -> bytes:
    """QR code pointing at the public verification page."""
    import qrcode

    code: Any = qrcode.QRCode(box_size=6, border=2)
    code.add_data(url)
    code.make(fit=True)
    image = code.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
