"""Data behind the review screen: runs, a run's digest leads with their dossiers and drafts, your marks, and
starting a run in the background. Kept out of the Streamlit file so it's testable."""

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from sqlmodel import Session, col, select

from db.models import Company, Dossier, Lead, OutreachStep, Review, Run, Score
from pipeline.context import Paths, now_iso
from pipeline.run import RunRefused, _fail_orphaned_runs, run_lock

FITS = ("good", "not_fit")
SEQUENCE_STATUSES = ("active", "replied", "stopped", "done")


@dataclass
class ReviewLead:
    rank: int
    lead: Lead
    company: Company | None
    score: Score
    dossier: Dossier
    steps: list[OutreachStep]
    review: Review | None


def runs(session: Session, limit: int = 50) -> list[Run]:
    return list(session.exec(select(Run).order_by(col(Run.id).desc()).limit(limit)).all())


def runs_with_digest(session: Session) -> list[Run]:
    ids = set(session.exec(select(Dossier.run_id)).all())
    return [r for r in runs(session, 200) if r.id in ids]


def digest_leads(session: Session, run_id: int) -> list[ReviewLead]:
    out = []
    for dossier in session.exec(select(Dossier).where(Dossier.run_id == run_id).order_by(col(Dossier.rank))).all():
        lead = session.get(Lead, dossier.lead_id)
        out.append(ReviewLead(
            rank=dossier.rank,
            lead=lead,
            company=session.get(Company, lead.company_id) if lead.company_id else None,
            score=session.get(Score, (run_id, lead.id)),
            dossier=dossier,
            steps=list(session.exec(
                select(OutreachStep).where(OutreachStep.run_id == run_id, OutreachStep.lead_id == lead.id)
                .order_by(col(OutreachStep.id))
            ).all()),
            review=session.get(Review, lead.id),
        ))
    return out


def save_review(session: Session, lead_id: int, *, fit: str | None = None, sequence_status: str | None = None,
                contacted: bool | None = None, notes: str | None = None, clear: tuple[str, ...] = ()) -> Review:
    """Record your marks on a lead. Marks persist across runs: a not-fit or active lead stays out of digests."""
    if fit is not None and fit not in FITS:
        raise ValueError(f"fit must be one of {FITS}")
    if sequence_status is not None and sequence_status not in SEQUENCE_STATUSES:
        raise ValueError(f"sequence_status must be one of {SEQUENCE_STATUSES}")
    review = session.get(Review, lead_id) or Review(lead_id=lead_id)
    if fit is not None:
        review.fit = fit
    if sequence_status is not None:
        review.sequence_status = sequence_status
        review.contacted = True
    if contacted is not None:
        review.contacted = contacted
    if notes is not None:
        review.notes = notes
    for field in clear:
        setattr(review, field, None)
    review.updated_at = now_iso()
    session.add(review)
    session.commit()
    session.refresh(review)
    return review


def start_run_in_background(paths: Paths) -> Path:
    """Start `leadgen run` detached, so it keeps going after the page reloads. Its output goes to a log file;
    refusals (another run in progress, daily cap) show up there and in run history."""
    log = paths.data_dir / "manual-run.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as handle:
        handle.write(f"\n--- run requested from the review screen at {now_iso()} ---\n")
        handle.flush()
        subprocess.Popen(
            [sys.executable, "-m", "pipeline.cli", "run"],
            cwd=paths.root, stdout=handle, stderr=subprocess.STDOUT, start_new_session=True,
        )
    return log


def run_in_progress(session: Session, paths: Paths) -> Run | None:
    """The run that's actually going. A run still marked running with no process holding the run lock was
    stopped or crashed; it's marked failed here so it doesn't block starting a new one."""
    try:
        with run_lock(paths.lock_path):
            _fail_orphaned_runs(session)
        return None
    except RunRefused:
        return session.exec(select(Run).where(Run.status == "running").order_by(col(Run.id).desc())).first()


def log_tail(paths: Paths, run: Run, lines: int = 30) -> list[str]:
    """The latest lines this run wrote to its log."""
    log = paths.run_output_dir(run) / "run.log"
    if not log.exists():
        return []
    marker = f" run {run.id}: "
    mine = [line.rstrip() for line in log.read_text().splitlines() if marker in line]
    return [line.replace(marker, " ", 1) for line in mine[-lines:]]
