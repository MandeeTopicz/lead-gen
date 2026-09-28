"""Orchestrates stages 1-10 under a single-run lock, recording each stage so halted runs can resume."""

import fcntl
import logging
import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from sqlmodel import Session, col, func, select

from db import make_engine
from db.models import Run, RunStage
from pipeline.config import Config, load_config
from pipeline.context import Budget, Halt, Paths, RunContext, now_iso
from pipeline.stages import SESSION_CHECK, STAGES, Stage


class RunRefused(Exception):
    """The run can't start: another run holds the lock, the daily cap is used, or resume isn't possible."""


@contextmanager
def run_lock(path: Path) -> Iterator[None]:
    """Exclusive, non-blocking lock. The OS releases it if the process dies, so it never goes stale."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise RunRefused("another run is in progress") from None
    try:
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def start_run(
    paths: Paths,
    trigger: str = "manual",
    dry_run: bool = False,
    stages: Sequence[Stage] = STAGES,
) -> Run:
    config = load_config(paths.config_dir)
    engine = make_engine(paths.db_path)
    with run_lock(paths.lock_path), Session(engine, expire_on_commit=False) as session:
        _fail_orphaned_runs(session)
        if not dry_run:
            _check_daily_cap(session, config, trigger)
        run = Run(
            started_at=now_iso(),
            trigger=trigger,
            status="running",
            dry_run=dry_run,
            icp_version=config.icp.version_tag,
        )
        session.add(run)
        session.commit()
        _execute(config, paths, session, run, list(stages))
        return run


def resume_run(
    paths: Paths,
    run_id: int | None = None,
    from_stage: int | None = None,
    stages: Sequence[Stage] = STAGES,
) -> Run:
    config = load_config(paths.config_dir)
    engine = make_engine(paths.db_path)
    with run_lock(paths.lock_path), Session(engine, expire_on_commit=False) as session:
        _fail_orphaned_runs(session)
        run = _find_resumable(session, run_id)
        if run.icp_version != config.icp.version_tag:
            raise RunRefused(
                f"run {run.id} used {run.icp_version} but config is now {config.icp.version_tag}; start a new run"
            )
        if from_stage is None:
            done = set(
                session.exec(
                    select(RunStage.stage).where(RunStage.run_id == run.id, RunStage.status == "completed")
                )
            )
            from_stage = next((s.number for s in stages if s.number not in done), None)
            if from_stage is None:
                raise RunRefused(f"run {run.id} has no unfinished stages")

        plan = [s for s in stages if s.number >= from_stage]
        # A new process has no browser session, so later LinkedIn stages need the session check again.
        if from_stage > SESSION_CHECK and any(s.uses_linkedin for s in plan):
            plan = [s for s in stages if s.number == SESSION_CHECK] + plan

        run.status = "running"
        run.finished_at = None
        run.halt_reason = None
        run.halt_screenshot_path = None
        session.add(run)
        session.commit()
        _execute(config, paths, session, run, plan)
        return run


def _execute(config: Config, paths: Paths, session: Session, run: Run, plan: list[Stage]) -> None:
    ctx = RunContext(
        config=config,
        paths=paths,
        session=session,
        run=run,
        budget=Budget(run, config.icp.caps),
        log=_run_logger(paths, run),
    )
    try:
        for stage in plan:
            record = session.get(RunStage, (run.id, stage.number)) or RunStage(
                run_id=run.id, stage=stage.number, name=stage.name, status="running", started_at=now_iso()
            )
            record.status, record.started_at, record.finished_at, record.error = "running", now_iso(), None, None
            session.add(record)
            session.commit()
            ctx.log.info("stage %d %s: start", stage.number, stage.name)

            try:
                stage.run(ctx)
            except Halt as halt:
                _rollback_keeping_counters(session, run)
                _finish_stage(session, record, "halted", halt.reason)
                run.status, run.halt_reason = "halted", halt.reason
                run.halt_screenshot_path = str(halt.screenshot_path) if halt.screenshot_path else None
                _finish_run(session, run)
                ctx.log.warning("stage %d %s: HALTED: %s", stage.number, stage.name, halt.reason)
                return
            except Exception as exc:
                _rollback_keeping_counters(session, run)
                _finish_stage(session, record, "failed", repr(exc))
                run.status, run.halt_reason = "failed", f"stage {stage.number} {stage.name} failed: {exc!r}"
                _finish_run(session, run)
                ctx.log.exception("stage %d %s: FAILED", stage.number, stage.name)
                raise

            _finish_stage(session, record, "completed")
            session.add(run)  # budget counters
            session.commit()
            ctx.log.info("stage %d %s: done", stage.number, stage.name)

        run.status = "completed"
        _finish_run(session, run)
        ctx.log.info(
            "run %d completed: %d pages, %d deep reads, $%.2f LLM, $%.2f enrichment",
            run.id, run.pages_viewed, run.profiles_read, run.llm_cost_usd, run.enrichment_cost_usd,
        )
    finally:
        for handler in list(ctx.log.handlers):
            handler.close()
            ctx.log.removeHandler(handler)


COUNTERS = ("pages_viewed", "profiles_read", "llm_cost_usd", "enrichment_cost_usd")


def _rollback_keeping_counters(session: Session, run: Run) -> None:
    """Undo the stage's partial writes, but keep what it spent: those pages were really viewed."""
    spent = {name: getattr(run, name) for name in COUNTERS}
    session.rollback()
    for name, value in spent.items():
        setattr(run, name, value)


def _finish_stage(session: Session, record: RunStage, status: str, error: str | None = None) -> None:
    record.status, record.finished_at, record.error = status, now_iso(), error
    session.add(record)
    session.commit()


def _finish_run(session: Session, run: Run) -> None:
    run.finished_at = now_iso()
    session.add(run)
    session.commit()


def _fail_orphaned_runs(session: Session) -> None:
    """Runs still marked running while we hold the lock belong to a process that died."""
    for run in session.exec(select(Run).where(Run.status == "running")).all():
        run.status, run.finished_at = "failed", now_iso()
        run.halt_reason = "process exited mid-run"
        session.add(run)
        for record in session.exec(
            select(RunStage).where(RunStage.run_id == run.id, RunStage.status == "running")
        ).all():
            record.status, record.finished_at, record.error = "failed", now_iso(), "process exited mid-run"
            session.add(record)
    session.commit()


def _check_daily_cap(session: Session, config: Config, trigger: str) -> None:
    caps = config.icp.caps
    limit = caps.scheduled_runs_per_day if trigger == "cron" else caps.manual_runs_per_day
    today = now_iso()[:10]
    count = session.exec(
        select(func.count())
        .select_from(Run)
        .where(Run.trigger == trigger, Run.dry_run == False, col(Run.started_at).startswith(today))  # noqa: E712
    ).one()
    if count >= limit:
        raise RunRefused(f"daily cap reached: {count}/{limit} {trigger} runs already started today")


def _find_resumable(session: Session, run_id: int | None) -> Run:
    if run_id is None:
        run = session.exec(
            select(Run).where(col(Run.status).in_(["halted", "failed"])).order_by(col(Run.id).desc())
        ).first()
        if run is None:
            raise RunRefused("no halted or failed run to resume")
        return run
    run = session.get(Run, run_id)
    if run is None:
        raise RunRefused(f"run {run_id} not found")
    if run.status not in ("halted", "failed"):
        raise RunRefused(f"run {run_id} is {run.status}; only halted or failed runs can resume")
    return run


def _run_logger(paths: Paths, run: Run) -> logging.Logger:
    log_dir = paths.run_output_dir(run)
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"leadgen.run.{run.id}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    formatter = logging.Formatter(f"%(asctime)s %(levelname)s run {run.id}: %(message)s", "%H:%M:%S")
    for handler in (logging.FileHandler(log_dir / "run.log"), logging.StreamHandler()):
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger
