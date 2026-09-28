"""Stage 6: read the full profile and company page for the highest-scoring leads, then rescore.

Only leads that passed the gates are read, best match first, up to `caps.deep_reads`. A lead or company read
within `caps.deep_read_refresh_days` is not read again; its stored data is reused.
"""

import random
from datetime import datetime, timedelta
from urllib.parse import urlparse

from playwright.sync_api import Page
from sqlmodel import col, delete, select

from db.models import Company, Lead, Post, Score
from pipeline.collect import latest_cards
from pipeline.context import CapReached, RunContext, now_iso
from pipeline.linkedin.pacing import Pacer
from pipeline.linkedin.profile_page import CompanyProfile, LeadProfile, parse_company_page, parse_lead_page
from pipeline.linkedin.results_page import ParseError
from pipeline.scoring.match_stage import match_score


def deep_read(ctx: RunContext) -> None:
    if ctx.dry_run:
        ctx.log.info("dry run: not opening LinkedIn")
        match_score(ctx)
        return
    pacer: Pacer = ctx.state["pacer"]
    caps = ctx.config.icp.caps
    cards = latest_cards(ctx.session, ctx.run.id)
    ranked = ctx.session.exec(
        select(Score)
        .where(Score.run_id == ctx.run.id, Score.gates_passed == True)  # noqa: E712
        .order_by(col(Score.match_score).desc())
        .limit(caps.deep_reads)
    ).all()

    read = reused = 0
    for score in ranked:
        lead = ctx.session.get(Lead, score.lead_id)
        if _fresh(lead.last_deep_read_at, caps.deep_read_refresh_days):
            reused += 1
            continue
        card = cards[lead.id]
        try:
            pacer.goto(card.profile_url, cost="deep_read")
        except CapReached:
            break
        store_profile(ctx.session, lead, _parse(pacer, parse_lead_page, "lead"))

        company = ctx.session.get(Company, lead.company_id) if lead.company_id else None
        if company and card.company_url and (
            company.industry is None or not _fresh(company.updated_at, caps.deep_read_refresh_days)
        ):
            pacer.goto(card.company_url)  # part of this lead's deep read, not a second one
            store_company(company, _parse(pacer, parse_company_page, "company"))
            ctx.session.add(company)
        ctx.session.add(ctx.run)
        ctx.session.commit()  # keep each lead's read even if a later page halts the run
        read += 1

    ctx.log.info("deep-read %d leads, reused %d recent reads", read, reused)
    match_score(ctx)


def store_profile(session, lead: Lead, profile: LeadProfile) -> None:
    lead.headline = profile.headline
    lead.about = profile.about
    lead.experience = [role.model_dump() for role in profile.experience]
    lead.education = profile.education
    lead.last_deep_read_at = now_iso()
    session.add(lead)
    session.exec(delete(Post).where(Post.lead_id == lead.id))
    for post in profile.posts:
        session.add(Post(lead_id=lead.id, posted_at=post.posted_at, text=post.text))


def store_company(company: Company, profile: CompanyProfile) -> None:
    if profile.website:
        company.domain = urlparse(profile.website).netloc.removeprefix("www.") or company.domain
    company.industry = profile.industry
    company.headcount = profile.headcount
    company.headcount_growth_6mo = profile.growth_6mo
    company.hq_location = profile.location
    company.description = profile.description
    company.updated_at = now_iso()


def _parse(pacer: Pacer, parser, kind: str):
    html = render_page(pacer.page)
    try:
        return parser(html)
    except ParseError as exc:
        pacer.halt(f"could not parse the {kind} page: {exc}")


def render_page(page: Page, rng: random.Random | None = None) -> str:
    """Profile sections render as they scroll into view; scroll through the page like a reader would."""
    rng = rng or random.Random()
    page.mouse.move(700, 500)
    for _ in range(rng.randint(8, 11)):
        page.mouse.wheel(0, rng.randint(550, 850))
        page.wait_for_timeout(rng.randint(500, 1000))
    return page.content()


def _fresh(timestamp: str | None, days: int) -> bool:
    if not timestamp:
        return False
    return datetime.now().astimezone() - datetime.fromisoformat(timestamp) < timedelta(days=days)
