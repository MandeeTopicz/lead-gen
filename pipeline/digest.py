"""The run digest: apply the quality bar, hold back repeat leads, rank by priority, write digest.md.

A lead is held back if you marked it not a fit, it's in an outreach sequence, or it was in a digest within
`quality_bar.suppress_days` (or finished a sequence), unless something new happened since: a new post, a job
change, or new hiring or news. Written by stage 10; dossiers join it in iteration 8.
"""

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlmodel import Session, col, select

from db.models import Company, Finding, Lead, Post, Review, Run, Score, SearchHit
from pipeline.collect import latest_cards
from pipeline.config import Config
from pipeline.context import Paths, RunContext


@dataclass
class DigestRow:
    score: Score
    lead: Lead
    company: Company | None
    url: str  # the Sales Navigator link from this run's result card


@dataclass
class Digest:
    run: Run
    rows: list[DigestRow]
    held_back: list[tuple[Lead, str]]
    counts: dict[str, int]
    cuts: list[tuple[str, int]]  # what removed the most leads, when fewer than warn_below pass
    min_match: int
    warn_below: int


def build_digest(session: Session, run: Run, config: Config) -> Digest:
    bar = config.icp.quality_bar
    scores = session.exec(select(Score).where(Score.run_id == run.id)).all()
    passed = [s for s in scores if s.gates_passed]
    above = [s for s in passed if (s.match_score or 0) >= bar.min_match]

    cards = latest_cards(session, run.id)
    rows, held_back = [], []
    for score in above:
        lead = session.get(Lead, score.lead_id)
        reason = hold_back_reason(session, lead, run, bar.suppress_days)
        if reason:
            held_back.append((lead, reason))
            continue
        company = session.get(Company, lead.company_id) if lead.company_id else None
        card = cards.get(lead.id)
        rows.append(DigestRow(score, lead, company, card.profile_url if card else lead.profile_url))
    rows.sort(key=lambda r: (r.score.priority_score or 0, r.score.match_score or 0), reverse=True)
    rows = rows[: bar.max_leads]

    counts = {"found": len(scores), "passed_gates": len(passed), "above_bar": len(above),
              "held_back": len(held_back), "in_digest": len(rows)}
    cuts = what_cut_leads(scores, bar.min_match) if len(rows) < bar.warn_below else []
    return Digest(run, rows, held_back, counts, cuts, bar.min_match, bar.warn_below)


def hold_back_reason(session: Session, lead: Lead, run: Run, suppress_days: int) -> str | None:
    review = session.get(Review, lead.id)
    if review and review.fit == "not_fit":
        return "marked not a fit"
    if review and review.sequence_status in ("active", "replied", "stopped"):
        return f"outreach {review.sequence_status}"
    previous = session.get(Run, lead.last_in_digest_run_id) if lead.last_in_digest_run_id else None
    if previous is None or previous.id == run.id:
        return None
    since = datetime.fromisoformat(previous.started_at)
    recent = datetime.fromisoformat(run.started_at) - since < timedelta(days=suppress_days)
    if not recent and not (review and review.sequence_status == "done"):
        return None
    if something_new_since(session, lead, previous.started_at):
        return None
    return f"in the digest on {previous.started_at[:10]}, nothing new since"


def something_new_since(session: Session, lead: Lead, since: str) -> bool:
    when = datetime.fromisoformat(since)
    posts = session.exec(select(Post.posted_at).where(Post.lead_id == lead.id)).all()
    if any(p and datetime.fromisoformat(p) > when for p in posts):
        return True
    if lead.started_current_role and lead.started_current_role > since[:10]:
        return True
    triggers = session.exec(
        select(Finding.found_at).where(Finding.lead_id == lead.id, col(Finding.kind).in_(["hiring", "news"]))
    ).all()
    return any(datetime.fromisoformat(t) > when for t in triggers)


def what_cut_leads(scores: list[Score], min_match: int) -> list[tuple[str, int]]:
    """Why leads didn't make it, most common first, so you can decide what to loosen."""
    reasons: Counter[str] = Counter()
    for score in scores:
        breakdown = score.match_breakdown or {}
        if breakdown.get("excluded"):
            reasons[f"excluded: {_reason_kind(breakdown['excluded'])}"] += 1
        elif breakdown.get("gate_failures"):
            for failure in breakdown["gate_failures"]:
                reasons[f"gate: {_reason_kind(failure)}"] += 1
        elif (score.match_score or 0) < min_match:
            weakest = min(
                (c for c in breakdown.get("criteria", []) if c["max"]),
                key=lambda c: c["points"] / c["max"],
                default=None,
            )
            reasons[f"match below {min_match}, weakest on {weakest['criterion'] if weakest else '?'}"] += 1
    return reasons.most_common(6)


def write_digest(paths: Paths, digest: Digest, session: Session) -> Path:
    out_dir = paths.run_output_dir(digest.run)
    out_dir.mkdir(parents=True, exist_ok=True)
    markdown = render(digest, session)
    path = out_dir / f"digest-run{digest.run.id}.md"
    path.write_text(markdown)
    (out_dir / "digest.md").write_text(markdown)  # the day's latest
    return path


def render(digest: Digest, session: Session) -> str:
    run, c = digest.run, digest.counts
    per_search = Counter(session.exec(select(SearchHit.search_code).where(SearchHit.run_id == run.id)).all())
    lines = [
        f"# Lead digest: {run.started_at[:10]} (run {run.id})",
        "",
        f"- **Run:** {run.trigger}, started {run.started_at[11:16]}, ICP {run.icp_version}",
        "- **Searches:** " + (", ".join(f"{code} ({n} cards)" for code, n in sorted(per_search.items())) or "none"),
        f"- **Leads:** {c['found']} found, {c['passed_gates']} passed gates, {c['above_bar']} at match "
        f"{digest.min_match}+, {c['held_back']} held back, **{c['in_digest']} in this digest**",
        f"- **Spend:** {run.pages_viewed} result pages, {run.profiles_read} deep reads, "
        f"${run.llm_cost_usd:.2f} Claude",
        "",
    ]
    if digest.rows:
        lines += [
            "| # | Lead | Title | Company | Match | Response | Priority | Top reason |",
            "| ---: | --- | --- | --- | ---: | --- | ---: | --- |",
        ]
        for rank, row in enumerate(digest.rows, 1):
            s = row.score
            lines.append(
                f"| {rank} | [{_cell(row.lead.full_name)}]({row.url}) | {_cell(row.lead.current_title)} "
                f"| {_cell(row.company.name if row.company else None)} | {s.match_score:.0f}% "
                f"| {s.response_score:.0f}% {s.response_label} | {s.priority_score:.0f} | {_cell(top_reason(s))} |"
            )
    else:
        lines.append("_No leads cleared the bar this run._")
    if digest.cuts:
        lines += ["", f"## Fewer than {digest.warn_below} leads: what cut them", ""]
        lines += [f"- {reason}: {n}" for reason, n in digest.cuts]
    if digest.held_back:
        lines += ["", "## Held back", ""]
        lines += [f"- {_cell(lead.full_name)}: {reason}" for lead, reason in digest.held_back]
    return "\n".join(lines) + "\n"


def top_reason(score: Score) -> str:
    """The single most useful reason to reach out now, from the response and match breakdowns."""
    factors = {f["factor"]: f for f in (score.response_breakdown or {}).get("factors", [])}
    for name in ("trigger", "new_in_role", "activity", "affinity"):
        factor = factors.get(name)
        if factor and factor["points"] and factor["detail"]:
            if name == "activity" and factor["points"] < factor["max"] * 0.7:
                continue  # only a very recent post leads
            return _sentence(factor["detail"].split("; ")[0])
    criteria = {c["criterion"]: c for c in (score.match_breakdown or {}).get("criteria", [])}
    fits = [criteria[k]["value"] for k in ("role", "geography") if criteria.get(k, {}).get("value")]
    return "Strong fit: " + ", ".join(fits) if fits else "Strong ICP fit"


def digest_stage(ctx: RunContext) -> None:
    """Stage 10 (digest now; dossiers arrive in iteration 8)."""
    digest = build_digest(ctx.session, ctx.run, ctx.config)
    for row in digest.rows:
        row.lead.last_in_digest_run_id = ctx.run.id
        ctx.session.add(row.lead)
    ctx.run.leads_qualified = len(digest.rows)
    ctx.session.add(ctx.run)
    ctx.session.commit()
    path = write_digest(ctx.paths, digest, ctx.session)
    ctx.log.info("digest: %d leads -> %s", len(digest.rows), path)


def _reason_kind(text: str) -> str:
    """'title contains 'assistant'' -> same; 'director-level or below (Director of Ops)' -> without the example."""
    return text.split(" (")[0]


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:]


def _cell(value: object) -> str:
    return "-" if value in (None, "") else str(value).replace("|", "\\|").replace("\n", " ")
