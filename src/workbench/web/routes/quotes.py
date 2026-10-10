"""报价单：从询价开报价、编辑多行价格、登记发出、记录对方结果、改价出新版本、打印成英文报价单。"""

from dataclasses import dataclass
from typing import assert_never

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from workbench.pipeline import QuoteAction
from workbench.pipeline.quotes import QuoteSheet, QuoteTerms, parse_lines
from workbench.store.models import Channel, Quote, Rfq
from workbench.web.deps import current_user, flash, get_wb, redirect, render

router = APIRouter()

DONE = {
    QuoteAction.SAVE: "报价草稿已保存",
    QuoteAction.MARK_SENT: "已登记发出，这一版不能再改",
    QuoteAction.ACCEPT: "已记录：对方接受报价",
    QuoteAction.REJECT: "已记录：对方未接受",
    QuoteAction.REVISE: "已出新版本草稿",
    QuoteAction.DISCARD: "报价草稿已删除",
}


@dataclass
class QuoteForm:
    """编辑页提交的原始字段。数字在 parse_lines 里解析，出错时提示是第几行。"""

    currency: str
    incoterm: str
    validity_days: int
    lead_time: str
    payment_terms: str
    packing: str
    remarks: str
    description: list[str]
    spec: list[str]
    quantity: list[str]
    unit: list[str]
    unit_price: list[str]
    unit_cost: list[str]

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


def quote_form(
    currency: str = Form("USD"),
    incoterm: str = Form(""),
    validity_days: int = Form(30, ge=1, le=365),
    lead_time: str = Form(""),
    payment_terms: str = Form(""),
    packing: str = Form(""),
    remarks: str = Form(""),
    description: list[str] = Form([]),
    spec: list[str] = Form([]),
    quantity: list[str] = Form([]),
    unit: list[str] = Form([]),
    unit_price: list[str] = Form([]),
    unit_cost: list[str] = Form([]),
) -> QuoteForm:
    return QuoteForm(
        currency,
        incoterm,
        validity_days,
        lead_time,
        payment_terms,
        packing,
        remarks,
        description,
        spec,
        quantity,
        unit,
        unit_price,
        unit_cost,
    )


@router.post("/rfqs/{rfq_id}/quote")
def open_quote(request: Request, rfq_id: int, user: str = Depends(current_user)) -> Response:
    try:
        quote_id = get_wb(request).quotes.open_for(rfq_id, user=user)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        flash(request, str(exc), error=True)
        return redirect(request.headers.get("referer") or "/followups")
    return redirect(f"/quotes/{quote_id}")


@router.get("/quotes/{quote_id}", response_class=HTMLResponse)
def quote_page(request: Request, quote_id: int, user: str = Depends(current_user)) -> HTMLResponse:
    quote, sheet = _load(request, quote_id)
    return render(request, "pages/quote.html", nav="followups", q=quote, sheet=sheet)


@router.get("/quotes/{quote_id}/print", response_class=HTMLResponse)
def quote_print(request: Request, quote_id: int, user: str = Depends(current_user)) -> HTMLResponse:
    quote, sheet = _load(request, quote_id)
    return render(request, "pages/quote_print.html", q=quote, sheet=sheet)


@router.post("/quotes/{quote_id}")
def quote_action(
    request: Request,
    quote_id: int,
    action: QuoteAction = Form(...),
    channel: Channel = Form(Channel.WHATSAPP),
    note: str = Form(""),
    then: str = Query("", alias="next"),
    form: QuoteForm = Depends(quote_form),
    user: str = Depends(current_user),
) -> Response:
    quotes = get_wb(request).quotes
    target = f"/quotes/{quote_id}"
    try:
        match action:
            case QuoteAction.SAVE | QuoteAction.MARK_SENT:
                lines = parse_lines(
                    form.description, form.spec, form.quantity, form.unit, form.unit_price, form.unit_cost
                )
                quotes.save(quote_id, form.terms(), lines)
                if then == "print":
                    target = f"/quotes/{quote_id}/print"
                if action is QuoteAction.MARK_SENT:
                    quotes.mark_sent(quote_id, channel=channel, user=user)
            case QuoteAction.ACCEPT:
                quotes.record_outcome(quote_id, accepted=True)
            case QuoteAction.REJECT:
                quotes.record_outcome(quote_id, accepted=False, note=note)
            case QuoteAction.REVISE:
                target = f"/quotes/{quotes.revise(quote_id, user=user)}"
            case QuoteAction.DISCARD:
                target = f"/companies/{quotes.discard(quote_id)}?tab=followup"
            case _:
                assert_never(action)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        flash(request, str(exc), error=True)
        return redirect(f"/quotes/{quote_id}")
    flash(request, DONE[action])
    return redirect(target)


def _load(request: Request, quote_id: int) -> tuple[Quote, QuoteSheet]:
    wb = get_wb(request)
    with wb.db.session() as s:
        quote = s.execute(
            select(Quote)
            .options(selectinload(Quote.rfq).selectinload(Rfq.quotes), selectinload(Quote.company))
            .where(Quote.id == quote_id)
        ).scalar_one_or_none()
    if quote is None:
        raise HTTPException(404, "报价单不存在")
    return quote, QuoteSheet.of(quote, wb.settings.timezone, wb.ctx.clock())
