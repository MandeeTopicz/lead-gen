"""`leadgen report`: a ranked Markdown table of a run's match scores, with each lead's breakdown."""

from sqlmodel import Session, col, select

from db.models import Company, Lead, Run, Score, SearchHit
from pipeline.context import Paths


class NoScores(Exception):
    pass


def match_report(session: Session, run_id: int | None = None, details: bool = False) -> tuple[Run, str]:
    run = _pick_run(session, run_id)
    rows = session.exec(
        select(Score, Lead)
        .join(Lead, col(Lead.id) == col(Score.lead_id))
        .where(Score.run_id == run.id)
        .order_by(col(Score.gates_passed).desc(), col(Score.match_score).desc(), col(Lead.full_name))
    ).all()
    searches = _searches_by_lead(session, run.id)

    passed = [(s, lead) for s, lead in rows if s.gates_passed]
    dropped = [(s, lead) for s, lead in rows if not s.gates_passed]
    lines = [
        f"# Match report: run {run.id}",
        "",
        f"{run.started_at[:16].replace('T', ' ')} · {run.icp_version} · {len(rows)} leads · "
        f"{len(passed)} passed gates · {len(dropped)} dropped",
        "",
        "| # | Name | Title | Company | Location | Match | Searches | Unknown |",
        "| ---: | --- | --- | --- | --- | ---: | --- | --- |",
    ]
    for rank, (score, lead) in enumerate(passed, 1):
        unknown = [c["criterion"] for c in score.match_breakdown["criteria"] if c["level"] == "unknown"]
        lines.append(
            f"| {rank} | {_cell(lead.full_name)} | {_cell(lead.current_title)} | {_cell(_company(session, lead))} "
            f"| {_cell(lead.location)} | {score.match_score:.0f} | {', '.join(searches.get(lead.id, []))} "
            f"| {', '.join(unknown) or '-'} |"
        )
    if dropped:
        lines += ["", "## Dropped", "", "| Name | Title | Reason |", "| --- | --- | --- |"]
        for score, lead in dropped:
            breakdown = score.match_breakdown
            reason = breakdown["excluded"] or "; ".join(breakdown["gate_failures"])
            lines.append(f"| {_cell(lead.full_name)} | {_cell(lead.current_title)} | {_cell(reason)} |")
    if details:
        lines += ["", "## Breakdowns"]
        for rank, (score, lead) in enumerate(passed, 1):
            lines += ["", f"### {rank}. {lead.full_name} — {score.match_score:.0f}", "", lead.profile_url, ""]
            lines += ["| Criterion | Level | Points | Evidence | Source |", "| --- | --- | ---: | --- | --- |"]
            for c in score.match_breakdown["criteria"]:
                lines.append(
                    f"| {c['criterion']} | {c['level']} | {c['points']}/{c['max']} | {_cell(c['value'])} "
                    f"| {c['source'] or '-'} |"
                )
    return run, "\n".join(lines) + "\n"


def write_report(paths: Paths, run: Run, markdown: str) -> str:
    out_dir = paths.run_output_dir(run)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"match-report-run{run.id}.md"
    path.write_text(markdown)
    return str(path)


def _pick_run(session: Session, run_id: int | None) -> Run:
    if run_id is not None:
        run = session.get(Run, run_id)
        if run is None:
            raise NoScores(f"run {run_id} not found")
        return run
    run = session.exec(
        select(Run).join(Score, col(Score.run_id) == col(Run.id)).order_by(col(Run.id).desc())
    ).first()
    if run is None:
        raise NoScores("no run has scored leads yet")
    return run


def _searches_by_lead(session: Session, run_id: int) -> dict[int, list[str]]:
    found: dict[int, list[str]] = {}
    for hit in session.exec(select(SearchHit).where(SearchHit.run_id == run_id).order_by(col(SearchHit.search_code))):
        found.setdefault(hit.lead_id, []).append(hit.search_code)
    return found


def _company(session: Session, lead: Lead) -> str | None:
    company = session.get(Company, lead.company_id) if lead.company_id else None
    return company.name if company else None


def _cell(value: object) -> str:
    return "-" if value in (None, "") else str(value).replace("|", "\\|").replace("\n", " ")
