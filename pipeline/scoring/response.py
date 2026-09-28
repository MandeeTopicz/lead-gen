"""Stage 8: response likelihood (0-100) and priority, for every lead that passed the gates.

A heuristic ranking, not a probability; calibrate it once reply data exists. Rules-based like the match score:
each factor records its points and the evidence behind them (finding ids, post dates, card data).
"""

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from sqlmodel import Session, select

from db.models import Finding, Lead, Post, Score
from pipeline.collect import facts_for_run, latest_cards
from pipeline.config import Config
from pipeline.context import RunContext
from pipeline.linkedin.cards import Card
from pipeline.research import published_email
from pipeline.scoring.match import LeadFacts, score_match


@dataclass
class ResponseInputs:
    days_since_post: int | None  # None: no known post
    mutual_connections: int
    connection_degree: int | None
    shared_background: list[str]
    in_primary_area: bool
    months_at_company: int | None
    findings: list[Finding] = field(default_factory=list)
    post_date_exact: bool = True  # False: only LinkedIn's "recent posts" badge, meaning within 30 days


@dataclass
class ResponseResult:
    score: float
    label: str
    factors: list[dict]

    def breakdown(self) -> dict:
        return {"factors": self.factors}


def score_response(inputs: ResponseInputs, config: Config) -> ResponseResult:
    points = config.icp.response_points
    weights = config.icp.weights.response
    usable = [f for f in inputs.findings if f.confidence in ("verified", "likely")]
    factors = []

    def add(name: str, earned: int, cap: int, detail: str | None, finding_ids: list[int] | None = None) -> None:
        factors.append({"factor": name, "points": min(earned, cap), "max": cap, "detail": detail,
                        "finding_ids": finding_ids or []})

    # Activity
    days = inputs.days_since_post
    rules = points.activity
    if days is None:
        add("activity", 0, weights.activity, "no recent posts known")
    else:
        earned = next((p for limit, p in ((7, rules.within_7_days), (30, rules.within_30_days),
                                          (90, rules.within_90_days)) if days <= limit), 0)
        detail = f"last post {days} days ago" if inputs.post_date_exact else \
            f"posted within the last {days} days (LinkedIn badge)"
        add("activity", earned, weights.activity, detail)

    # Affinity and warm paths. Mutual connections earn their points once; the second half needs a real shared
    # employer, school, or community with the sender (sender.yaml), not just any "affinity" finding.
    affinity_findings = [f for f in usable if f.kind == "affinity"]
    earned, details = 0, []
    if inputs.mutual_connections:
        earned += points.affinity.shared_connections
        details.append(_plural(inputs.mutual_connections, "mutual connection"))
    if inputs.shared_background:
        earned += points.affinity.shared_background
        details.append("shared " + ", ".join(inputs.shared_background))
    add("affinity", earned, weights.affinity, "; ".join(details) or None, [f.id for f in affinity_findings])

    # Local
    add("local", points.local if inputs.in_primary_area else 0, weights.local,
        "Greater Austin: coffee is realistic" if inputs.in_primary_area else None)

    # Timing trigger
    hiring = [f for f in usable if f.kind == "hiring" and _ops_role(f.value, points.trigger.hiring_role_terms)]
    news = [f for f in usable if f.kind == "news" and _within_days(f.event_date, points.trigger.news_within_days)]
    earned, details = 0, []
    if hiring:
        earned += points.trigger.hiring
        details.append(hiring[0].value)
    if news:
        earned += points.trigger.news
        details.append(news[0].value)
    add("trigger", earned, weights.trigger, "; ".join(details) or None, [f.id for f in hiring[:1] + news[:1]])

    # Reachability
    email = published_email(usable)
    phones = [f for f in usable if f.kind == "phone"]
    earned, details, ids = 0, [], []
    if email:
        earned += points.reach.verified_email
        details.append(f"published email {email.value}")
        ids.append(email.id)
    if phones:
        earned += points.reach.phone
        details.append(f"phone {phones[0].value}")
        ids.append(phones[0].id)
    if inputs.connection_degree in (1, 2):
        earned += points.reach.connection_or_open_profile
        details.append(f"{_ordinal(inputs.connection_degree)}-degree connection")
    add("reach", earned, weights.reach, "; ".join(details) or None, ids)

    # New in role
    months = inputs.months_at_company
    new = months is not None and months < points.new_in_role.within_months
    add("new_in_role", points.new_in_role.points if new else 0, weights.new_in_role,
        f"joined {months} months ago" if new else None)

    score = float(sum(f["points"] for f in factors))
    labels = points.labels
    label = "High" if score >= labels.high else "Medium" if score >= labels.medium else "Low"
    return ResponseResult(score=score, label=label, factors=factors)


def priority(match: float, response: float, config: Config) -> float:
    weights = config.icp.weights.priority
    return round(weights.match * match + weights.response * response, 1)


def response_score(ctx: RunContext) -> None:
    """Stage 8."""
    cards = latest_cards(ctx.session, ctx.run.id)
    facts = facts_for_run(ctx.session, ctx.run.id)
    scored = 0
    for score in ctx.session.exec(select(Score).where(Score.run_id == ctx.run.id)).all():
        if not score.gates_passed:
            continue
        lead = ctx.session.get(Lead, score.lead_id)
        inputs = response_inputs(ctx.session, lead, cards[lead.id], facts[lead.id], ctx.config)
        result = score_response(inputs, ctx.config)
        score.response_score = result.score
        score.response_label = result.label
        score.response_breakdown = result.breakdown()
        score.priority_score = priority(score.match_score, result.score, ctx.config)
        ctx.session.add(score)
        scored += 1
    ctx.session.commit()
    ctx.log.info("scored response likelihood and priority for %d leads", scored)


def response_inputs(session: Session, lead: Lead, card: Card, facts: LeadFacts, config: Config) -> ResponseInputs:
    posts = session.exec(select(Post).where(Post.lead_id == lead.id)).all() if lead.last_deep_read_at else []
    latest = max((p.posted_at for p in posts if p.posted_at), default=None)
    if latest:
        days = (datetime.now(UTC) - datetime.fromisoformat(latest)).days
    else:
        days = facts.posted_within_days  # the card's "recent posts" badge: within 30 days
    findings = session.exec(select(Finding).where(Finding.lead_id == lead.id)).all()
    return ResponseInputs(
        days_since_post=None if facts.no_recent_posts else days,
        post_date_exact=latest is not None,
        mutual_connections=card.mutual_connections or 0,
        connection_degree=card.connection_degree,
        shared_background=shared_background(lead, config),
        in_primary_area=_in_primary_area(facts, config),
        months_at_company=facts.months_at_company,
        findings=list(findings),
    )


def shared_background(lead: Lead, config: Config) -> list[str]:
    """Employers, schools, and communities the lead shares with the sender (sender.yaml)."""
    background = config.sender.background
    employers = {(r.get("company_name") or "").lower() for r in lead.experience or []}
    schools = {(e.get("school") or "").lower() for e in lead.education or []}
    text = " ".join(t for t in (lead.headline, lead.about) if t).lower()
    shared = [e for e in background.employers if e.lower() in employers]
    shared += [s for s in background.schools if s.lower() in schools]
    shared += [c for c in background.communities if c.lower() in text]
    return shared


def _in_primary_area(facts: LeadFacts, config: Config) -> bool:
    geography = next(c for c in score_match(facts, config.icp).criteria if c.criterion == "geography")
    return geography.level == "full"


def _within_days(event_date: str | None, days: int) -> bool:
    if not event_date:
        return False
    try:
        when = date.fromisoformat(event_date if len(event_date) == 10 else f"{event_date}-01")
    except ValueError:
        return False
    return (date.today() - when).days <= days


def _ops_role(text: str, terms: list[str]) -> bool:
    """PRD: the trigger is the company hiring ops or logistics roles, not any opening."""
    lowered = text.lower()
    return any(re.search(rf"\b{re.escape(term.lower())}", lowered) for term in terms)


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def _ordinal(n: int | None) -> str:
    return {1: "1st", 2: "2nd", 3: "3rd"}.get(n, str(n))
