"""Stages 3-5 end to end with a fake Sales Navigator: search runner, dedupe, scoring, report."""

import pytest
import yaml
from sqlmodel import Session, select

from db import make_engine
from db.models import Company, Lead, Score, SearchHit
from pipeline.context import Halt
from pipeline.linkedin.cards import POSTED_RECENTLY, Card, canonical_lead_url, tenure_months
from pipeline.linkedin.searches import run_searches
from pipeline.report import match_report
from pipeline.run import start_run
from pipeline.stages import STAGES, Stage

ALICE = Card(
    profile_url="https://www.linkedin.com/sales/lead/ACwAAA1,NAME_SEARCH,x1?_ntb=abc",
    full_name="Alice Ortiz",
    title="VP of Operations",
    company_name="Hill Country Freight",
    company_url="https://www.linkedin.com/sales/company/111?_ntb=abc",
    location="Austin, Texas, United States",
    time_in_role="4 months in role",
    time_in_company="4 months in company",
    spotlights=[POSTED_RECENTLY],
)
BOB = Card(
    profile_url="https://www.linkedin.com/sales/lead/ACwAAA2,NAME_SEARCH,x2",
    full_name="Bob Lee",
    title="Director of Operations",
    company_name="Lone Star Cold Chain",
    location="Houston, Texas, United States",
    time_in_company="2 years in company",
)
CARA = Card(
    profile_url="https://www.linkedin.com/sales/lead/ACwAAA3,NAME_SEARCH,x3",
    full_name="Cara Diaz",
    title="Chief Operating Officer",
    company_name="Bee Cave Fulfillment",
    location="Bee Cave, Texas, United States",
    time_in_company="6 years 2 months in company",
)


class FakeSearches:
    """Serves result pages per saved-search name and spends the page budget like the real navigator."""

    def __init__(self, ctx, results: dict[str, list[list[Card]]]):
        self.ctx = ctx
        self.results = results
        self.opened: list[str] = []
        self.pages: list[list[Card]] = []
        self.index = 0

    def open(self, name):
        if name not in self.results:
            raise Halt(f"no saved search named {name!r}")
        self.ctx.budget.use_page()
        self.opened.append(name)
        self.pages, self.index = self.results[name], 0

    def cards(self):
        return self.pages[self.index] if self.pages else []

    def next_page(self):
        if self.index + 1 >= len(self.pages):
            return False
        self.ctx.budget.use_page()
        self.index += 1
        return True


def stages_with_searches(results, seen=None):
    def searches(ctx):
        fake = FakeSearches(ctx, results)
        run_searches(ctx, fake)
        if seen is not None:
            seen.extend(fake.opened)

    # Stage 2 is swapped out too: tests never open a browser or touch LinkedIn.
    swapped = {2: lambda ctx: None, 3: searches}
    return [Stage(s.number, s.name, swapped.get(s.number, s.run), s.uses_linkedin) for s in STAGES]


ALL_SEARCHES = ["S1-strict", "S2-no-activity", "S3-no-tenure", "S4-wider-size", "S5-wider-area",
                "S6-no-keywords", "S7-new-in-role"]


def empty_except(**named):
    results = {name: [[]] for name in ALL_SEARCHES}
    results.update(named)
    return results


def db(paths):
    return Session(make_engine(paths.db_path))


def test_card_url_and_tenure_helpers():
    assert canonical_lead_url(ALICE.profile_url) == "https://www.linkedin.com/sales/lead/ACwAAA1"
    assert tenure_months("3 years 2 months in company") == 38
    assert tenure_months("1 year in role") == 12
    assert tenure_months("11 months in company") == 11
    assert tenure_months("Less than 1 year in role") == 6
    assert tenure_months(None) is None


def test_leads_are_deduped_across_searches_and_scored(paths):
    results = empty_except(**{"S1-strict": [[ALICE]], "S2-no-activity": [[ALICE, BOB], [CARA]]})
    run = start_run(paths, stages=stages_with_searches(results))
    assert run.status == "completed"
    assert run.leads_found == 3
    assert run.pages_viewed == 8  # S2 has two pages; the other six searches one each

    with db(paths) as session:
        assert len(session.exec(select(Lead)).all()) == 3
        alice = session.exec(select(Lead).where(Lead.full_name == "Alice Ortiz")).one()
        hits = session.exec(select(SearchHit.search_code).where(SearchHit.lead_id == alice.id)).all()
        assert sorted(hits) == ["S1", "S2"]
        assert session.get(Company, alice.company_id).linkedin_url == "https://www.linkedin.com/sales/company/111"
        assert alice.started_current_role is not None

        scores = {s.lead_id: s for s in session.exec(select(Score)).all()}
        assert scores[alice.id].gates_passed and scores[alice.id].match_score >= 96
        bob = session.exec(select(Lead).where(Lead.full_name == "Bob Lee")).one()
        assert not scores[bob.id].gates_passed


def test_leads_persist_across_runs_without_duplicates(paths):
    results = empty_except(**{"S1-strict": [[ALICE]]})
    first = start_run(paths, stages=stages_with_searches(results))
    second = start_run(paths, stages=stages_with_searches(results))
    with db(paths) as session:
        leads = session.exec(select(Lead)).all()
        assert len(leads) == 1
        assert leads[0].first_seen_run_id == first.id
        assert len(session.exec(select(Score).where(Score.run_id == second.id)).all()) == 1


def test_per_search_page_limit(paths):
    many_pages = [[ALICE]] * 10
    run = start_run(paths, stages=stages_with_searches(empty_except(**{"S1-strict": many_pages})))
    assert run.pages_viewed == 4 + 6  # capped at 4 pages for S1, then one page each for the rest


def test_run_wide_cap_stops_remaining_searches_but_keeps_results(paths):
    icp_path = paths.config_dir / "icp.yaml"
    data = yaml.safe_load(icp_path.read_text())
    data["caps"]["result_pages"] = 10
    icp_path.write_text(yaml.safe_dump(data))

    seen = []
    results = {name: [[ALICE]] * 10 for name in ALL_SEARCHES}
    run = start_run(paths, stages=stages_with_searches(results, seen))
    assert run.status == "completed"
    assert run.pages_viewed == 10
    assert seen == ["S1-strict", "S2-no-activity", "S3-no-tenure"]  # 4 + 4 + 2 pages, then the cap
    with db(paths) as session:
        codes = sorted(set(session.exec(select(SearchHit.search_code)).all()))
    assert codes == ["S1", "S2", "S3"]


def test_missing_saved_search_halts_the_run(paths):
    results = {"S1-strict": [[ALICE]]}  # S2 onward don't exist
    run = start_run(paths, stages=stages_with_searches(results))
    assert run.status == "halted"
    assert run.halt_reason == "no saved search named 'S2-no-activity'"
    with db(paths) as session:  # S1's page was kept
        assert len(session.exec(select(SearchHit)).all()) == 1


def test_report_ranks_passed_leads_and_lists_dropped(paths):
    results = empty_except(**{"S1-strict": [[ALICE]], "S2-no-activity": [[BOB, CARA]]})
    start_run(paths, stages=stages_with_searches(results))
    with db(paths) as session:
        _, markdown = match_report(session, details=True)
    lines = markdown.splitlines()
    ranked = [line for line in lines if line.startswith("| 1 ") or line.startswith("| 2 ")]
    assert "Alice Ortiz" in ranked[0] and "Cara Diaz" in ranked[1]
    assert "## Dropped" in markdown and "director-level or below" in markdown
    assert "| industry | full | 18/18 | - | search:S1 |" in markdown


def test_report_without_scores(paths):
    from pipeline.report import NoScores

    start_run(paths, dry_run=True)
    with db(paths) as session, pytest.raises(NoScores):
        match_report(session)
