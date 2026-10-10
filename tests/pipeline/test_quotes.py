"""报价单：从询价开报价、发出后锁定、改价出新版本、对方结果、拒绝联系的公司不能报价。"""

from decimal import Decimal

import pytest
from sqlalchemy import select

from tests.conftest import NOW
from workbench.pipeline.quotes import QuoteLine, QuoteSheet, QuoteTerms, guess_quantity, parse_lines
from workbench.store.models import (
    Channel,
    Company,
    CompanyStatus,
    Direction,
    Interaction,
    Quote,
    QuoteStatus,
    Rfq,
    RfqStatus,
)

TERMS = QuoteTerms("usd", "FOB Tianjin", 30, "25-30 days after deposit", "30% T/T deposit", "Cartons on pallets", "")


@pytest.fixture
def wb(db, make_wb):
    with db.session() as s:
        s.add(Company(name="Alpha", domain="alpha.ae", country="AE", status=CompanyStatus.REPLIED))
    wb, _ = make_wb({})
    wb.outreach.add_rfq(1, products="Tek screws ST4.8x19", quantity="200k", incoterm="CIF Jebel Ali", user="op")
    return wb


def test_open_for_prefills_from_rfq_and_reuses_draft(wb, db):
    quote_id = wb.quotes.open_for(1, user="op")
    assert wb.quotes.open_for(1, user="op") == quote_id
    with db.session() as s:
        quote = s.get(Quote, quote_id)
        assert quote.number == "GW-Q0001" and quote.status is QuoteStatus.DRAFT
        assert quote.incoterm == "CIF Jebel Ali" and quote.validity_days == 30
        line = QuoteLine.model_validate(quote.items[0])
        assert line.description == "Tek screws ST4.8x19" and line.quantity == 200000 and line.unit_price is None


def test_send_locks_version_and_records_interaction(wb, db, settings):
    quote_id = wb.quotes.open_for(1, user="op")
    with pytest.raises(ValueError, match="单价"):
        wb.quotes.mark_sent(quote_id, channel=Channel.WHATSAPP, user="op")
    wb.quotes.save(quote_id, TERMS, parse_lines(["Screw"], [""], ["200"], ["1000 pcs"], ["18.5"], ["15"]))
    with db.session() as s:
        sheet = QuoteSheet.of(s.get(Quote, quote_id), settings.timezone, NOW)
    assert sheet.total == Decimal("3700.00") and round(sheet.margin, 4) == round(3.5 / 18.5, 4)
    wb.quotes.mark_sent(quote_id, channel=Channel.WHATSAPP, user="op")
    with db.session() as s:
        quote = s.get(Quote, quote_id)
        assert quote.status is QuoteStatus.SENT and quote.currency == "USD"
        assert s.get(Rfq, 1).status is RfqStatus.QUOTED
        note = s.execute(select(Interaction)).scalar_one()
        assert note.direction is Direction.OUT and note.channel is Channel.WHATSAPP
        assert "GW-Q0001" in note.summary and "USD 3,700.00" in note.summary
    with pytest.raises(ValueError, match="不能再改"):
        wb.quotes.save(quote_id, TERMS, [])
    with pytest.raises(ValueError, match="不能删除"):
        wb.quotes.discard(quote_id)


def test_revise_supersedes_previous_version(wb, db):
    first = wb.quotes.open_for(1, user="op")
    wb.quotes.save(first, TERMS, parse_lines(["Screw"], [""], ["200"], ["1000 pcs"], ["18.5"], [""]))
    wb.quotes.mark_sent(first, channel=Channel.EMAIL, user="op")
    second = wb.quotes.revise(first, user="op")
    assert wb.quotes.revise(first, user="op") == second
    assert wb.quotes.open_for(1) == second
    with db.session() as s:
        draft = s.get(Quote, second)
        assert draft.number == "GW-Q0001-R2" and draft.items == s.get(Quote, first).items
    wb.quotes.save(second, TERMS, parse_lines(["Screw"], [""], ["200"], ["1000 pcs"], ["17.9"], [""]))
    wb.quotes.mark_sent(second, channel=Channel.EMAIL, user="op")
    with db.session() as s:
        assert s.get(Quote, first).status is QuoteStatus.SUPERSEDED
        assert s.get(Quote, second).status is QuoteStatus.SENT


def test_outcome_updates_rfq(wb, db):
    quote_id = wb.quotes.open_for(1, user="op")
    wb.quotes.save(quote_id, TERMS, parse_lines(["Screw"], [""], ["200"], ["1000 pcs"], ["18.5"], [""]))
    with pytest.raises(ValueError, match="已发出"):
        wb.quotes.record_outcome(quote_id, accepted=True)
    wb.quotes.mark_sent(quote_id, channel=Channel.EMAIL, user="op")
    with pytest.raises(ValueError, match="原因"):
        wb.quotes.record_outcome(quote_id, accepted=False)
    wb.quotes.record_outcome(quote_id, accepted=False, note="比现有供应商贵 8%")
    with db.session() as s:
        assert s.get(Quote, quote_id).outcome_note == "比现有供应商贵 8%"
        assert s.get(Rfq, 1).status is RfqStatus.LOST
    revised = wb.quotes.revise(quote_id, user="op")
    wb.quotes.mark_sent(revised, channel=Channel.EMAIL, user="op")
    wb.quotes.record_outcome(revised, accepted=True)
    with db.session() as s:
        assert s.get(Rfq, 1).status is RfqStatus.WON


def test_suppressed_company_cannot_be_quoted(wb):
    wb.outreach.suppress(1, reason="对方不需要", user="op")
    with pytest.raises(ValueError, match="拒绝联系"):
        wb.quotes.open_for(1, user="op")


def test_discard_draft(wb, db):
    quote_id = wb.quotes.open_for(1, user="op")
    assert wb.quotes.discard(quote_id) == 1
    with db.session() as s:
        assert s.get(Quote, quote_id) is None


def test_parse_lines_skips_blank_rows_and_reports_bad_numbers():
    parsed = parse_lines(
        ["Nut", "", "Bolt"], ["M8", "", ""], ["20,000", "", "5"], ["", "pcs", "kg"], ["0.012", "", ""], ["", "", ""]
    )
    assert [p.description for p in parsed] == ["Nut", "Bolt"]
    assert parsed[0].quantity == 20000 and parsed[0].unit == "pcs" and parsed[0].amount == Decimal("240.00")
    assert parsed[1].amount is None
    with pytest.raises(ValueError, match="第 1 行单价"):
        parse_lines(["Nut"], [""], ["1"], ["pcs"], ["abc"], [""])
    with pytest.raises(ValueError, match="负数"):
        parse_lines(["Nut"], [""], ["-1"], ["pcs"], ["1"], [""])


@pytest.mark.parametrize(
    ("text", "expected"),
    [("200k", 200000), ("20,000 pcs", 20000), ("5万", 50000), ("待定", None), ("500k + 200k", None)],
)
def test_guess_quantity(text, expected):
    assert guess_quantity(text) == (Decimal(expected) if expected is not None else None)
