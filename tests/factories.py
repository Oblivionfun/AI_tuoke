"""测试用的 agent 交卷样例和脚本函数。"""

import json

from workbench.agents.schemas import (
    Candidate,
    CompanyDossier,
    ContactPoint,
    DraftEmail,
    ProductMatch,
    ReviewIssue,
    ReviewResult,
    ScoreItem,
    Sourced,
)


def candidate(name: str, website: str, country: str = "AE") -> Candidate:
    return Candidate(
        name=name,
        website=website,
        country=country,
        city="Dubai",
        company_type="紧固件批发商",
        criteria_met=["产品页列出自钻螺钉"],
        sources=[f"{website.rstrip('/')}/products"],
        why="测试",
    )


def dossier(
    name: str, website: str, country: str = "AE", queue: str = "first_touch", score: int = 60, email: str | None = None
) -> CompanyDossier:
    src = [f"{website.rstrip('/')}/about"]
    contacts = []
    if email:
        contacts.append(ContactPoint(kind="email", value=email, label="官网联系页邮箱", source=f"{website}/contact"))
    return CompanyDossier(
        company_name=name,
        website=website,
        country=country,
        city=Sourced(value="Dubai", sources=src),
        identity=Sourced(value=f"{name}，官网可访问", sources=src),
        business_type=Sourced(value="批发", sources=src),
        buyer_role=Sourced(value="进口商", sources=src),
        products=[
            ProductMatch(
                their_product="Tek screws",
                our_product="Pan head self-drilling screws / 盘头自钻螺钉",
                match_level="family",
                source=f"{website}/products",
            )
        ],
        contacts=contacts,
        signals=[],
        risks=[],
        missing=["采购负责人"],
        scores=[ScoreItem(dimension="identity", score=20, max_score=25, reason="官网可访问")],
        total_score=score,
        queue=queue,  # type: ignore[arg-type]
        queue_reason="测试",
        contact_route="官网邮箱首封",
        summary_zh=f"{name} 是测试公司",
    )


def review(verdict: str = "pass") -> ReviewResult:
    issues = (
        []
        if verdict == "pass"
        else [
            ReviewIssue(
                field="contacts[0]", problem="邮箱在来源中找不到", evidence="联系页只有电话", severity="must_fix"
            )
        ]
    )
    return ReviewResult(
        verdict=verdict,  # type: ignore[arg-type]
        checked_sources=["https://example.ae/contact"],
        issues=issues,
        notes_for_researcher="无" if verdict == "pass" else "删除找不到的邮箱",
        summary_zh="已核对" if verdict == "pass" else "联系方式有问题",
    )


def draft(kind: str = "first_email") -> DraftEmail:
    return DraftEmail(
        kind=kind,  # type: ignore[arg-type]
        subject_en="Self-drilling screws from Yongnian",
        body_en="Dear team, ...",
        subject_zh="来自永年的自钻螺钉",
        body_zh="您好……",
        personalization_basis=["对方在售 Tek screws"],
        send_tips_zh="发往官网通用邮箱",
    )


def company_from_input(input_text: str) -> dict:
    return json.loads(input_text)["company"]


def researcher_echo(spec, input_text):
    """调研脚本：按输入的公司原样交回一份档案，邮箱用 info@域名。"""
    c = company_from_input(input_text)
    return dossier(c["name"], c["website"], c["country"], email=f"info@{c['website'].split('//')[1].strip('/')}")
