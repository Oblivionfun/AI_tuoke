"""报价单 PDF：按版本快照生成英文报价单（A4）。用 reportlab 画，不依赖系统字体和浏览器。"""

from functools import lru_cache
from io import BytesIO
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from reportlab.platypus.flowables import Flowable, HRFlowable

from workbench.config import WorkbenchSettings
from workbench.pipeline.quotes import QuoteSheet, money, price_text, quantity_text

GRAPHITE = colors.HexColor("#1D2733")
STEEL = colors.HexColor("#5C6B7A")
LINE = colors.HexColor("#D9E0E7")
HEAD_BG = colors.HexColor("#F3F5F7")
CJK_FONT = "STSong-Light"

BASE = ParagraphStyle("base", fontName="Helvetica", fontSize=9, leading=12, textColor=GRAPHITE)
MUTED = ParagraphStyle("muted", parent=BASE, textColor=STEEL)
BOLD = ParagraphStyle("bold", parent=BASE, fontName="Helvetica-Bold")
RIGHT = ParagraphStyle("right", parent=BASE, alignment=TA_RIGHT)
RIGHT_BOLD = ParagraphStyle("right_bold", parent=RIGHT, fontName="Helvetica-Bold")
SELLER = ParagraphStyle("seller", parent=BOLD, fontSize=13, leading=17)
TITLE = ParagraphStyle("title", parent=RIGHT_BOLD, fontSize=20, leading=24)
HEADING = ParagraphStyle("heading", parent=MUTED, fontName="Helvetica-Bold", fontSize=8, leading=11)


@lru_cache(maxsize=1)
def _cjk_font() -> str:
    pdfmetrics.registerFont(UnicodeCIDFont(CJK_FONT))
    return CJK_FONT


def _p(value: Any, style: ParagraphStyle = BASE) -> Paragraph:
    """Helvetica 只有西文字符；含中文等字符的文字换成 CJK 字体，避免印成方块。"""
    text = str(value or "")
    try:
        text.encode("cp1252")
    except UnicodeEncodeError:
        style = ParagraphStyle(f"{style.name}-cjk", parent=style, fontName=_cjk_font())
    return Paragraph(escape(text).replace("\n", "<br/>"), style)


def _date(value: Any) -> str:
    return value.strftime("%d %b %Y")


def render_quote_pdf(
    number: str, version: int, snapshot: dict[str, Any], sheet: QuoteSheet, settings: WorkbenchSettings
) -> bytes:
    customer, terms = snapshot["customer"], snapshot["terms"]
    currency = terms["currency"]
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
        title=f"Quotation {number}",
        author=settings.seller_company,
    )
    width = doc.width

    contacts = "  |  ".join(
        f"{label}: {value}"
        for label, value in (
            ("Email", settings.seller_email),
            ("WhatsApp", settings.seller_whatsapp),
            ("Web", settings.seller_website),
        )
        if value
    )
    seller = [_p(settings.seller_company, SELLER), _p(settings.seller_address, MUTED)]
    if contacts:
        seller.append(_p(contacts, MUTED))
    meta_rows = [("No.", number), ("Date", _date(sheet.issued)), ("Valid until", _date(sheet.valid_until))]
    if version > 1:
        meta_rows.insert(1, ("Revision", str(version)))
    meta = Table(
        [[_p(label, MUTED), _p(value, RIGHT_BOLD)] for label, value in meta_rows],
        colWidths=[22 * mm, 38 * mm],
        style=[
            ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ("TOPPADDING", (0, 0), (-1, -1), 1),
        ],
    )
    header = Table(
        [[seller, [_p("QUOTATION", TITLE), Spacer(1, 4), meta]]],
        colWidths=[width - 62 * mm, 62 * mm],
        style=[
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ],
    )

    to_block = [_p("To", HEADING), _p(customer["name"], BOLD)]
    to_block += [_p(customer[key]) for key in ("country", "contact") if customer.get(key)]
    term_rows = [
        (label, terms.get(key) or "")
        for label, key in (
            ("Incoterm", "incoterm"),
            ("Currency", "currency"),
            ("Payment", "payment_terms"),
            ("Lead time", "lead_time"),
            ("Packing", "packing"),
        )
        if terms.get(key)
    ]
    terms_table = Table(
        [[_p(label, MUTED), _p(value)] for label, value in term_rows],
        colWidths=[22 * mm, width / 2 - 24 * mm],
        style=[
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ("TOPPADDING", (0, 0), (-1, -1), 1),
        ],
    )
    parties = Table(
        [[to_block, [_p("Terms", HEADING), terms_table]]],
        colWidths=[width / 2, width / 2],
        style=[("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)],
    )

    head = [
        "No.",
        "Description",
        "Specification",
        "Quantity",
        "Unit",
        f"Unit price ({currency})",
        f"Amount ({currency})",
    ]
    rows: list[list[Any]] = [[_p(h, BOLD if i < 3 or i == 4 else RIGHT_BOLD) for i, h in enumerate(head)]]
    for n, line in enumerate(sheet.lines, 1):
        rows.append(
            [
                _p(n),
                _p(line.description),
                _p(line.specification),
                _p(quantity_text(line.quantity), RIGHT),
                line.unit,
                _p(price_text(line.unit_price), RIGHT),
                _p(money(line.amount) if line.amount is not None else "", RIGHT),
            ]
        )
    total_label = f"Total ({terms['incoterm']})" if terms.get("incoterm") else "Total"
    rows.append([_p(total_label, RIGHT_BOLD), "", "", "", "", "", _p(f"{currency} {money(sheet.total)}", RIGHT_BOLD)])
    items = Table(
        rows,
        colWidths=[10 * mm, 38 * mm, 42 * mm, 20 * mm, 19 * mm, 22 * mm, width - 151 * mm],
        repeatRows=1,
        style=TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), HEAD_BG),
                ("LINEBELOW", (0, 0), (-1, 0), 0.8, GRAPHITE),
                ("LINEBELOW", (0, 1), (-1, -3), 0.4, LINE),
                ("LINEABOVE", (0, -1), (-1, -1), 0.8, GRAPHITE),
                ("SPAN", (0, -1), (5, -1)),
                ("FONTSIZE", (4, 1), (4, -2), 9),
                ("TEXTCOLOR", (4, 1), (4, -2), GRAPHITE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        ),
    )

    story: list[Flowable] = [
        header,
        Spacer(1, 6),
        HRFlowable(width="100%", thickness=1.2, color=GRAPHITE),
        Spacer(1, 10),
        parties,
        Spacer(1, 10),
        items,
    ]
    if terms.get("remarks"):
        story += [Spacer(1, 12), _p("Remarks", HEADING), _p(terms["remarks"])]
    signature = [_p("For and on behalf of", MUTED), _p(settings.seller_company, BOLD)]
    if settings.seller_contact_name:
        signature.append(_p(settings.seller_contact_name))
    footer = Table(
        [[_p(f"Prices are in {currency} and valid until {_date(sheet.valid_until)}.", MUTED), signature]],
        colWidths=[width - 75 * mm, 75 * mm],
        style=[
            ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("LINEBELOW", (1, 0), (1, 0), 0.8, GRAPHITE),
        ],
    )
    story += [Spacer(1, 28), footer]
    doc.build(story)
    return buffer.getvalue()
