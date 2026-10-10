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
    for path in [
        "/today",
        "/companies",
        "/runs",
        "/products",
        "/followups",
        "/tiktok",
        "/quotes",
        "/quotes/new",
        f"/companies/{cid}",
    ]:
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


def test_confirm_sku_and_interaction(client, db):
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
    assert "要报价" in client.get("/followups").text
    assert "新建报价" in client.get(f"/companies/{cid}?tab=followup").text


QUOTE_FORM = {
    "company_id": "",
    "customer_name": "Gulf Build Materials",
    "customer_country": "United Arab Emirates",
    "customer_contact": "",
    "currency": "USD",
    "incoterm": "FOB Tianjin",
    "validity_days": "30",
    "lead_time": "25 days",
    "payment_terms": "30% T/T deposit",
    "packing": "",
    "remarks": "",
    "product_id": ["GW-M001"],
    "description": ["Hex bolt"],
    "size": ["M12 x 60"],
    "spec": ["DIN 933, 8.8"],
    "quantity": ["200"],
    "unit": ["1000 pcs"],
    "unit_price": ["18.5"],
    "unit_cost": ["15"],
}


def test_quote_from_product_to_pdf(client, db):
    login(client)
    r = client.post(
        "/products",
        data={
            "category": "螺栓",
            "name_zh": "六角螺栓",
            "name_en": "Hex bolt",
            "price_unit": "1000 pcs",
            "ref_price": "18.5",
            "cost": "15",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    options = client.get("/quote-products", params={"q": "六角螺栓"}).text
    assert "Hex bolt" in options and "参考价 18.5 / 1000 pcs" in options and "data-add-line" in options
    page = client.get("/quotes/new", params={"product_id": "GW-M001"}).text
    assert "新报价单" in page and 'value="Hex bolt"' in page and 'value="18.5"' in page
    r = client.post("/quotes", data={**QUOTE_FORM, "action": "generate"}, follow_redirects=False)
    assert r.headers["location"] == "/quotes/1?download=1"
    page = client.get("/quotes/1?download=1").text
    assert "已生成第 1 版" in page and "/quotes/1/versions/1.pdf" in page and "已报价" in page
    pdf = client.get("/quotes/1/versions/1.pdf")
    assert pdf.headers["content-type"] == "application/pdf" and pdf.content.startswith(b"%PDF")
    assert "GW-Q0001-1.pdf" in pdf.headers["content-disposition"]
    listing = client.get("/quotes").text
    assert "GW-Q0001" in listing and "Gulf Build Materials" in listing and "USD 3,700.00" in listing
    assert "Gulf Build Materials" in client.get("/quote-customers", params={"customer_name": "gulf"}).text
    bad = client.post("/quotes/1", data={**QUOTE_FORM, "unit_price": ["abc"], "action": "save"}, follow_redirects=False)
    assert bad.headers["location"] == "/quotes/1" and "第 1 行单价" in client.get("/quotes/1").text
    client.post("/quotes/1", data={"action": "lost", "reason": "价格高"})
    with db.session() as s:
        quote = s.get(Quote, 1)
        assert quote.status is QuoteStatus.LOST and quote.lost_reason == "价格高"


def test_quote_from_company_and_parse(client, db):
    login(client)
    cid = alpha_id(db)
    page = client.get("/quotes/new", params={"company_id": cid}).text
    assert 'value="Alpha Fasteners"' in page and "AI 获客档案" in page
    form = {
        **QUOTE_FORM,
        "company_id": str(cid),
        "customer_name": "Alpha Fasteners",
        "inquiry": "pls quote M12 bolts 20k",
    }
    r = client.post("/quotes", data={**form, "action": "parse"}, follow_redirects=False)
    assert r.headers["location"] == "/quotes/1"
    assert "AI 正在拆分" in client.get("/quotes/1").text
    assert client.get("/quotes/1/parse").status_code == 200
    with db.session() as s:
        assert s.get(Quote, 1).inquiry == "pls quote M12 bolts 20k"
        assert s.execute(select(Run).where(Run.kind == RunKind.PARSE)).scalar_one().params == {"quote_id": 1}
    assert "GW-Q0001" in client.get(f"/companies/{cid}?tab=followup").text


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
