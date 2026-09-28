"""Where a run's time and Claude spend went: by stage, by lead, and each lead's research trail."""

from dataclasses import dataclass
from datetime import datetime

from sqlmodel import Session, col, select

from db.models import Lead, LlmCall, RunStage


@dataclass
class StageLine:
    stage: int
    name: str
    status: str
    seconds: float | None
    cost_usd: float


@dataclass
class LeadCost:
    lead: str
    research_usd: float
    drafting_usd: float
    searches: int
    pages_read: int

    @property
    def total_usd(self) -> float:
        return self.research_usd + self.drafting_usd


STAGE_OF_CALL = {"research": 7, "style": 9, "drafting": 9}


def stage_lines(session: Session, run_id: int) -> list[StageLine]:
    calls = session.exec(select(LlmCall).where(LlmCall.run_id == run_id)).all()
    cost: dict[int, float] = {}
    for call in calls:
        stage = STAGE_OF_CALL.get(call.stage)
        cost[stage] = cost.get(stage, 0.0) + call.cost_usd
    lines = []
    for record in session.exec(select(RunStage).where(RunStage.run_id == run_id).order_by(col(RunStage.stage))):
        seconds = None
        if record.started_at:
            end = datetime.fromisoformat(record.finished_at) if record.finished_at else datetime.now().astimezone()
            seconds = (end - datetime.fromisoformat(record.started_at)).total_seconds()
        lines.append(StageLine(record.stage, record.name, record.status, seconds, cost.get(record.stage, 0.0)))
    return lines


def lead_costs(session: Session, run_id: int) -> list[LeadCost]:
    by_lead: dict[int, LeadCost] = {}
    for call in session.exec(select(LlmCall).where(LlmCall.run_id == run_id, col(LlmCall.lead_id).is_not(None))):
        if call.lead_id not in by_lead:
            lead = session.get(Lead, call.lead_id)
            by_lead[call.lead_id] = LeadCost(lead.full_name if lead else "?", 0.0, 0.0, 0, 0)
        line = by_lead[call.lead_id]
        if call.stage == "research":
            line.research_usd += call.cost_usd
            line.searches += call.web_searches
            line.pages_read += call.web_fetches
        else:
            line.drafting_usd += call.cost_usd
    return sorted(by_lead.values(), key=lambda c: c.total_usd, reverse=True)


def research_trail(session: Session, run_id: int, lead_id: int) -> dict | None:
    """What Claude searched and read for one lead in one run, with tokens, time, and cost."""
    calls = session.exec(
        select(LlmCall).where(LlmCall.run_id == run_id, LlmCall.lead_id == lead_id, LlmCall.stage == "research")
        .order_by(col(LlmCall.id))
    ).all()
    if not calls:
        return None
    return {
        "model": calls[0].model,
        "queries": [q for c in calls for q in (c.detail or {}).get("queries", [])],
        "fetched": [u for c in calls for u in (c.detail or {}).get("fetched", [])],
        "tokens_in": sum(c.input_tokens + c.cache_read_tokens + c.cache_write_tokens for c in calls),
        "tokens_cached": sum(c.cache_read_tokens for c in calls),
        "tokens_out": sum(c.output_tokens for c in calls),
        "cost_usd": sum(c.cost_usd for c in calls),
        "seconds": sum(c.seconds for c in calls),
    }


def duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m {secs:02d}s" if minutes else f"{secs}s"
