"""产品库：样册的 84 张产品卡和工厂新增的产品。侧边抽屉里填报价信息（英文规格、参考价、成本），确认首推 SKU 的规格。"""

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


@router.get("/products/new/form", response_class=HTMLResponse)
def new_product_form(request: Request, user: str = Depends(current_user)) -> HTMLResponse:
    """新增产品抽屉的内容（htmx 加载）。"""
    return render(request, "partials/product_new.html", categories=CATEGORY_ORDER)


@router.post("/products")
def add_product(
    request: Request,
    category: str = Form(""),
    name_zh: str = Form(""),
    name_en: str = Form(""),
    spec_en: str = Form(""),
    price_unit: str = Form("1000 pcs"),
    ref_price: str = Form(""),
    cost: str = Form(""),
    user: str = Depends(current_user),
) -> Response:
    try:
        product_id = get_wb(request).quotes.add_product(
            category=category,
            name_zh=name_zh,
            name_en=name_en,
            spec_en=spec_en,
            price_unit=price_unit,
            ref_price=ref_price,
            cost=cost,
        )
    except ValueError as exc:
        flash(request, str(exc), error=True)
        return redirect("/products")
    flash(request, f"已新增 {product_id}，报价时可以搜到")
    return redirect("/products")


@router.post("/products/{product_id}/pricing")
def save_pricing(
    request: Request,
    product_id: str,
    spec_en: str = Form(""),
    price_unit: str = Form("1000 pcs"),
    ref_price: str = Form(""),
    cost: str = Form(""),
    user: str = Depends(current_user),
) -> Response:
    try:
        get_wb(request).quotes.set_pricing(
            product_id, spec_en=spec_en, price_unit=price_unit, ref_price=ref_price, cost=cost
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        flash(request, str(exc), error=True)
        return redirect("/products")
    flash(request, f"{product_id} 的报价信息已保存")
    return redirect("/products")


@router.get("/products/{product_id}/form", response_class=HTMLResponse)
def product_form(request: Request, product_id: str, user: str = Depends(current_user)) -> HTMLResponse:
    """规格与价格抽屉的内容（htmx 加载）。"""
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
