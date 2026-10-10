import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.conftest import NOW, ROOT
from tests.factories import dossier, draft, review
from workbench.agents.runner import ScriptedRunner
from workbench.agents.schemas import AgentRole
from workbench.catalog import import_catalog
from workbench.store.models import (
    Company,
    CompanyStatus,
    Draft,
    DraftStatus,
    Product,
    Queue,
    Quote,
    QuoteStatus,
    Rfq,
    Run,
    RunKind,
    RunStatus,
)
from workbench.store.users import upsert_user
from workbench.web.app import create_app

CATALOG = ROOT / "seed" / "guanwei_catalog.sqlite"


@pytest.fixture
def client(db, settings):
    with db.session() as s:
        upsert_user(s, "op", "secret-pass")
        if CATALOG.exists():
            import_catalog(s, CATALOG, NOW)
        s.add(
            Company(
                name="Alpha Fasteners",
                domain="alpha.ae",
                website="https://alpha.ae",
                country="AE",
                status=CompanyStatus.QUEUED,
                queue=Queue.FIRST_TOUCH,
                score=66,
                dossier=dossier("Alpha Fasteners", "https://alpha.ae", email="info@alpha.ae").model_dump(),
                review=review().model_dump(),
            )
        )
    runner = ScriptedRunner({AgentRole.WRITER: lambda spec, text: draft()})
    app = create_app(settings, db, runner=runner, clock=lambda: NOW)
    with TestClient(app, base_url="http://wb.test") as c:
        yield c


def alpha_id(db) -> int:
    with db.session() as s:
        return s.execute(select(Company.id).where(Company.domain == "alpha.ae")).scalar_one()


def login(client):
    r = client.post("/login", data={"username": "op", "password": "secret-pass"}, follow_redirects=False)
    assert r.status_code == 303


def test_requires_login(client):
    r = client.get("/today", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    bad = client.post("/login", data={"username": "op", "password": "wrong"})
    assert bad.status_code == 401 and "不对" in bad.text


def test_pages_render(client, db):
    login(client)
    cid = alpha_id(db)
    for path in ["/today", "/companies", "/runs", "/products", "/followups", "/tiktok", f"/companies/{cid}"]:
        r = client.get(path)
        assert r.status_code == 200, path
    assert "Alpha Fasteners" in client.get("/today").text
    products = client.get("/products").text
    assert "盘头自钻螺钉" in products and "建议优先确认" in products
    assert "info@alpha.ae" in client.get(f"/companies/{cid}").text


def test_review_accept_via_htmx_then_write_and_send(client, db):
    login(client)
    cid = alpha_id(db)
    r = client.post(f"/companies/{cid}/review", data={"decision": "accept"}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and "已接受" in r.text
    with db.session() as s:
        run = s.execute(select(Run).where(Run.kind == RunKind.WRITE)).scalar_one()
    wb = client.app.state.wb
    assert wb.worker.claim_next() == run.id
    asyncio.run(wb.worker.execute(run.id))
    page = client.get(f"/companies/{cid}?tab=drafts").text
    assert "Self-drilling screws from Yongnian" in page and "我已从企业邮箱发出" in page
    with db.session() as s:
        draft_id = s.execute(select(Draft.id)).scalar_one()
    r = client.post(
        f"/drafts/{draft_id}",
        data={"action": "mark_sent", "body_en": "Edited", "subject_en": "Hi"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    with db.session() as s:
        d = s.get(Draft, draft_id)
        assert d.status is DraftStatus.SENT and d.body_en == "Edited"
        assert s.get(Company, cid).status is CompanyStatus.CONTACTED


def test_start_prospect_and_sse_stream(client, db):
    login(client)
    r = client.post(
        "/runs/prospect", data={"country": "AE", "product_group": "自钻螺钉", "count": "5"}, follow_redirects=False
    )
    assert r.status_code == 303
    run_id = int(r.headers["location"].rsplit("/", 1)[1])
    assert "等待 worker" in client.get(f"/runs/{run_id}").text
    wb = client.app.state.wb
    wb.ctx.event(run_id, "search", "[寻客] 搜索：self drilling screws Dubai")
    with db.session() as s:
        s.get(Run, run_id).status = RunStatus.SUCCEEDED
    with client.stream("GET", f"/runs/{run_id}/events") as resp:
        body = "".join(resp.iter_text())
    assert "event: step" in body and "self drilling screws Dubai" in body and "event: done" in body


def test_confirm_sku_and_rfq_and_interaction(client, db):
    if not CATALOG.exists():
        pytest.skip("样册数据库不在仓库里")
    login(client)
    cid = alpha_id(db)
    r = client.post(
        "/skus/GW-P04-04/confirm", data={"standard": "DIN 7504N", "sizes": "ST4.8 × 19"}, follow_redirects=False
    )
    assert r.status_code == 303
    with db.session() as s:
        assert s.get(Product, "GW-P04-04").sales_ready
    client.post(f"/companies/{cid}/interactions", data={"channel": "email", "direction": "in", "summary": "要报价"})
    client.post("/rfqs", data={"company_id": str(cid), "products": "Tek screws ST4.8x19", "quantity": "200k"})
    follow = client.get("/followups").text
    assert "要报价" in follow and "Tek screws ST4.8x19" in follow


def test_quote_from_rfq_edit_print_and_send(client, db):
    login(client)
    cid = alpha_id(db)
    client.post("/rfqs", data={"company_id": str(cid), "products": "Tek screws ST4.8x19", "quantity": "200k"})
    with db.session() as s:
        rfq_id = s.execute(select(Rfq.id)).scalar_one()
    assert "写报价单" in client.get(f"/companies/{cid}?tab=followup").text
    r = client.post(f"/rfqs/{rfq_id}/quote", follow_redirects=False)
    quote_url = r.headers["location"]
    page = client.get(quote_url).text
    assert "GW-Q0001" in page and "Tek screws ST4.8x19" in page and "200000" in page
    form = {
        "currency": "USD",
        "incoterm": "FOB Tianjin",
        "validity_days": "30",
        "lead_time": "25 days",
        "payment_terms": "30% T/T deposit",
        "packing": "",
        "remarks": "",
        "description": ["Self-drilling screw", ""],
        "spec": ["DIN 7504N ST4.8x19", ""],
        "quantity": ["200", ""],
        "unit": ["1000 pcs", "pcs"],
        "unit_price": ["18.5", ""],
        "unit_cost": ["15", ""],
    }
    r = client.post(f"{quote_url}?next=print", data={**form, "action": "save"}, follow_redirects=False)
    assert r.headers["location"] == f"{quote_url}/print"
    sheet = client.get(f"{quote_url}/print").text
    assert "Quotation" in sheet and "USD 3,700.00" in sheet and "United Arab Emirates" in sheet
    assert "15" not in sheet.split("<tbody>")[1].split("</tbody>")[0]
    bad = client.post(quote_url, data={**form, "unit_price": ["abc", ""], "action": "save"}, follow_redirects=False)
    assert bad.headers["location"] == quote_url and "第 1 行单价" in client.get(quote_url).text
    client.post(quote_url, data={**form, "action": "mark_sent", "channel": "email"})
    with db.session() as s:
        assert s.execute(select(Quote.status)).scalar_one() is QuoteStatus.SENT
    follow = client.get("/followups").text
    assert "GW-Q0001" in follow and "已发出，USD 3,700.00" in follow


def test_empty_count_and_double_accept(client, db):
    login(client)
    r = client.post("/runs/prospect", data={"country": "ZA", "count": "", "urls": ""}, follow_redirects=False)
    assert r.status_code == 303
    cid = alpha_id(db)
    client.post(f"/companies/{cid}/review", data={"decision": "accept"})
    r = client.post(f"/companies/{cid}/review", data={"decision": "accept"})
    assert r.status_code == 200 and "已经处理过" in r.text
    with db.session() as s:
        assert len(s.execute(select(Run).where(Run.kind == RunKind.WRITE)).scalars().all()) == 1


def test_cross_site_post_rejected(client, db):
    login(client)
    r = client.post(
        f"/companies/{alpha_id(db)}/suppress", data={"reason": "x"}, headers={"Origin": "https://evil.example"}
    )
    assert r.status_code == 403


def test_suppress_from_page(client, db):
    login(client)
    cid = alpha_id(db)
    client.post(f"/companies/{cid}/suppress", data={"reason": "对方不需要"}, headers={"Origin": "http://wb.test"})
    with db.session() as s:
        assert s.get(Company, cid).status is CompanyStatus.ARCHIVED


def test_htmx_decision_moves_to_next_card_with_toast(client, db):
    with db.session() as s:
        s.add(
            Company(
                name="Beta Trading",
                domain="beta.ae",
                website="https://beta.ae",
                country="AE",
                status=CompanyStatus.QUEUED,
                queue=Queue.FIRST_TOUCH,
                score=50,
                dossier=dossier("Beta Trading", "https://beta.ae").model_dump(),
            )
        )
    login(client)
    first = alpha_id(db)
    r = client.post(
        f"/companies/{first}/review", data={"decision": "unfit", "note": "同行制造商"}, headers={"HX-Request": "true"}
    )
    assert r.status_code == 200
    assert 'id="card-title" translate="no">Beta Trading' in r.text
    assert "toast" in r.headers["HX-Trigger"]
    with db.session() as s:
        beta = s.execute(select(Company.id).where(Company.domain == "beta.ae")).scalar_one()
    assert r.headers["HX-Push-Url"] == f"/today?focus={beta}"


def test_company_stages_and_product_drawer(client, db):
    login(client)
    assert "Alpha Fasteners" in client.get("/companies?stage=review").text
    assert "Alpha Fasteners" not in client.get("/companies?stage=archived").text
    if CATALOG.exists():
        drawer = client.get("/products/GW-P04-04/form").text
        assert "工厂已确认" in drawer and "建议优先确认" in drawer
