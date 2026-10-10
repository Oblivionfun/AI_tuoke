"""产品库：84 张产品卡按品类浏览，工厂在侧边抽屉里确认首推 SKU 的规格。"""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from workbench.agents.prompts import CATEGORY_ORDER
from workbench.catalog import SUGGESTED_FIRST_SKUS, confirm_sku, unconfirm_sku
from workbench.store.models import Product
from workbench.web.deps import current_user, flash, get_wb, redirect, render

router = APIRouter()


@router.get("/products", response_class=HTMLResponse)
def products(
    request: Request, category: str = "", suggested: bool = False, user: str = Depends(current_user)
) -> HTMLResponse:
    with get_wb(request).db.session() as s:
        rows = s.execute(select(Product).options(selectinload(Product.variants))).scalars().all()
    order = {c: i for i, c in enumerate(CATEGORY_ORDER)}
    rows = sorted(
        rows, key=lambda p: (not p.sales_ready, p.id not in SUGGESTED_FIRST_SKUS, order.get(p.category, 99), p.id)
    )
    categories = sorted({p.category for p in rows}, key=lambda c: order.get(c, 99))
    counts = {c: sum(1 for p in rows if p.category == c) for c in categories}
    shown = [
        p for p in rows if (not category or p.category == category) and (not suggested or p.id in SUGGESTED_FIRST_SKUS)
    ]
    return render(
        request,
        "pages/products.html",
        nav="products",
        rows=shown,
        total=len(rows),
        confirmed=sum(1 for p in rows if p.sales_ready),
        categories=categories,
        counts=counts,
        category=category,
        suggested_only=suggested,
        suggested=SUGGESTED_FIRST_SKUS,
    )


@router.get("/products/{product_id}/form", response_class=HTMLResponse)
def product_form(request: Request, product_id: str, user: str = Depends(current_user)) -> HTMLResponse:
    """确认规格抽屉的内容（htmx 加载）。"""
    with get_wb(request).db.session() as s:
        product = s.execute(
            select(Product).options(selectinload(Product.variants)).where(Product.id == product_id)
        ).scalar_one_or_none()
    if product is None:
        raise HTTPException(404, "产品不存在")
    return render(
        request,
        "partials/sku_form.html",
        p=product,
        v=product.variants[0] if product.variants else None,
        suggested=SUGGESTED_FIRST_SKUS,
    )


@router.post("/skus/{product_id}/confirm")
def confirm(
    request: Request,
    product_id: str,
    standard: str = Form(""),
    material: str = Form(""),
    sizes: str = Form(""),
    finish: str = Form(""),
    moq: str = Form(""),
    lead_time: str = Form(""),
    note: str = Form(""),
    user: str = Depends(current_user),
) -> Response:
    wb = get_wb(request)
    with wb.db.session() as s:
        try:
            confirm_sku(
                s,
                product_id,
                standard=standard,
                material=material,
                sizes=sizes,
                finish=finish,
                moq=moq,
                lead_time=lead_time,
                note=note,
                user=user,
                now=wb.ctx.clock(),
            )
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
    flash(request, f"{product_id} 已确认为首推 SKU，之后的调研和文案会引用这些规格")
    return redirect("/products")


@router.post("/skus/{product_id}/unconfirm")
def unconfirm(request: Request, product_id: str, user: str = Depends(current_user)) -> Response:
    with get_wb(request).db.session() as s:
        unconfirm_sku(s, product_id)
    flash(request, f"{product_id} 已取消首推")
    return redirect("/products")
