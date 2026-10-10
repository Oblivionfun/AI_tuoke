"""4 个 agent 的交卷格式。

这些类型只用来固定字段、存库和展示，不在代码里校验结论；结论质量靠提示词契约和审查 agent。
所有字段都必填（Agents SDK 的严格 JSON Schema 要求），查不到时 agent 写"未找到"或给空列表。
"""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

NOT_FOUND = "未找到"


class AgentRole(StrEnum):
    PROSPECTOR = "prospector"
    RESEARCHER = "researcher"
    REVIEWER = "reviewer"
    WRITER = "writer"
    JUDGE = "judge"


ROLE_LABELS: dict[AgentRole, str] = {
    AgentRole.PROSPECTOR: "寻客",
    AgentRole.RESEARCHER: "调研",
    AgentRole.REVIEWER: "审查",
    AgentRole.WRITER: "文案",
    AgentRole.JUDGE: "评委",
}


# ---------- 寻客 ----------


class Candidate(BaseModel):
    name: str = Field(description="公司名，按官网写法")
    website: str = Field(description="公司自己的官网首页 URL；不能是目录站、平台店铺或社交主页")
    country: str = Field(description="ISO 3166-1 两位国家代码，例如 AE、ZA、SA")
    city: str = Field(description="城市；查不到写'未找到'")
    company_type: str = Field(description="批发商 / 分销商 / 进口商 / 五金建材连锁 / 工业供应商 等，用中文")
    criteria_met: list[str] = Field(description="满足了寻客条件中的哪 1-3 条，每条一句中文，写清楚从哪里看出来")
    sources: list[str] = Field(description="能证明上述条件的页面 URL，至少 1 个，优先官网页面")
    why: str = Field(description="一句中文入选理由")


class CandidateList(BaseModel):
    candidates: list[Candidate]
    search_notes: str = Field(description="中文：搜了哪些方向、哪些方向没结果、遇到的困难")


# ---------- 调研 ----------


class Sourced(BaseModel):
    value: str = Field(description="结论；查不到写'未找到'")
    sources: list[str] = Field(description="支撑这条结论的 URL；'未找到'时给空列表")


class ProductMatch(BaseModel):
    their_product: str = Field(description="对方在售或在采购的产品，按对方网页原文")
    our_product: str = Field(description="对应的我方产品卡名称（来自知识包里的产品清单）")
    match_level: Literal["sku", "family"] = Field(description="sku=对上已确认的首推 SKU；family=只对上产品族")
    source: str = Field(description="对方网页 URL")


class ContactPoint(BaseModel):
    kind: Literal["email", "phone", "whatsapp", "form", "supplier_portal", "linkedin", "address", "other"]
    value: str = Field(description="原样抄录的联系方式；不得推测或拼接")
    label: str = Field(description="中文说明，例如'官网联系页的通用邮箱''供应商注册入口'")
    source: str = Field(description="出现这条联系方式的页面 URL")


class Signal(BaseModel):
    text: str = Field(description="采购信号，中文，例如'官网招募供应商''近期招标采购紧固件''新开分店'")
    date: str = Field(description="信号日期，YYYY-MM 或 YYYY；查不到写'未找到'")
    source: str


class Risk(BaseModel):
    text: str = Field(description="风险，中文")
    severity: Literal["red", "yellow"] = Field(
        description="red=红线（同行制造商、反倾销申诉方、受制裁、身份无法确认）；yellow=需注意"
    )
    source: str = Field(description="依据 URL；属于知识包规则的写'知识包'")


class ScoreItem(BaseModel):
    dimension: Literal["identity", "product_fit", "purchase_signal", "reachability", "risk"]
    score: int
    max_score: int
    reason: str = Field(description="中文打分理由，引用档案里的事实")


class CompanyDossier(BaseModel):
    company_name: str
    website: str
    country: str = Field(description="ISO 两位国家代码")
    city: Sourced
    identity: Sourced = Field(description="公司是谁：法人名称、成立年份、规模、地址，能查到多少写多少")
    business_type: Sourced = Field(description="业务类型：批发 / 分销 / 进口 / 零售 / 制造 / 项目承包")
    buyer_role: Sourced = Field(description="为什么它会向我们这样的工厂采购：进口、分销、项目采购、自有品牌等")
    products: list[ProductMatch]
    contacts: list[ContactPoint]
    signals: list[Signal]
    risks: list[Risk]
    missing: list[str] = Field(description="还缺哪些关键信息，中文")
    scores: list[ScoreItem] = Field(description="按 25/35/15/15/10 口径的 5 项打分")
    total_score: int
    queue: Literal["first_touch", "qualified", "unfit"]
    queue_reason: str = Field(description="中文：为什么归入这个队列")
    contact_route: str = Field(description="中文：建议的联系路线和顺序，遵守目标国家说明")
    summary_zh: str = Field(description="两三句中文概述，给业务员一眼看懂")


# ---------- 审查 ----------


class ReviewIssue(BaseModel):
    field: str = Field(description="有问题的档案字段，例如 contacts[0]、identity")
    problem: str = Field(description="中文：问题是什么")
    evidence: str = Field(description="中文：来源实际写了什么，或打不开 / 找不到")
    severity: Literal["must_fix", "minor"]


class ReviewResult(BaseModel):
    verdict: Literal["pass", "needs_fix"]
    checked_sources: list[str] = Field(description="实际打开核对过的 URL")
    issues: list[ReviewIssue]
    notes_for_researcher: str = Field(description="中文修改意见；通过时写'无'")
    summary_zh: str = Field(description="一句中文审查结论，给业务员看")


# ---------- 文案 ----------


class DraftEmail(BaseModel):
    kind: Literal["first_email", "consent_request"]
    subject_en: str
    body_en: str
    subject_zh: str
    body_zh: str = Field(description="body_en 的中文对照，给业务员看懂用")
    personalization_basis: list[str] = Field(description="中文：信里用到了档案里的哪些事实")
    send_tips_zh: str = Field(description="中文发送提示：发给哪个地址、什么时间、注意什么")


# ---------- 评委（评测集用） ----------


class JudgeScore(BaseModel):
    criterion: Literal["sourcing", "accuracy", "fit_judgement", "contactability", "usefulness"]
    score: int = Field(description="1-5 分")
    reason: str


class JudgeResult(BaseModel):
    scores: list[JudgeScore]
    queue_agrees_with_label: bool
    fabrication_suspected: bool = Field(description="是否怀疑有编造的邮箱、人名或来源")
    comments: str = Field(description="中文点评")


OUTPUT_TYPES: dict[AgentRole, type[BaseModel]] = {
    AgentRole.PROSPECTOR: CandidateList,
    AgentRole.RESEARCHER: CompanyDossier,
    AgentRole.REVIEWER: ReviewResult,
    AgentRole.WRITER: DraftEmail,
    AgentRole.JUDGE: JudgeResult,
}
