"""导入冠维样册数据库（guanwei_catalog.sqlite），以及工厂确认首推 SKU。"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from workbench.normalize import normalize_country, normalize_domain
from workbench.store.models import Company, Product, SkuVariant

# 建议工厂优先确认的 10 个 SKU：避开南非反倾销调查的六角类，覆盖阿联酋种子客户在售的品类。
SUGGESTED_FIRST_SKUS: dict[str, str] = {
    "GW-P04-04": "钢结构、屋面通用，阿联酋和南非都可推",
    "GW-P04-01": "轻钢龙骨、薄板连接",
    "GW-P04-02": "彩钢屋面配套",
    "GW-P05-01": "干墙装修，批发商走量品",
    "GW-P05-02": "木作、家具",
    "GW-P06-03": "铆钉类走量品，种子客户在售",
    "GW-P09-12": "拉铆螺母，种子客户在售",
    "GW-P08-04": "自钻螺钉配套垫圈",
    "GW-P08-03": "通用配套件",
    "GW-P03-01": "建筑锚固，南非首推品类",
}


@dataclass
class ImportReport:
    products_added: int = 0
    products_updated: int = 0
    companies_added: int = 0
    companies_skipped: int = 0


def _main_name(raw: str) -> str:
    return raw.split("（", 1)[0].split("(", 1)[0].strip() or raw.strip()


def import_catalog(session: Session, path: Path, now: datetime) -> ImportReport:
    report = ImportReport()
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    try:
        for row in src.execute("SELECT * FROM products ORDER BY product_id"):
            product = session.get(Product, row["product_id"])
            if product is None:
                product = Product(id=row["product_id"], created_at=now)
                session.add(product)
                report.products_added += 1
            else:
                report.products_updated += 1
            product.category = row["category"]
            product.name_en = row["name_en_normalized"] or row["name_en_raw"]
            product.name_zh = row["name_zh"] or ""
            product.material_declared = row["material_declared"] or ""
            product.source_page = row["source_page"]
            product.source_issue = row["source_issue"] or ""
            product.duplicate_group = row["possible_duplicate_group"] or ""

        evidence: dict[str, list[sqlite3.Row]] = {}
        for ev in src.execute("SELECT * FROM lead_evidence ORDER BY evidence_id"):
            evidence.setdefault(ev["lead_id"], []).append(ev)
        for lead in src.execute("SELECT * FROM lead_candidates ORDER BY lead_id"):
            domain = normalize_domain(lead["website"])
            if not domain or session.scalar(select(Company.id).where(Company.domain == domain)) is not None:
                report.companies_skipped += 1
                continue
            details = json.loads(lead["details_json"])
            items = evidence.get(lead["lead_id"], [])
            session.add(
                Company(
                    name=_main_name(lead["company_name"]),
                    domain=domain,
                    website=lead["website"],
                    country=normalize_country(lead["country"]) or "",
                    source="catalog",
                    prospect_evidence={
                        "name": lead["company_name"],
                        "website": lead["website"],
                        "country": normalize_country(lead["country"]) or "",
                        "city": details.get("city") or "未找到",
                        "company_type": details.get("buyer_role") or "未找到",
                        "criteria_met": [f"{e['evidence_summary']}（{e['access_status']}）" for e in items],
                        "sources": [e["source_url"] for e in items],
                        "why": details.get("fit_reason", ""),
                        "catalog_lead": {
                            "lead_id": lead["lead_id"],
                            "review_status": lead["review_status"],
                            "contact_hint": details.get("contact"),
                            "uncertainties": details.get("uncertainties", []),
                        },
                    },
                    created_at=now,
                    updated_at=now,
                )
            )
            report.companies_added += 1
        session.flush()
    finally:
        src.close()
    return report


def confirm_sku(
    session: Session,
    product_id: str,
    *,
    standard: str = "",
    material: str = "",
    sizes: str = "",
    finish: str = "",
    moq: str = "",
    lead_time: str = "",
    note: str = "",
    user: str,
    now: datetime,
) -> SkuVariant:
    """工厂确认一个首推 SKU 的规格。确认后进入知识包，文案可以引用。"""
    product = session.get(Product, product_id)
    if product is None:
        raise ValueError(f"产品 {product_id} 不存在")
    variant = product.variants[0] if product.variants else SkuVariant(product=product)
    variant.standard = standard.strip()
    variant.material = material.strip()
    variant.sizes = sizes.strip()
    variant.finish = finish.strip()
    variant.moq = moq.strip()
    variant.lead_time = lead_time.strip()
    variant.note = note.strip()
    variant.confirmed_by = user
    variant.confirmed_at = now
    product.sales_ready = True
    session.add(variant)
    session.flush()
    return variant


def unconfirm_sku(session: Session, product_id: str) -> None:
    product = session.get(Product, product_id)
    if product is None:
        raise ValueError(f"产品 {product_id} 不存在")
    product.sales_ready = False
    for variant in product.variants:
        variant.confirmed_at = None
