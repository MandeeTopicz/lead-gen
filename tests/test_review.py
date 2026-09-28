"""Review screen data, the Streamlit app itself (AppTest), and the launchd schedule file. Fakes only."""

import plistlib

import pytest
from sqlmodel import Session, select
from streamlit.testing.v1 import AppTest

from db import make_engine
from db.models import Lead, Review
from pipeline import review, schedule
from tests.conftest import REPO_ROOT
from tests.test_drafting import FakeWriter, run_pipeline, with_sender

APP = REPO_ROOT / "app" / "review.py"


@pytest.fixture
def seeded(paths):
    with_sender(paths)
    return run_pipeline(paths, FakeWriter())


def db(paths):
    return Session(make_engine(paths.db_path), expire_on_commit=False)


def test_digest_leads_bring_dossier_and_drafts(paths, seeded):
    with db(paths) as session:
        assert [r.id for r in review.runs_with_digest(session)] == [seeded.id]
        (item,) = review.digest_leads(session, seeded.id)
        assert item.rank == 1 and item.lead.full_name == "Jordan Reyes"
        assert len(item.steps) == 14 and item.dossier.md_path.endswith(".md")


def test_marks_persist_and_keep_leads_out_of_later_digests(paths, seeded):
    with db(paths) as session:
        lead = session.exec(select(Lead)).one()
        review.save_review(session, lead.id, fit="good")
        review.save_review(session, lead.id, sequence_status="active", notes="Sent L1")
        saved = session.get(Review, lead.id)
        assert (saved.fit, saved.sequence_status, saved.contacted, saved.notes) == ("good", "active", True, "Sent L1")
        # This run's digest already includes the lead; a new run would hold it back while the sequence is active.
        from pipeline.digest import hold_back_reason
        assert hold_back_reason(session, lead, seeded, 30) == "outreach active"
        review.save_review(session, lead.id, clear=("sequence_status",))
        assert session.get(Review, lead.id).sequence_status is None
    with db(paths) as session, pytest.raises(ValueError):
        review.save_review(session, lead.id, fit="maybe")


def test_app_renders_and_marks_a_lead(paths, seeded, monkeypatch):
    monkeypatch.setenv("LEADGEN_ROOT", str(paths.root))
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    assert not app.exception
    assert app.title[0].value == f"Digest: run {seeded.id}"
    assert "Jordan Reyes" in app.expander[0].label
    assert len(app.code) == 14  # every draft, copyable
    next(b for b in app.button if b.label == "Good fit").click().run()
    assert "GOOD FIT" in app.expander[0].label
    with db(paths) as session:
        assert session.exec(select(Review)).one().fit == "good"


def test_run_history_page(paths, seeded, monkeypatch):
    monkeypatch.setenv("LEADGEN_ROOT", str(paths.root))
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    app.sidebar.radio[0].set_value("Run history").run()
    assert not app.exception
    assert app.title[0].value == "Run history"
    assert any(b.label == "Run now" for b in app.sidebar.button)


def test_schedule_plist(paths):
    spec = schedule.plist(paths, 7, 30, "/opt/homebrew/bin/uv")
    assert spec["Label"] == "com.leadgen.daily"
    assert spec["StartCalendarInterval"] == {"Hour": 7, "Minute": 30}
    assert spec["ProgramArguments"][-4:] == ["leadgen", "run", "--trigger", "cron"]
    assert spec["WorkingDirectory"] == str(paths.root)
    assert "/opt/homebrew/bin" in spec["EnvironmentVariables"]["PATH"]
    plistlib.dumps(spec)  # valid plist


def test_schedule_rejects_bad_times(paths):
    with pytest.raises(ValueError):
        schedule.install(paths, "25:00")


def test_stale_running_run_is_cleared_and_live_one_is_shown(paths, seeded):
    from db.models import Run
    from pipeline.context import now_iso
    from pipeline.run import run_lock

    with db(paths) as session:
        session.add(Run(started_at=now_iso(), trigger="manual", status="running", icp_version="x@v1"))
        session.commit()
        with run_lock(paths.lock_path):  # a live run holds the lock
            assert review.run_in_progress(session, paths).status == "running"
        assert review.run_in_progress(session, paths) is None  # nothing holds it: stale, marked failed
        assert session.exec(select(Run).where(Run.status == "running")).first() is None
