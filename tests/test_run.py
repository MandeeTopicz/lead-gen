import pytest
import yaml
from sqlmodel import Session, col, select

from db import make_engine
from db.models import Run, RunStage
from pipeline.context import CapReached, Halt, now_iso
from pipeline.run import RunRefused, resume_run, run_lock, start_run
from pipeline.stages import STAGES, Stage


def noop(ctx):
    pass


def make_stages(**overrides):
    """The real stage list with some stage functions swapped out by number, e.g. make_stages(s3=fn)."""
    return [
        Stage(s.number, s.name, overrides.get(f"s{s.number}", noop), s.uses_linkedin) for s in STAGES
    ]


def stage_rows(paths, run_id):
    with Session(make_engine(paths.db_path)) as session:
        rows = session.exec(
            select(RunStage).where(RunStage.run_id == run_id).order_by(col(RunStage.stage))
        ).all()
        return {r.stage: r.status for r in rows}


def load_run(paths, run_id):
    with Session(make_engine(paths.db_path)) as session:
        return session.get(Run, run_id)


def test_dry_run_completes_all_ten_stages(paths):
    run = start_run(paths, dry_run=True)
    assert run.status == "completed"
    assert run.finished_at is not None
    assert run.icp_version == "austin-logistics-ops-leaders@v1"
    assert stage_rows(paths, run.id) == {n: "completed" for n in range(1, 11)}
    assert (paths.output_dir / run.started_at[:10] / "run.log").exists()


def test_second_run_is_refused_while_lock_is_held(paths):
    with run_lock(paths.lock_path):
        with pytest.raises(RunRefused, match="another run is in progress"):
            start_run(paths, dry_run=True, stages=make_stages())
    # Lock is released afterwards.
    assert start_run(paths, dry_run=True, stages=make_stages()).status == "completed"


def test_manual_runs_are_capped_per_day_and_dry_runs_do_not_count(paths):
    stages = make_stages(s3=lambda ctx: ctx.budget.use_page())  # runs that read search results
    start_run(paths, stages=make_stages())  # read no results (e.g. stopped at login): not counted
    start_run(paths, dry_run=True, stages=stages)
    start_run(paths, stages=stages)
    start_run(paths, stages=stages)
    with pytest.raises(RunRefused, match="2/2 manual"):
        start_run(paths, stages=stages)
    # Cron has its own cap.
    start_run(paths, trigger="cron", stages=stages)
    with pytest.raises(RunRefused, match="1/1 cron"):
        start_run(paths, trigger="cron", stages=stages)


def test_halt_marks_run_and_stage_and_skips_the_rest(paths):
    def warning_page(ctx):
        raise Halt("LinkedIn warning banner", paths.root / "halt.png")

    reached = []
    run = start_run(paths, stages=make_stages(s3=warning_page, s4=lambda ctx: reached.append(4)))
    assert run.status == "halted"
    assert run.halt_reason == "LinkedIn warning banner"
    assert run.halt_screenshot_path.endswith("halt.png")
    assert stage_rows(paths, run.id) == {1: "completed", 2: "completed", 3: "halted"}
    assert reached == []


def test_failure_rolls_back_stage_writes_but_keeps_spend(paths):
    def crashes_after_viewing_pages(ctx):
        ctx.budget.use_page()
        ctx.budget.use_page()
        ctx.run.leads_found = 99  # a partial write that should not survive
        raise RuntimeError("parser broke")

    with pytest.raises(RuntimeError, match="parser broke"):
        start_run(paths, stages=make_stages(s3=crashes_after_viewing_pages))

    run = load_run(paths, 1)
    assert run.status == "failed"
    assert "parser broke" in run.halt_reason
    assert run.pages_viewed == 2
    assert run.leads_found == 0
    assert stage_rows(paths, 1)[3] == "failed"


def test_budget_raises_cap_reached(paths):
    caps_seen = []

    def page_through(ctx):
        try:
            while True:
                ctx.budget.use_page()
        except CapReached as cap:
            caps_seen.append(cap.cap)

    run = start_run(paths, stages=make_stages(s3=page_through))
    assert run.status == "completed"
    assert run.pages_viewed == 30
    assert caps_seen == ["result_pages"]


def test_resume_continues_from_halted_stage_and_rechecks_session(paths):
    attempts = {"s6": 0}
    calls = []

    def flaky_deep_read(ctx):
        attempts["s6"] += 1
        if attempts["s6"] == 1:
            raise Halt("verification prompt")

    def record(n):
        return lambda ctx: calls.append(n)

    stages = make_stages(s2=record(2), s4=record(4), s6=flaky_deep_read, s7=record(7))
    halted = start_run(paths, stages=stages)
    assert halted.status == "halted"
    calls.clear()

    resumed = resume_run(paths, stages=stages)
    assert resumed.id == halted.id
    assert resumed.status == "completed"
    assert resumed.halt_reason is None
    assert calls == [2, 7]  # session check again, stage 6 retried, stage 4 not repeated
    assert stage_rows(paths, halted.id) == {n: "completed" for n in range(1, 11)}


def test_resume_from_explicit_stage(paths):
    calls = []
    stages = make_stages(s8=lambda ctx: calls.append(8), s9=lambda ctx: (_ for _ in ()).throw(ValueError("x")))
    with pytest.raises(ValueError):
        start_run(paths, stages=stages)
    calls.clear()
    stages = make_stages(s8=lambda ctx: calls.append(8))
    resume_run(paths, run_id=1, from_stage=8, stages=stages)
    assert calls == [8]  # stages 7+ don't touch LinkedIn, so no session re-check


def test_resume_refuses_completed_runs_and_changed_icp(paths):
    run = start_run(paths, stages=make_stages())
    with pytest.raises(RunRefused, match="only halted or failed"):
        resume_run(paths, run_id=run.id)  # completed: needs an explicit --from-stage
    with pytest.raises(RunRefused, match="no halted or failed run"):
        resume_run(paths)

    halted = start_run(paths, stages=make_stages(s5=lambda ctx: (_ for _ in ()).throw(Halt("stop"))))
    icp_path = paths.config_dir / "icp.yaml"
    data = yaml.safe_load(icp_path.read_text())
    data["icp"]["version"] = 2
    icp_path.write_text(yaml.safe_dump(data))
    with pytest.raises(RunRefused, match="start a new run"):
        resume_run(paths, run_id=halted.id)


def test_orphaned_running_runs_are_marked_failed(paths):
    engine = make_engine(paths.db_path)
    with Session(engine) as session:
        session.add(Run(started_at=now_iso(), trigger="manual", status="running", icp_version="x@v1"))
        session.add(RunStage(run_id=1, stage=3, name="searches", status="running", started_at=now_iso()))
        session.commit()

    start_run(paths, dry_run=True, stages=make_stages())

    orphan = load_run(paths, 1)
    assert orphan.status == "failed"
    assert orphan.halt_reason == "process exited mid-run"
    assert stage_rows(paths, 1) == {3: "failed"}


def test_cap_override_lowers_but_never_raises(paths):
    from pipeline.config import load_config
    from pipeline.run import with_caps

    config = load_config(paths.config_dir)
    assert with_caps(config, deep_reads=10).icp.caps.deep_reads == 10
    assert with_caps(config, deep_reads=500).icp.caps.deep_reads == config.icp.caps.deep_reads
    seen = []
    start_run(paths, stages=make_stages(s6=lambda ctx: seen.append(ctx.config.icp.caps.deep_reads)), max_deep_reads=3)
    assert seen == [3]


def test_completed_run_can_redo_later_stages(paths):
    calls = []
    run = start_run(paths, stages=make_stages())
    redone = resume_run(paths, run_id=run.id, from_stage=7, stages=make_stages(s7=lambda ctx: calls.append(7)))
    assert redone.status == "completed" and calls == [7]


def test_ctrl_c_marks_the_run_failed(paths):
    def interrupted(ctx):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        start_run(paths, stages=make_stages(s7=interrupted))
    run = load_run(paths, 1)
    assert run.status == "failed" and run.halt_reason == "stopped by you during stage 7 research"
    assert stage_rows(paths, 1)[7] == "failed"


def test_log_marks_each_pass(paths):
    run = start_run(paths, stages=make_stages(s5=lambda ctx: (_ for _ in ()).throw(Halt("stop"))))
    resume_run(paths, stages=make_stages())
    log = (paths.output_dir / run.started_at[:10] / "run.log").read_text()
    assert f"──── started run {run.id} (manual) ────" in log
    assert f"──── resumed run {run.id} from stage 5 match_score ────" in log


def test_rescore_runs_only_scoring_and_documents(paths):
    from pipeline.run import rescore_run

    calls = []
    run = start_run(paths, stages=make_stages())
    stages = make_stages(**{f"s{n}": (lambda n: lambda ctx: calls.append(n))(n) for n in range(1, 11)})
    assert rescore_run(paths, run.id, stages=stages).status == "completed"
    assert calls == [8, 10]
