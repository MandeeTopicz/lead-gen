"""Stage 8 (response likelihood, priority) and the stage 10 digest, end to end with fakes. No LinkedIn, no API."""

from types import SimpleNamespace as NS

import pytest
from sqlmodel import Session, select

from db import make_engine
from db.models import Finding, Lead, Review, Run, Score
from pipeline.config import load_config
from pipeline.linkedin.cards import POSTED_RECENTLY, Card
from pipeline.linkedin.searches import run_searches
from pipeline.research import research
from pipeline.run import start_run
from pipeline.scoring.response import ResponseInputs, priority, score_response
from pipeline.stages import STAGES, Stage
from tests.test_research import FakeClient, record, response


@pytest.fixture
def config(paths):
    return load_config(paths.config_dir)


def inputs(**overrides):
    base = dict(days_since_post=None, mutual_connections=0, connection_degree=3, shared_background=[],
                in_primary_area=False, months_at_company=40, findings=[])
    base.update(overrides)
    return ResponseInputs(**base)


def finding(id, kind, value="x", confidence="likely", event_date=None, provider="claude_web"):
    return Finding(id=id, kind=kind, value=value, confidence=confidence, event_date=event_date,
                   source_provider=provider, found_at="2026-09-01T00:00:00-05:00", source_url="https://x.example")


def factors(result):
    return {f["factor"]: f for f in result.factors}


@pytest.mark.parametrize("days, points", [(3, 25), (7, 25), (20, 18), (60, 8), (120, 0), (None, 0)])
def test_activity_points(config, days, points):
    assert factors(score_response(inputs(days_since_post=days), config))["activity"]["points"] == points


def test_every_factor_at_full_marks_is_100_and_high(config):
    from datetime import date, timedelta

    recent = (date.today() - timedelta(days=20)).isoformat()
    result = score_response(inputs(
        days_since_post=2, mutual_connections=12, shared_background=["UT Austin"], in_primary_area=True,
        connection_degree=2, months_at_company=4,
        findings=[finding(1, "hiring", "Hiring a dispatch manager"), finding(2, "news", "New Hutto facility",
                  event_date=recent), finding(3, "email", "j@co.com", provider="claude_web:published"),
                  finding(4, "phone", "(512) 555-0100")],
    ), config)
    assert (result.score, result.label) == (100, "High")
    assert factors(result)["trigger"]["finding_ids"] == [1, 2]
    assert factors(result)["reach"]["finding_ids"] == [3, 4]


def test_unverified_findings_and_old_news_earn_nothing(config):
    result = score_response(inputs(findings=[
        finding(1, "hiring", confidence="unverified"),
        finding(2, "news", event_date="2025-01-10"),
        finding(3, "email", "j@co.com", provider="claude_web:pattern"),  # guessed, not published
    ]), config)
    assert factors(result)["trigger"]["points"] == 0
    assert factors(result)["reach"]["points"] == 0


def test_labels(config):
    assert score_response(inputs(days_since_post=2, in_primary_area=True, months_at_company=3,
                                 connection_degree=2), config).label == "Medium"  # 25+15+10+5 = 55
    assert score_response(inputs(), config).label == "Low"


def test_priority_weights_match_over_response(config):
    assert priority(90, 40, config) == 70.0


# --- end to end


ALICE = Card(profile_url="https://www.linkedin.com/sales/lead/ACwAAA1,NAME_SEARCH,a", full_name="Alice Ortiz",
             title="VP of Operations", company_name="Hill Country Freight",
             company_url="https://www.linkedin.com/sales/company/111", location="Austin, Texas, United States",
             time_in_company="4 months in company", connection_degree=2, mutual_connections=8,
             spotlights=[POSTED_RECENTLY])
CARA = Card(profile_url="https://www.linkedin.com/sales/lead/ACwAAA3,NAME_SEARCH,c", full_name="Cara Diaz",
            title="Chief Operating Officer", company_name="Bee Cave Fulfillment",
            company_url="https://www.linkedin.com/sales/company/333", location="Dallas, Texas, United States",
            time_in_company="6 years in company", connection_degree=3)
BOB = Card(profile_url="https://www.linkedin.com/sales/lead/ACwAAA2,NAME_SEARCH,b", full_name="Bob Lee",
           title="Director of Operations", company_name="Lone Star", location="Austin, Texas, United States")


def run_pipeline(paths, cards, client=None):
    class Search:
        def open(self, name): pass
        def cards(self): return list(cards)
        def next_page(self): return False

    client = client or FakeClient([response(record([]), searches=0)] * 5)
    swapped = {2: lambda ctx: None, 3: lambda ctx: run_searches(ctx, Search()), 6: lambda ctx: None,
               7: lambda ctx: research(ctx, client)}
    return start_run(paths, stages=[Stage(s.number, s.name, swapped.get(s.number, s.run), s.uses_linkedin)
                                    for s in STAGES])


def digest_text(paths, run):
    return (paths.output_dir / run.started_at[:10] / f"digest-run{run.id}.md").read_text()


def test_digest_ranks_by_priority_and_explains_itself(paths):
    run = run_pipeline(paths, [CARA, ALICE, BOB])
    assert run.status == "completed" and run.leads_qualified == 2
    text = digest_text(paths, run)
    table = [line for line in text.splitlines() if line.startswith("| 1 ") or line.startswith("| 2 ")]
    assert "Alice Ortiz" in table[0] and "Cara Diaz" in table[1]  # Austin + active + new in role beats Dallas
    assert "(https://www.linkedin.com/sales/lead/ACwAAA1,NAME_SEARCH,a)" in table[0]  # the card's working link
    assert "Joined 4 months ago" in table[0]
    assert "3 found, 2 passed gates" in text
    assert "## Fewer than 10 leads: what cut them" in text
    assert "gate: director-level or below: 1" in text
    with Session(make_engine(paths.db_path)) as session:
        alice = session.exec(select(Lead).where(Lead.full_name == "Alice Ortiz")).one()
        assert alice.last_in_digest_run_id == run.id


def test_repeat_leads_are_held_back_until_something_new(paths):
    run_pipeline(paths, [ALICE])
    second = run_pipeline(paths, [ALICE])
    text = digest_text(paths, second)
    assert "Alice Ortiz: in the digest on" in text and second.leads_qualified == 0

    # New company news after the last digest brings her back.
    with Session(make_engine(paths.db_path)) as session:
        lead = session.exec(select(Lead)).one()
        session.add(Finding(lead_id=lead.id, kind="news", value="Won a regional contract.", confidence="likely",
                            source_url="https://x.example", source_provider="manual",
                            found_at="2099-01-01T00:00:00-06:00"))
        session.commit()
    third = run_pipeline(paths, [ALICE])
    assert third.leads_qualified == 1


def test_not_a_fit_and_active_sequences_are_held_back(paths):
    run_pipeline(paths, [ALICE, CARA])
    with Session(make_engine(paths.db_path)) as session:
        leads = {l.full_name: l for l in session.exec(select(Lead)).all()}
        session.add(Review(lead_id=leads["Alice Ortiz"].id, fit="not_fit"))
        session.add(Review(lead_id=leads["Cara Diaz"].id, sequence_status="active"))
        session.commit()
    text = digest_text(paths, run_pipeline(paths, [ALICE, CARA]))
    assert "Alice Ortiz: marked not a fit" in text
    assert "Cara Diaz: outreach active" in text


def test_research_findings_feed_the_response_score(paths):
    from datetime import date, timedelta

    news = {"kind": "news", "value": "Opened a Hutto cross-dock.", "source_url": "https://news.example/a",
            "confidence": "likely", "date": (date.today() - timedelta(days=10)).isoformat(), "email_basis": None}
    run = run_pipeline(paths, [CARA], FakeClient([response(record([news]), searches=1)]))
    with Session(make_engine(paths.db_path)) as session:
        score = session.exec(select(Score).where(Score.run_id == run.id)).one()
    trigger = next(f for f in score.response_breakdown["factors"] if f["factor"] == "trigger")
    assert trigger["points"] == 7 and trigger["finding_ids"]
    assert "Opened a Hutto cross-dock." in digest_text(paths, run)  # the top reason


def test_mutual_connections_alone_score_once(config):
    result = score_response(inputs(mutual_connections=1, findings=[
        finding(9, "affinity", "1 mutual connection on LinkedIn", provider="linkedin")]), config)
    affinity = factors(result)["affinity"]
    assert (affinity["points"], affinity["detail"]) == (10, "1 mutual connection")


@pytest.mark.parametrize("text, points", [
    ("Hiring movers, drivers, sales team members, and admin staff.", 0),
    ("Hiring a Dispatch Operations Manager in Round Rock.", 8),
    ("Open role: Warehouse Supervisor, second shift.", 8),
])
def test_hiring_trigger_needs_an_ops_role(config, text, points):
    assert factors(score_response(inputs(findings=[finding(1, "hiring", text)]), config))["trigger"]["points"] == points
