"""文案与跟进：南非写征求同意信、登记已发送、收件地址的选择。"""

import asyncio
import json

import pytest
from sqlalchemy import select

from tests.factories import dossier, draft
from workbench.agents.schemas import AgentRole
from workbench.pipeline import Decision, DraftAction, pick_email
from workbench.store.models import (
    Channel,
    Company,
    CompanyStatus,
    Direction,
    Draft,
    DraftStatus,
    Interaction,
    Queue,
)


def test_accept_writes_consent_draft_for_za_and_mark_sent(db, settings, make_wb):
    with db.session() as s:
        s.add(
            Company(
                name="Zulu",
                domain="zulu.co.za",
                country="ZA",
                status=CompanyStatus.QUEUED,
                queue=Queue.FIRST_TOUCH,
                dossier=dossier("Zulu", "https://zulu.co.za", "ZA", email="info@zulu.co.za").model_dump(),
            )
        )
    captured = {}

    def writer(spec, text):
        captured.update(json.loads(text))
        return draft(captured["kind"])

    wb, _ = make_wb({AgentRole.WRITER: writer})
    write_run = wb.review.decide(1, Decision.ACCEPT, user="op")
    assert wb.worker.claim_next() == write_run
    asyncio.run(wb.worker.execute(write_run))
    assert captured["kind"] == "consent_request"
    assert captured["signature"]["whatsapp"] == "+86 310 000 0000"
    with db.session() as s:
        d = s.execute(select(Draft)).scalar_one()
        assert d.to_address == "info@zulu.co.za" and d.kind == "consent_request"
    wb.outreach.update_draft(d.id, DraftAction.MARK_SENT, body_en="Edited body", user="op")
    with db.session() as s:
        d = s.get(Draft, d.id)
        assert d.status is DraftStatus.SENT and d.body_en == "Edited body"
        assert s.get(Company, 1).status is CompanyStatus.CONTACTED
        assert s.execute(select(Interaction)).scalar_one().direction is Direction.OUT
    wb.outreach.add_interaction(1, channel=Channel.EMAIL, direction=Direction.IN, summary="同意接收资料", user="op")
    with db.session() as s:
        assert s.get(Company, 1).status is CompanyStatus.REPLIED


@pytest.mark.parametrize(
    ("contacts", "expected"),
    [
        (
            [
                {"kind": "email", "value": "info@x.ae", "label": "通用"},
                {"kind": "email", "value": "purchasing@x.ae", "label": ""},
            ],
            "purchasing@x.ae",
        ),
        (
            [{"kind": "phone", "value": "+971", "label": ""}, {"kind": "email", "value": "sales@x.ae", "label": ""}],
            "sales@x.ae",
        ),
        ([], ""),
    ],
)
def test_pick_email(contacts, expected):
    assert pick_email({"contacts": contacts}) == expected
