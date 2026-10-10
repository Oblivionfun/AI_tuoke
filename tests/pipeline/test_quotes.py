"""报价：新建、生成版本快照、上次报价带出、成交未成交、拒绝联系、AI 拆询价、产品库报价信息、PDF。"""

import asyncio
import json
from decimal import Decimal

import pytest

from workbench.agents.schemas import AgentRole, InquiryItem, InquiryParse
from workbench.pipeline.quotes import Customer, QuoteLine, QuoteSheet, QuoteTerms, parse_lines
from workbench.quote_pdf import render_quote_pdf
from workbench.store.models import Company, CompanyStatus, Product, Quote, QuoteStatus, QuoteVersion, Run, RunStatus

TERMS = QuoteTerms("usd", "FOB Tianjin", 30, "25 days", "30% T/T deposit", "", "")
ALPHA = Customer("Alpha Fasteners", "United Arab Emirates", "info@alpha.ae")


@pytest.fixture
def wb(db, make_wb):
    with db.session() as s:
        s.add(Company(name="Alpha Fasteners", domain="alpha.ae", country="AE", status=CompanyStatus.REPLIED))
        s.add(
            Product(
                id="GW-P06-03",
                category="铆钉",
                name_en="Open-end blind rivet",
                name_zh="开口型抽芯铆钉",
                spec_en="aluminium / steel",
                price_unit="1000 pcs",
                ref_price="6.8",
                cost="5.7",
            )
        )
    wb, _ = make_wb({})
    return wb


def rivets(price: str = "6.8") -> list[QuoteLine]:
    return parse_lines(
        ["GW-P06-03"], ["Open-end blind rivet"], ["4.8 x 12"], [""], ["500"], ["1000 pcs"], [price], ["5.7"]
    )


def test_new_quote_prefills_customer_and_product(wb):
    quote = wb.quotes.new(company_id=1, product_id="GW-P06-03")
    assert quote.id is None and quote.customer_name == "Alpha Fasteners"
    assert quote.customer_country == "United Arab Emirates" and quote.incoterm == "FOB Tianjin"
    line = QuoteLine.model_validate(quote.items[0])
    assert line.description == "Open-end blind rivet" and line.spec == "aluminium / steel"
    assert line.unit == "1000 pcs" and line.unit_price == Decimal("6.8") and line.unit_cost == Decimal("5.7")


def test_generate_snapshots_versions_and_skips_unchanged(wb, db, settings):
    quote_id = wb.quotes.save(None, ALPHA, TERMS, rivets(), user="op")
    assert wb.quotes.generate(quote_id, user="op") == 1
    assert wb.quotes.generate(quote_id, user="op") == 1
    wb.quotes.save(quote_id, ALPHA, TERMS, rivets("6.5"))
    assert wb.quotes.generate(quote_id, user="op") == 2
    with db.session() as s:
        quote = s.get(Quote, quote_id)
        assert quote.status is QuoteStatus.QUOTED and quote.currency == "USD" and quote.number == "GW-Q0001"
        first, second = sorted(quote.versions, key=lambda v: v.version)
        assert first.total == "3400.00" and second.total == "3250.00"
        assert first.snapshot["items"][0]["unit_price"] == "6.8"
        sheet = QuoteSheet.of_version(first, settings.timezone)
        assert round(sheet.margin, 4) == round((3400 - 2850) / 3400, 4) and sheet.uncosted == 0


def test_generate_requires_customer_quantity_and_price(wb):
    lines = parse_lines([""], ["Hex bolt"], ["M12"], [""], [""], ["pcs"], [""], [""])
    quote_id = wb.quotes.save(None, Customer(""), TERMS, lines)
    with pytest.raises(ValueError, match=r"没写客户.*第 1 行没填数量、单价"):
        wb.quotes.generate(quote_id)


def test_last_price_for_customer_is_suggested(wb):
    quote_id = wb.quotes.save(None, ALPHA, TERMS, rivets("6.5"))
    wb.quotes.generate(quote_id)
    [(product, line, last)] = wb.quotes.search_products("铆钉", customer_name="alpha fasteners")
    assert product.id == "GW-P06-03" and last is not None and line.unit_price == Decimal("6.5")
    [(_, other_line, other_last)] = wb.quotes.search_products("rivet", customer_name="Beta Trading")
    assert other_last is None and other_line.unit_price == Decimal("6.8")
    names = [c.name for c in wb.quotes.customers("alpha")]
    assert names == ["Alpha Fasteners"]


def test_outcome_regenerate_and_delete(wb, db):
    quote_id = wb.quotes.save(None, ALPHA, TERMS, rivets())
    wb.quotes.generate(quote_id)
    wb.quotes.set_outcome(quote_id, won=False, reason="价格高")
    with db.session() as s:
        assert s.get(Quote, quote_id).status is QuoteStatus.LOST
    with pytest.raises(ValueError, match="不能删除"):
        wb.quotes.delete(quote_id)
    wb.quotes.save(quote_id, ALPHA, TERMS, rivets("6.2"))
    wb.quotes.generate(quote_id)
    with db.session() as s:
        quote = s.get(Quote, quote_id)
        assert quote.status is QuoteStatus.QUOTED and quote.lost_reason == ""
    draft = wb.quotes.save(None, Customer("Beta"), TERMS, [])
    wb.quotes.delete(draft)
    with db.session() as s:
        assert s.get(Quote, draft) is None


def test_suppressed_company_cannot_be_quoted(wb):
    wb.outreach.suppress(1, reason="对方不需要", user="op")
    with pytest.raises(ValueError, match="拒绝联系"):
        wb.quotes.save(None, Customer("Alpha Fasteners", company_id=1), TERMS, rivets())
    assert wb.quotes.customers("alpha") == []


def test_parse_inquiry_appends_lines_without_prices_from_ai(db, make_wb, wb):
    inquiry = "pls quote 4.8x12 blind rivets 500k pcs and M8 nylon nuts, CIF Jebel Ali"
    parsed = InquiryParse(
        items=[
            InquiryItem(
                product_id="GW-P06-03",
                description="Blind rivet",
                size="4.8 x 12 mm",
                spec="",
                quantity="500000",
                unit="pcs",
                source_text="4.8x12 blind rivets 500k pcs",
            ),
            InquiryItem(
                product_id="",
                description="Nylon lock nut",
                size="M8",
                spec="",
                quantity="",
                unit="pcs",
                source_text="M8 nylon nuts",
            ),
        ],
        incoterm="CIF Jebel Ali",
        questions_zh=["M8 尼龙螺母没写数量"],
    )
    wb, runner = make_wb({AgentRole.PARSER: [parsed]})
    quote_id = wb.quotes.save(None, ALPHA, TERMS, [], inquiry=inquiry)
    run_id = wb.quotes.start_parse(quote_id, user="op")
    assert wb.worker.claim_next() == run_id
    asyncio.run(wb.worker.execute(run_id))
    assert json.loads(runner.calls[0][1])["inquiry"] == inquiry
    with db.session() as s:
        quote = s.get(Quote, quote_id)
        rivet, nut = (QuoteLine.model_validate(i) for i in quote.items)
        assert rivet.product_id == "GW-P06-03" and rivet.quantity == 500 and rivet.unit == "1000 pcs"
        assert rivet.unit_price == Decimal("6.8") and rivet.size == "4.8 x 12 mm"
        assert nut.product_id is None and nut.unit_price is None and nut.quantity is None
        assert quote.incoterm == "CIF Jebel Ali"
        run = s.get(Run, run_id)
        assert run.status is RunStatus.SUCCEEDED and run.params["questions"] == ["M8 尼龙螺母没写数量"]
    assert wb.quotes.parse_run(quote_id).id == run_id


def test_parse_needs_inquiry_text(wb):
    quote_id = wb.quotes.save(None, ALPHA, TERMS, [])
    with pytest.raises(ValueError, match="询价原文"):
        wb.quotes.start_parse(quote_id)


def test_add_product_and_pricing(wb, db):
    assert (
        wb.quotes.add_product(
            category="螺栓",
            name_zh="六角螺栓",
            name_en="Hex bolt",
            spec_en="DIN 933, 8.8",
            price_unit="pcs",
            ref_price="0.02",
            cost="",
        )
        == "GW-M001"
    )
    assert (
        wb.quotes.add_product(
            category="", name_zh="", name_en="Washer", spec_en="", price_unit="", ref_price="", cost=""
        )
        == "GW-M002"
    )
    wb.quotes.set_pricing("GW-P06-03", spec_en="Al/steel", price_unit="1000 pcs", ref_price="7.10", cost="5.9")
    with db.session() as s:
        assert s.get(Product, "GW-M002").category == "其他"
        assert s.get(Product, "GW-P06-03").ref_price == "7.10"
    with pytest.raises(ValueError, match="参考价"):
        wb.quotes.set_pricing("GW-P06-03", spec_en="", price_unit="pcs", ref_price="abc", cost="")
    with pytest.raises(ValueError, match="英文品名"):
        wb.quotes.add_product(category="", name_zh="螺母", name_en="", spec_en="", price_unit="", ref_price="", cost="")


def test_parse_lines_skips_blank_rows_and_reports_bad_numbers():
    parsed = parse_lines(
        ["", "", ""],
        ["Nut", "", "Bolt"],
        ["M8", "", ""],
        ["", "", ""],
        ["20,000", "", "5"],
        ["", "pcs", "kg"],
        ["0.012", "", ""],
        ["", "", ""],
    )
    assert [p.description for p in parsed] == ["Nut", "Bolt"]
    assert parsed[0].quantity == 20000 and parsed[0].unit == "pcs" and parsed[0].amount == Decimal("240.00")
    with pytest.raises(ValueError, match="第 1 行单价"):
        parse_lines([""], ["Nut"], [""], [""], ["1"], ["pcs"], ["abc"], [""])
    with pytest.raises(ValueError, match="第 1 行数量"):
        parse_lines([""], ["Nut"], [""], [""], ["-1"], ["pcs"], ["1"], [""])


def test_pdf_renders_from_snapshot(wb, db, settings):
    quote_id = wb.quotes.save(None, ALPHA, TERMS, rivets())
    wb.quotes.generate(quote_id)
    with db.session() as s:
        version = s.query(QuoteVersion).one()
        pdf = render_quote_pdf(
            "GW-Q0001", 1, version.snapshot, QuoteSheet.of_version(version, settings.timezone), settings
        )
    assert pdf.startswith(b"%PDF") and len(pdf) > 1500
