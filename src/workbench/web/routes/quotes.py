"""报价：报价单列表、一页式报价单（选客户、选产品、填价格、生成 PDF）、AI 拆询价、产品和客户联想、按版本下载 PDF。"""

from dataclasses import dataclass
from typing import assert_never
from urllib.parse import quote as urlquote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import Text, cast, func, or_, select
from sqlalchemy.orm import selectinload

from workbench.pipeline import QuoteAction
from workbench.pipeline.quotes import Customer, QuoteLine, QuoteSheet, QuoteTerms, last_prices, parse_lines
from workbench.quote_pdf import render_quote_pdf
from workbench.store.models import Company, Quote, QuoteStatus, QuoteVersion, RunStatus
from workbench.web.deps import current_user, flash, get_wb, redirect, render

router = APIRouter()

STATUS_TABS = [("", "全部"), ("pending", "待报价"), ("quoted", "已报价"), ("won", "成交"), ("lost", "未成交")]
DONE = {
    QuoteAction.SAVE: "已保存",
    QuoteAction.PARSE: "AI 正在拆分询价，十几秒后自动刷新",
    QuoteAction.WON: "已标为成交",
    QuoteAction.LOST: "已标为未成交",
}


@dataclass
class QuoteForm:
    """报价单页面提交的原始字段；数字在 parse_lines 里解析，出错时提示是第几行。"""

    company_id: str
    customer_name: str
    customer_country: str
    customer_contact: str
    inquiry: str | None
    currency: str
    incoterm: str
    validity_days: int
    lead_time: str
    payment_terms: str
    packing: str
    remarks: str
    product_id: list[str]
    description: list[str]
    size: list[str]
    spec: list[str]
    quantity: list[str]
    unit: list[str]
    unit_price: list[str]
    unit_cost: list[str]

    def customer(self) -> Customer:
        company_id = int(self.company_id) if self.company_id.isdigit() else None
        return Customer(self.customer_name, self.customer_country, self.customer_contact, company_id)

    def terms(self) -> QuoteTerms:
        return QuoteTerms(
            self.currency,
            self.incoterm,
            self.validity_days,
            self.lead_time,
            self.payment_terms,
            self.packing,
            self.remarks,
        )

    def lines(self) -> list[QuoteLine]:
        return parse_lines(
            self.product_id,
            self.description,
            self.size,
            self.spec,
            self.quantity,
            self.unit,
            self.unit_price,
            self.unit_cost,
        )


def quote_form(
    company_id: str = Form(""),
    customer_name: str = Form(""),
    customer_country: str = Form(""),
    customer_contact: str = Form(""),
    inquiry: str | None = Form(None),
    currency: str = Form("USD"),
    incoterm: str = Form(""),
    validity_days: int = Form(30, ge=1, le=365),
    lead_time: str = Form(""),
    payment_terms: str = Form(""),
    packing: str = Form(""),
    remarks: str = Form(""),
    product_id: list[str] = Form([]),
    description: list[str] = Form([]),
    size: list[str] = Form([]),
    spec: list[str] = Form([]),
    quantity: list[str] = Form([]),
    unit: list[str] = Form([]),
    unit_price: list[str] = Form([]),
    unit_cost: list[str] = Form([]),
) -> QuoteForm:
    return QuoteForm(
        company_id,
        customer_name,
        customer_country,
        customer_contact,
        inquiry,
        currency,
        incoterm,
        validity_days,
        lead_time,
        payment_terms,
        packing,
        remarks,
        product_id,
        description,
        size,
        spec,
        quantity,
        unit,
        unit_price,
        unit_cost,
    )


@router.get("/quotes", response_class=HTMLResponse)
def quotes(request: Request, status: str = "", q: str = "", user: str = Depends(current_user)) -> HTMLResponse:
    wb = get_wb(request)
    stmt = select(Quote).options(selectinload(Quote.versions))
    if status in QuoteStatus._value2member_map_:
        stmt = stmt.where(Quote.status == QuoteStatus(status))
    if q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Quote.customer_name.ilike(like), cast(Quote.items, Text).ilike(like)))
    with wb.db.session() as s:
        rows = s.execute(stmt.order_by(Quote.updated_at.desc()).limit(200)).scalars().all()
        counts = dict(s.execute(select(Quote.status, func.count(Quote.id)).group_by(Quote.status)).all())
    tabs = [
        (key, label, sum(counts.values()) if not key else counts.get(QuoteStatus(key), 0)) for key, label in STATUS_TABS
    ]
    return render(request, "pages/quotes.html", nav="quotes", rows=rows, tabs=tabs, status=status, q=q)


@router.get("/quotes/new", response_class=HTMLResponse)
def new_quote(
    request: Request, company_id: int | None = None, product_id: str = "", user: str = Depends(current_user)
) -> HTMLResponse:
    quote = get_wb(request).quotes.new(company_id=company_id, product_id=product_id or None)
    return _render_quote(request, quote)


@router.post("/quotes")
def create_quote(
    request: Request,
    action: QuoteAction = Form(...),
    form: QuoteForm = Depends(quote_form),
    user: str = Depends(current_user),
) -> Response:
    return _act(request, None, action, form, "", user)


@router.get("/quotes/{quote_id}", response_class=HTMLResponse)
def quote_page(
    request: Request, quote_id: int, download: int | None = None, user: str = Depends(current_user)
) -> HTMLResponse:
    with get_wb(request).db.session() as s:
        quote = s.execute(
            select(Quote).options(selectinload(Quote.versions)).where(Quote.id == quote_id)
        ).scalar_one_or_none()
    if quote is None:
        raise HTTPException(404, "报价单不存在")
    return _render_quote(request, quote, download=download)


@router.post("/quotes/{quote_id}")
def quote_action(
    request: Request,
    quote_id: int,
    action: QuoteAction = Form(...),
    reason: str = Form(""),
    form: QuoteForm = Depends(quote_form),
    user: str = Depends(current_user),
) -> Response:
    return _act(request, quote_id, action, form, reason, user)


@router.get("/quotes/{quote_id}/parse", response_class=HTMLResponse)
def parse_status(request: Request, quote_id: int, user: str = Depends(current_user)) -> Response:
    """拆询价进行中时页面每 2 秒轮询一次；结束后让页面整体刷新，显示拆出来的行。"""
    run = get_wb(request).quotes.parse_run(quote_id)
    if run is None or run.status not in (RunStatus.QUEUED, RunStatus.RUNNING):
        return Response(headers={"HX-Refresh": "true"})
    return render(request, "partials/quote_parse.html", run=run, quote_id=quote_id)


@router.get("/quotes/{quote_id}/versions/{version}.pdf")
def quote_pdf(request: Request, quote_id: int, version: int, user: str = Depends(current_user)) -> Response:
    wb = get_wb(request)
    with wb.db.session() as s:
        row = s.execute(
            select(QuoteVersion).where(QuoteVersion.quote_id == quote_id, QuoteVersion.version == version)
        ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "没有这一版报价单")
    number = f"GW-Q{quote_id:04d}"
    pdf = render_quote_pdf(number, version, row.snapshot, QuoteSheet.of_version(row, wb.settings.timezone), wb.settings)
    customer = row.snapshot["customer"]["name"].strip(" .") or "customer"
    filename = f"Quotation {number}-{version} {customer}.pdf"
    disposition = f"attachment; filename=\"{number}-{version}.pdf\"; filename*=UTF-8''{urlquote(filename)}"
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": disposition})


@router.get("/quote-products", response_class=HTMLResponse)
def product_options(
    request: Request, q: str = "", customer_name: str = "", company_id: str = "", user: str = Depends(current_user)
) -> HTMLResponse:
    results = get_wb(request).quotes.search_products(
        q, customer_name=customer_name, company_id=int(company_id) if company_id.isdigit() else None
    )
    return render(request, "partials/quote_products.html", results=results, q=q)


@router.get("/quote-customers", response_class=HTMLResponse)
def customer_options(request: Request, customer_name: str = "", user: str = Depends(current_user)) -> HTMLResponse:
    found = get_wb(request).quotes.customers(customer_name) if customer_name.strip() else []
    return render(request, "partials/quote_customers.html", customers=found)


def _act(
    request: Request, quote_id: int | None, action: QuoteAction, form: QuoteForm, reason: str, user: str
) -> Response:
    quotes = get_wb(request).quotes
    try:
        match action:
            case QuoteAction.SAVE | QuoteAction.GENERATE | QuoteAction.PARSE:
                quote_id = quotes.save(
                    quote_id, form.customer(), form.terms(), form.lines(), inquiry=form.inquiry, user=user
                )
                if action is QuoteAction.GENERATE:
                    version = quotes.generate(quote_id, user=user)
                    return redirect(f"/quotes/{quote_id}?download={version}")
                if action is QuoteAction.PARSE:
                    quotes.start_parse(quote_id, user=user)
            case QuoteAction.WON | QuoteAction.LOST:
                quotes.set_outcome(_required(quote_id), won=action is QuoteAction.WON, reason=reason)
            case QuoteAction.DELETE:
                quotes.delete(_required(quote_id))
                flash(request, "报价单已删除")
                return redirect("/quotes")
            case _:
                assert_never(action)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        flash(request, str(exc), error=True)
        return redirect(f"/quotes/{quote_id}" if quote_id else "/quotes/new")
    flash(request, DONE[action])
    return redirect(f"/quotes/{quote_id}")


def _required(quote_id: int | None) -> int:
    if quote_id is None:
        raise LookupError("报价单还没保存")
    return quote_id


def _render_quote(request: Request, quote: Quote, *, download: int | None = None) -> HTMLResponse:
    wb = get_wb(request)
    with wb.db.session() as s:
        last = last_prices(s, quote.customer_name, quote.company_id, exclude_quote_id=quote.id)
        company = s.get(Company, quote.company_id) if quote.company_id else None
    run = wb.quotes.parse_run(quote.id) if quote.id else None
    return render(
        request,
        "pages/quote.html",
        nav="quotes",
        q=quote,
        company=company,
        sheet=QuoteSheet.of(quote, wb.settings.timezone, wb.ctx.clock()),
        last=last,
        run=run,
        parsing=run is not None and run.status in (RunStatus.QUEUED, RunStatus.RUNNING),
        download=download if download and any(v.version == download for v in quote.versions) else None,
    )
