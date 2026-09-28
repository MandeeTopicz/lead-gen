"""Every claim in a reason or a draft must cite a stored source. Research findings already are one; this turns
what LinkedIn showed (current role, posts, mutual connections, the company page) into findings too, so reasons
and drafts cite everything the same way: by finding id.
"""

from sqlmodel import Session, col, delete, select

from db.models import Company, Finding, Lead, Post
from pipeline.config import Config
from pipeline.context import now_iso
from pipeline.linkedin.cards import Card, canonical_company_url
from pipeline.scoring.response import shared_background

LINKEDIN = "linkedin"


def refresh_linkedin_evidence(session: Session, run_id: int, lead: Lead, card: Card, company: Company | None,
                              config: Config) -> None:
    session.exec(delete(Finding).where(Finding.lead_id == lead.id, Finding.source_provider == LINKEDIN))
    profile = lead.profile_url
    found_at = now_iso()

    def add(kind: str, value: str, source: str | None = profile, event_date: str | None = None) -> None:
        session.add(Finding(lead_id=lead.id, company_id=company.id if company and kind == "company" else None,
                            kind=kind, value=value, source_url=source, source_provider=LINKEDIN,
                            confidence="verified", event_date=event_date, found_at=found_at, run_id=run_id))

    current = next((r for r in lead.experience or [] if not r.get("end")), None)
    since = f" since {current['start']}" if current and current.get("start") else ""
    if card.title:
        add("role", f"{card.title} at {card.company_name}{since}")
    if card.location:
        add("location", f"Based in {card.location}")
    if lead.headline:
        add("profile", f"LinkedIn headline: {lead.headline}")
    if lead.about:
        add("profile", f"LinkedIn About: {lead.about[:700]}")
    for post in session.exec(select(Post).where(Post.lead_id == lead.id).order_by(col(Post.posted_at).desc())).all()[:5]:
        if post.text:
            add("post", f"LinkedIn post: {post.text[:500]}", event_date=(post.posted_at or "")[:10] or None)
    if card.mutual_connections:
        add("affinity", f"{card.mutual_connections} mutual connections on LinkedIn")
    for shared in shared_background(lead, config):
        add("affinity", f"Shared background with the sender: {shared}")
    if company is not None:
        company_url = canonical_company_url(card.company_url) or company.linkedin_url
        details = [company.industry, f"{company.headcount} employees on LinkedIn" if company.headcount else None,
                   f"headquartered in {company.hq_location}" if company.hq_location else None]
        if any(details):
            add("company", f"{company.name}: " + ", ".join(d for d in details if d), source=company_url)
        if company.headcount_growth_6mo:
            direction = "grew" if company.headcount_growth_6mo > 0 else "shrank"
            add("company", f"{company.name} headcount {direction} {abs(company.headcount_growth_6mo):.0%} "
                           "in the last six months", source=company_url)
    session.flush()


def usable_evidence(session: Session, lead: Lead) -> list[Finding]:
    """Findings a draft may cite: everything stored for the lead except unverified ones."""
    return list(session.exec(
        select(Finding).where(Finding.lead_id == lead.id, Finding.confidence != "unverified").order_by(col(Finding.id))
    ).all())


def evidence_list(findings: list[Finding]) -> str:
    """The numbered evidence block drafts cite from."""
    lines = []
    for f in findings:
        meta = ", ".join(x for x in (f.confidence, f.event_date) if x)
        lines.append(f"[{f.id}] {f.kind} ({meta}): {f.value}")
    return "\n".join(lines)
