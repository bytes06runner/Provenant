"""Evidence pack PDF for a recourse case (CLAUDE.md section 4.7).

Everything a dispute reviewer needs, each item traceable to a hash: the signed mandate, the
merchant's signed claims for the purchased item and the signature check, the established facts,
the counterfactual table and fault shares, the remedy and its PayPal ids, and the Flight Recorder
anchors (the custom_id PayPal holds, and both chain heads).
"""

from __future__ import annotations

import io
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


def _esc(x: Any) -> str:
    return str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_evidence_pack(case: dict[str, Any]) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=LETTER,
        leftMargin=0.8 * inch,
        rightMargin=0.8 * inch,
        topMargin=0.8 * inch,
        bottomMargin=0.8 * inch,
        title=f"Provenant evidence pack {case['case_id']}",
        author="Provenant",
    )
    ss = getSampleStyleSheet()
    h1, h2, body = ss["Title"], ss["Heading2"], ss["BodyText"]
    mono = ParagraphStyle("mono", parent=body, fontName="Courier", fontSize=8, leading=10)
    grid = TableStyle(
        [
            ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EAE4D8")),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]
    )

    def kv(rows: list[tuple[str, Any]]) -> Table:
        t = Table(
            [[k, Paragraph(_esc(v), mono)] for k, v in rows], colWidths=[1.8 * inch, 5.0 * inch]
        )
        t.setStyle(grid)
        return t

    story: list[Any] = [
        Paragraph(f"Evidence pack: case {_esc(case['case_id'])}", h1),
        Paragraph(f"Order {_esc(case['order_id'])} at merchant {_esc(case['merchant_id'])}", body),
        Spacer(1, 8),
        Paragraph("1. Binding between the money and the decision trace", h2),
        kv(
            [
                ("PayPal custom_id", case["custom_id"]),
                ("Purchase session", case["purchase_session"]),
                ("Purchase chain head", case["purchase_chain_head"]),
                ("Case chain head", case.get("case_chain_head", "(written after this pack)")),
            ]
        ),
        Paragraph("2. The user's signed mandate", h2),
        kv(
            [
                ("Mandate hash", case["mandate"]["hash"]),
                ("Signing key id", case["mandate"]["key_id"]),
                ("Requirements", case["mandate"]["requirements"]),
                ("Clarified intent (signed)", case["mandate"]["clarified"]),
            ]
        ),
        Paragraph("3. The merchant's signed claims for the purchased item", h2),
        kv(
            [
                ("Manifest hash", case["merchant"]["manifest_hash"]),
                ("Merchant key id", case["merchant"]["key_id"]),
                ("Signature", case["merchant"]["signature_check"]),
                ("Item", case["merchant"]["sku"]),
                ("Signed attributes", case["merchant"]["signed_attributes"]),
            ]
        ),
        Paragraph("4. Established facts", h2),
        kv([(k, v) for k, v in case["facts"].items()]),
        Paragraph("5. Counterfactual replay and fault attribution", h2),
    ]
    att = case.get("attribution")
    if att:
        rows = [["Intervention", "P(bad purchase)"]] + [[k, v] for k, v in att["v"].items()]
        t = Table(rows, colWidths=[2.4 * inch, 2.0 * inch])
        t.setStyle(grid)
        story += [t, Spacer(1, 6)]
        rows = [["Party", "Fault share", f"{att['ci_level']} interval"]]
        names = {"U": "User", "M": "Merchant", "A": "Agent"}
        for p in ("U", "M", "A"):
            share = (att["shares"] or {}).get(p, "n/a")
            lo, hi = att["ci"][p]
            rows.append([names[p], share, f"{lo} to {hi}"])
        t = Table(rows, colWidths=[1.6 * inch, 1.6 * inch, 2.4 * inch])
        t.setStyle(grid)
        story += [
            t,
            Paragraph(_esc(f"k = {att['k']} samples per coalition. {att['reason']}"), body),
        ]
    else:
        story.append(Paragraph(_esc(case.get("attribution_note", "Not replayed.")), body))
    story += [Paragraph("6. Remedy", h2), kv([(k, v) for k, v in case["remedy"].items()])]
    if case.get("ruling"):
        r = case["ruling"]
        story += [Paragraph("7. Ruling", h2), Paragraph(_esc(r["heading"]), body)]
        story += [Paragraph(_esc(f"{i}. {f}"), body) for i, f in enumerate(r["findings"], 1)]
        story += [Paragraph(_esc(r["holding"]), body), Paragraph(_esc(r["remedy"]), body)]
    doc.build(story)
    return buf.getvalue()
