"""模板用的展示逻辑：显示名称、评分构成、来源编号、联系路线拆步、下一步提示。

原则：每张卡先回答"值不值得联系、为什么、怎么联系"，细节和来源退到第二层。
"""

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, assert_never
from urllib.parse import urlsplit

from workbench.pipeline import pick_email
from workbench.pipeline.quotes import QuoteSheet
from workbench.store.models import (
    Channel,
    Company,
    CompanyStatus,
    Queue,
    Quote,
    QuoteStatus,
    RfqStatus,
    RunKind,
    RunStatus,
)

COUNTRIES = {"AE": "阿联酋", "ZA": "南非", "SA": "沙特"}
COUNTRIES_EN = {"AE": "United Arab Emirates", "ZA": "South Africa", "SA": "Saudi Arabia"}
QUEUE_LABELS = {q: q.label for q in Queue}
STATUS_LABELS = {
    CompanyStatus.NEW: "待调研",
    CompanyStatus.RESEARCHING: "调研中",
    CompanyStatus.QUEUED: "待审核",
    CompanyStatus.ACCEPTED: "已接受",
    CompanyStatus.NEEDS_EVIDENCE: "补证中",
    CompanyStatus.CONTACTED: "已联系",
    CompanyStatus.REPLIED: "已回复",
    CompanyStatus.ARCHIVED: "已归档",
    CompanyStatus.FAILED: "调研失败",
}
STATUS_TONE = {
    CompanyStatus.QUEUED: "blue",
    CompanyStatus.ACCEPTED: "pass",
    CompanyStatus.CONTACTED: "pass",
    CompanyStatus.REPLIED: "pass",
    CompanyStatus.NEEDS_EVIDENCE: "caution",
    CompanyStatus.RESEARCHING: "live",
    CompanyStatus.FAILED: "reject",
}
RUN_KIND_LABELS = {RunKind.PROSPECT: "寻客", RunKind.RESEARCH: "调研", RunKind.WRITE: "写信"}
RUN_STATUS_LABELS = {
    RunStatus.QUEUED: "排队中",
    RunStatus.RUNNING: "运行中",
    RunStatus.SUCCEEDED: "完成",
    RunStatus.FAILED: "失败",
}
RUN_STATUS_TONE = {
    RunStatus.QUEUED: "",
    RunStatus.RUNNING: "live",
    RunStatus.SUCCEEDED: "pass",
    RunStatus.FAILED: "reject",
}
CONTACT_LABELS = {
    "email": "邮箱",
    "phone": "电话",
    "whatsapp": "WhatsApp",
    "form": "询价表单",
    "supplier_portal": "供应商入口",
    "linkedin": "LinkedIn",
    "address": "地址",
    "other": "其他",
}
CHANNEL_LABELS = {
    Channel.EMAIL: "邮件",
    Channel.WHATSAPP: "WhatsApp",
    Channel.PHONE: "电话",
    Channel.LINKEDIN: "LinkedIn",
    Channel.PORTAL: "供应商入口",
    Channel.OTHER: "其他",
}
RFQ_LABELS = {
    RfqStatus.OPEN: "待报价",
    RfqStatus.QUOTED: "已报价",
    RfqStatus.SAMPLE: "打样中",
    RfqStatus.WON: "成交",
    RfqStatus.LOST: "未成交",
}
QUOTE_LABELS = {
    QuoteStatus.DRAFT: "草稿",
    QuoteStatus.SENT: "已发出",
    QuoteStatus.ACCEPTED: "对方接受",
    QuoteStatus.REJECTED: "对方未接受",
    QuoteStatus.SUPERSEDED: "已被新版本替代",
}
QUOTE_TONE = {
    QuoteStatus.DRAFT: "",
    QuoteStatus.SENT: "blue",
    QuoteStatus.ACCEPTED: "pass",
    QuoteStatus.REJECTED: "reject",
    QuoteStatus.SUPERSEDED: "",
}
CURRENCIES = ["USD", "EUR", "CNY"]
QUOTE_UNITS = ["pcs", "1000 pcs", "kg", "MT", "sets", "cartons"]
PRODUCT_GROUPS = ["自钻螺钉", "抽芯铆钉 / 拉铆螺母", "垫圈", "膨胀锚栓", "干壁钉 / 木螺钉", "螺栓螺母"]
UNFIT_REASONS = ["同行制造商", "产品不对口", "规模太小", "联系不上", "只做零售"]

RUBRIC = [
    ("identity", "身份真实", 25),
    ("product_fit", "产品匹配", 35),
    ("purchase_signal", "采购信号", 15),
    ("reachability", "可联系性", 15),
    ("risk", "风险", 10),
]


@dataclass
class RubricPart:
    key: str
    label: str
    score: int
    max_score: int
    reason: str

    @property
    def pct(self) -> int:
        return round(100 * min(max(self.score, 0), self.max_score) / self.max_score) if self.max_score else 0

    @property
    def weak(self) -> bool:
        return self.pct < 50


def rubric(dossier: dict[str, Any] | None) -> list[RubricPart]:
    """按 25/35/15/15/10 的固定顺序取出五项得分；缺项按 0 分。"""
    given = {s["dimension"]: s for s in (dossier or {}).get("scores", [])}
    parts = []
    for key, label, max_score in RUBRIC:
        item = given.get(key, {})
        parts.append(
            RubricPart(
                key,
                label,
                int(item.get("score", 0)),
                int(item.get("max_score", max_score)) or max_score,
                item.get("reason", ""),
            )
        )
    return parts


class SourceIndex:
    """给档案里的来源编号：正文按出现顺序引用 [1][2]，页面底部列出完整网址。"""

    def __init__(self) -> None:
        self.urls: list[str] = []

    def ref(self, url: str | None) -> int | None:
        if not url or not url.startswith("http"):
            return None
        if url not in self.urls:
            self.urls.append(url)
        return self.urls.index(url) + 1


def short_url(url: str) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").removeprefix("www.")
    path = parts.path.rstrip("/")
    return host + (path if len(path) <= 40 else path[:38] + "…")


def our_product(name: str) -> str:
    """'Pan head self-drilling screws / 盘头自钻螺钉' → '盘头自钻螺钉'。"""
    return name.rsplit(" / ", 1)[-1].strip() if " / " in name else name


_STEP_SPLIT = re.compile(r"(?:^|(?<=[\s。；;，,]))\d{1,2}[.、)]\s*")


def route_steps(text: str | None) -> list[str]:
    """把"1. … 2. …"形式的联系路线拆成步骤；没有编号就整段返回。"""
    if not text:
        return []
    steps = [s.strip(" ；;。") for s in _STEP_SPLIT.split(text) if s.strip(" ；;。")]
    return steps or [text]


def primary_contact(dossier: dict[str, Any] | None) -> tuple[dict[str, Any] | None, int]:
    """最该用的联系入口（采购邮箱 > 第一个邮箱 > 第一个联系方式）和其余入口数。"""
    contacts = (dossier or {}).get("contacts", [])
    if not contacts:
        return None, 0
    email = pick_email(dossier)
    chosen = next((c for c in contacts if c.get("value") == email), None) if email else None
    chosen = chosen or next((c for c in contacts if c.get("kind") != "address"), contacts[0])
    return chosen, len(contacts) - 1


def top_risks(dossier: dict[str, Any] | None, limit: int = 3) -> list[dict[str, Any]]:
    risks = (dossier or {}).get("risks", [])
    return sorted(risks, key=lambda r: r.get("severity") != "red")[:limit]


def next_step(company: Company) -> str:
    """客户列表里"下一步"一栏：告诉业务员这家公司现在等谁做什么。"""
    match company.status:
        case CompanyStatus.QUEUED:
            return "等你审核"
        case CompanyStatus.ACCEPTED:
            return "草稿待发送" if any(d.status.value == "draft" for d in company.drafts) else "正在写草稿"
        case CompanyStatus.CONTACTED:
            return "等对方回复"
        case CompanyStatus.REPLIED:
            return "跟进报价"
        case CompanyStatus.NEEDS_EVIDENCE | CompanyStatus.RESEARCHING | CompanyStatus.NEW:
            return "调研中"
        case CompanyStatus.FAILED:
            return "调研失败，可重试"
        case CompanyStatus.ARCHIVED:
            return "已归档"
        case _:
            assert_never(company.status)


def lane_result(company: Company | None, fallback: str) -> str:
    """运行页分道的结果：用公司当前的队列、分数和审查结论，而不是运行时写下的原始摘要。"""
    if company is None or not company.dossier or company.queue is None:
        return fallback
    verdict = (company.review or {}).get("verdict")
    review = "审查通过" if verdict == "pass" else "审查未通过" if verdict else "未审查"
    return f"{company.queue.label}，{company.score} 分，{review}"


def review_summary(review: dict[str, Any] | None) -> tuple[str, str]:
    """审查结论的语气和一句话说明。"""
    if not review:
        return "", "还没有审查"
    if review.get("verdict") == "pass":
        return "pass", f"审查通过，核对了 {len(review.get('checked_sources', []))} 个来源"
    must_fix = sum(1 for i in review.get("issues", []) if i.get("severity") == "must_fix")
    return "reject", f"审查未通过，{must_fix} 处必须修改"


def quote_sheet(quote: Quote, tz: str, now: datetime) -> QuoteSheet:
    return QuoteSheet.of(quote, tz, now)


def money(value: Decimal) -> str:
    return f"{value:,.2f}"


def unit_price(value: Decimal | None) -> str:
    """单价保留实际填写的位数，至少两位小数：0.0185、1.20。"""
    if value is None:
        return ""
    whole, _, frac = format(value.normalize(), "f").partition(".")
    return f"{int(whole):,}.{frac.ljust(2, '0')}"


def quantity(value: Decimal | None) -> str:
    return format(value.normalize(), ",f") if value is not None else ""


def plain(value: Decimal | None) -> str:
    """填回输入框的数字：不带千分位。"""
    return format(value.normalize(), "f") if value is not None else ""


def ago(value: datetime | None, now: datetime) -> str:
    if value is None:
        return ""
    seconds = (now - value).total_seconds()
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小时前"
    return f"{int(seconds // 86400)} 天前"
