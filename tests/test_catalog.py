from pathlib import Path

import pytest
from sqlalchemy import func, select

from tests.conftest import NOW, ROOT
from workbench.agents.prompts import PromptLibrary, products_summary
from workbench.agents.schemas import AgentRole
from workbench.catalog import (
    DEMO_PRICE_NOTE,
    SUGGESTED_FIRST_SKUS,
    apply_demo_prices,
    clear_demo_prices,
    confirm_sku,
    import_catalog,
    is_demo_priced,
    unconfirm_sku,
)
from workbench.config import WorkbenchSettings
from workbench.store.models import Company, Product

CATALOG = ROOT / "seed" / "guanwei_catalog.sqlite"
pytestmark = pytest.mark.skipif(not CATALOG.exists(), reason="冠维样册数据库不在仓库里")


def test_import_catalog_is_idempotent(db):
    with db.session() as s:
        first = import_catalog(s, CATALOG, NOW)
    assert (first.products_added, first.companies_added) == (84, 6)
    with db.session() as s:
        second = import_catalog(s, CATALOG, NOW)
        assert (second.products_added, second.products_updated, second.companies_skipped) == (0, 84, 6)
        assert s.scalar(select(func.count(Product.id))) == 84
        countries = sorted(s.execute(select(Company.country)).scalars())
        assert countries == ["AE", "AE", "SA", "ZA", "ZA", "ZA"]
        zujer = s.execute(select(Company).where(Company.domain == "zujertrading.com")).scalar_one()
        assert zujer.source == "catalog" and zujer.prospect_evidence["sources"]
        kha = s.execute(select(Company).where(Company.domain == "khamisainhardware.com")).scalar_one()
        assert "（" not in kha.name
        assert all(s.get(Product, pid) is not None for pid in SUGGESTED_FIRST_SKUS)


def test_confirmed_sku_enters_knowledge_pack(db, settings: WorkbenchSettings, tmp_path: Path):
    with db.session() as s:
        import_catalog(s, CATALOG, NOW)
        before = products_summary(s)
        assert "暂无" in before
        confirm_sku(
            s,
            "GW-P04-04",
            standard="DIN 7504N",
            sizes="ST4.2-ST6.3 × 13-100mm",
            finish="蓝白锌",
            moq="每规格 50,000 件",
            user="厂长",
            now=NOW,
        )
    with db.session() as s:
        after = products_summary(s)
        assert "[GW-P04-04] Pan head self-drilling screws" in after and "DIN 7504N" in after
        lib = PromptLibrary(settings)
        v_after = lib.build(AgentRole.WRITER, country="AE", products=after).version
        v_before = lib.build(AgentRole.WRITER, country="AE", products=before).version
        assert v_after != v_before
        unconfirm_sku(s, "GW-P04-04")
    with db.session() as s:
        assert "暂无" in products_summary(s)


def test_demo_prices_mark_and_clear(db):
    with db.session() as s:
        import_catalog(s, CATALOG, NOW)
        updated = apply_demo_prices(s)
        assert set(updated) == set(SUGGESTED_FIRST_SKUS)
        rivet = s.get(Product, "GW-P06-03")
        assert rivet is not None
        assert rivet.ref_price == "6.80" and rivet.price_unit == "1000 pcs"
        assert is_demo_priced(rivet) and rivet.variants[0].note == DEMO_PRICE_NOTE
        assert not rivet.sales_ready
    with db.session() as s:
        cleared = clear_demo_prices(s)
        assert "GW-P06-03" in cleared
        rivet = s.get(Product, "GW-P06-03")
        assert rivet is not None and rivet.ref_price == "" and not is_demo_priced(rivet)
