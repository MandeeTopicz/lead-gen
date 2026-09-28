"""Stage 4: turn this run's search hits into deduped leads and companies, and build scoring facts from them.

Stage 3 records each parsed card as a search hit as soon as it's read (so a halt mid-search keeps what was
collected); this stage normalizes those hits into the leads and companies tables.
"""

from datetime import UTC, date, datetime, timedelta

from sqlmodel import Session, select

from db.models import Company, Lead, Post, Run, SearchHit
from pipeline.context import RunContext, now_iso
from pipeline.linkedin.cards import (
    POSTED_RECENTLY,
    POSTED_RECENTLY_DAYS,
    Card,
    canonical_company_url,
    canonical_lead_url,
    tenure_months,
)
from pipeline.linkedin.profile_page import months_since
from pipeline.scoring.match import LeadFacts


def record_card(session: Session, run: Run, search_code: str, card: Card) -> Lead:
    """Store one result card as a search hit, creating the lead on first sight. Deduped by profile URL."""
    profile_url = canonical_lead_url(card.profile_url)
    lead = session.exec(select(Lead).where(Lead.profile_url == profile_url)).first()
    if lead is None:
        lead = Lead(profile_url=profile_url, full_name=card.full_name, first_seen_run_id=run.id)
        session.add(lead)
        session.flush()
    hit = session.get(SearchHit, (run.id, lead.id, search_code))
    if hit is None:
        hit = SearchHit(run_id=run.id, lead_id=lead.id, search_code=search_code)
    hit.card_data = card.model_dump(mode="json")
    session.add(hit)
    return lead


def collect_dedupe(ctx: RunContext) -> None:
    """Stage 4."""
    cards = latest_cards(ctx.session, ctx.run.id)
    for lead_id, card in cards.items():
        lead = ctx.session.get(Lead, lead_id)
        lead.full_name = card.full_name
        lead.current_title = card.title
        lead.location = card.location
        lead.connection_degree = card.connection_degree
        lead.company_id = _upsert_company(ctx.session, card)
        months_in_role = tenure_months(card.time_in_role)
        if months_in_role is not None:
            lead.started_current_role = (date.today() - timedelta(days=round(months_in_role * 30.44))).isoformat()
        ctx.session.add(lead)
    ctx.run.leads_found = len(cards)
    ctx.session.add(ctx.run)
    ctx.session.commit()
    ctx.log.info("%d unique leads from %d search hits", len(cards), _hit_count(ctx.session, ctx.run.id))


def latest_cards(session: Session, run_id: int) -> dict[int, Card]:
    """One card per lead for this run. The same person's card is the same across searches; keep the richest."""
    cards: dict[int, Card] = {}
    for hit in session.exec(select(SearchHit).where(SearchHit.run_id == run_id)).all():
        card = Card.model_validate(hit.card_data)
        current = cards.get(hit.lead_id)
        if current is None or _filled(card) > _filled(current):
            if current is not None:
                card.spotlights = sorted(set(card.spotlights) | set(current.spotlights))
            cards[hit.lead_id] = card
        else:
            current.spotlights = sorted(set(current.spotlights) | set(card.spotlights))
    return cards


def facts_for_run(session: Session, run_id: int) -> dict[int, LeadFacts]:
    """Scoring facts per lead from this run's cards and the searches that returned them."""
    searches: dict[int, set[str]] = {}
    for hit in session.exec(select(SearchHit).where(SearchHit.run_id == run_id)).all():
        searches.setdefault(hit.lead_id, set()).add(hit.search_code)
    facts = {}
    for lead_id, card in latest_cards(session, run_id).items():
        facts[lead_id] = facts_from_card(card, searches.get(lead_id, set()))
        lead = session.get(Lead, lead_id)
        if lead.last_deep_read_at:
            posts = session.exec(select(Post).where(Post.lead_id == lead_id)).all()
            add_profile_facts(facts[lead_id], lead, posts, card)
        company = session.get(Company, lead.company_id) if lead.company_id else None
        if company is not None and company.industry:
            add_company_facts(facts[lead_id], company)
    return facts


def add_profile_facts(facts: LeadFacts, lead: Lead, posts: list[Post], card: Card) -> None:
    """Confirmed data from the lead's full profile replaces what the card and searches implied."""
    latest = max((p.posted_at for p in posts if p.posted_at), default=None)
    if latest:
        facts.posted_within_days = (datetime.now(UTC) - datetime.fromisoformat(latest)).days
    else:
        facts.no_recent_posts = True
    facts.sources["activity"] = "profile"

    # Tenure at the company: from the earliest role there (people are often promoted within a company).
    company_id = canonical_company_url(card.company_url)
    starts = [
        role["start"]
        for role in lead.experience or []
        if role.get("start")
        and ((company_id and role.get("company_url") == company_id) or role.get("company_name") == card.company_name)
    ]
    if starts:
        facts.months_at_company = months_since(min(starts))
        facts.sources["tenure"] = "profile"

    texts = [lead.headline, lead.about] + [
        f"{role.get('title') or ''} {role.get('description') or ''}" for role in (lead.experience or [])[:3]
    ]
    facts.keyword_text = " ".join(t for t in [facts.keyword_text, *texts] if t)
    facts.sources["keywords"] = "profile"


def add_company_facts(facts: LeadFacts, company: Company) -> None:
    facts.industry = company.industry
    facts.headcount = company.headcount
    facts.keyword_text = " ".join(t for t in [facts.keyword_text, company.industry, company.description] if t)
    facts.sources["industry"] = facts.sources["size"] = "company_page"


def facts_from_card(card: Card, searches: set[str]) -> LeadFacts:
    facts = LeadFacts(
        title=card.title,
        company_name=card.company_name,
        location=card.location,
        months_at_company=tenure_months(card.time_in_company),
        keyword_text=" ".join(t for t in (card.title, card.company_name, card.about) if t),
        searches=searches,
    )
    # The "Posted on LinkedIn" spotlight proves a post in the last 30 days. Its absence proves nothing:
    # searches without the activity filter don't show it for everyone who posted.
    if POSTED_RECENTLY in card.spotlights:
        facts.posted_within_days = POSTED_RECENTLY_DAYS
    return facts


def _upsert_company(session: Session, card: Card) -> int | None:
    if not card.company_name:
        return None
    url = canonical_company_url(card.company_url)
    query = select(Company).where(Company.linkedin_url == url) if url else select(Company).where(
        Company.name == card.company_name, Company.linkedin_url == None  # noqa: E711
    )
    company = session.exec(query).first()
    if company is None:
        company = Company(linkedin_url=url, name=card.company_name, updated_at=now_iso())
        session.add(company)
        session.flush()
    return company.id


def _filled(card: Card) -> int:
    return sum(value not in (None, "", []) for value in card.model_dump().values())


def _hit_count(session: Session, run_id: int) -> int:
    return len(session.exec(select(SearchHit.lead_id).where(SearchHit.run_id == run_id)).all())
