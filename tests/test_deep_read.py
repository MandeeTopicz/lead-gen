"""Stage 6 with a fake pacer serving the synthetic lead and company pages. Never touches LinkedIn."""

from datetime import UTC, datetime, timedelta

import pytest
import yaml
from sqlmodel import Session, select

from db import make_engine
from db.models import Company, Lead, Post, Score
from pipeline.context import Halt
from pipeline.deep_read import deep_read
from pipeline.linkedin.cards import POSTED_RECENTLY, Card
from pipeline.linkedin.profile_page import months_since, parse_company_page, parse_lead_page
from pipeline.linkedin.searches import run_searches
from pipeline.run import start_run
from pipeline.stages import STAGES, Stage
from tests.linkedin_pages import FIXTURES, fixture_html

CAPTURED = FIXTURES.parent / "captured"
RECENT = (datetime.now(UTC) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

JORDAN = Card(
    profile_url="https://www.linkedin.com/sales/lead/ACwAAAtest1,NAME_SEARCH,Ab1?_ntb=x",
    full_name="Jordan Reyes",
    title="VP of Operations",
    company_name="Hill Country Freight",
    company_url="https://www.linkedin.com/sales/company/4242?_ntb=x",
    location="Round Rock, Texas, United States",
    time_in_company="1 year 7 months in company",  # the card only knows the current stint
)
SAM = Card(
    profile_url="https://www.linkedin.com/sales/lead/ACwAAAtest2,NAME_SEARCH,Cd2",
    full_name="Sam Patel",
    title="Chief Operating Officer",
    company_name="Pecan Street Media",
    company_url="https://www.linkedin.com/sales/company/5151",
    location="Austin, Texas, United States",
    time_in_company="4 years in company",
    spotlights=[POSTED_RECENTLY],
)


def lead_html(posted_at=RECENT):
    return fixture_html("lead_page").replace("{{POSTED_AT}}", posted_at)


def media_company_html():
    return fixture_html("company_page").replace("Truck Transportation", "Online Audio and Video Media")


class FakePage:
    def __init__(self):
        self.html = ""
        self.mouse = self

    def move(self, x, y):
        pass

    def wheel(self, x, y):
        pass

    def wait_for_timeout(self, ms):
        pass

    def content(self):
        return self.html


class FakePacer:
    def __init__(self, ctx, pages: dict[str, str]):
        self.ctx, self.pages, self.page, self.visited = ctx, pages, FakePage(), []

    def goto(self, url, cost=None):
        if cost == "deep_read":
            self.ctx.budget.use_deep_read()
        self.visited.append(url.split("?")[0])
        self.page.html = self.pages[url.split("?")[0]]

    def halt(self, reason):
        raise Halt(reason)


class FakeSearch:
    def __init__(self, cards):
        self.cards_ = cards

    def open(self, name):
        pass

    def cards(self):
        return self.cards_

    def next_page(self):
        return False


PAGES = {
    "https://www.linkedin.com/sales/lead/ACwAAAtest1,NAME_SEARCH,Ab1": lead_html(),
    "https://www.linkedin.com/sales/company/4242": fixture_html("company_page"),
    "https://www.linkedin.com/sales/lead/ACwAAAtest2,NAME_SEARCH,Cd2": lead_html(),
    "https://www.linkedin.com/sales/company/5151": media_company_html(),
}


def run_with_deep_read(paths, cards, pages=PAGES, visited=None):
    def session(ctx):
        ctx.state["pacer"] = pacer = FakePacer(ctx, pages)
        if visited is not None:
            ctx.state["visited"] = visited
            pacer.visited = visited

    swapped = {2: session, 3: lambda ctx: run_searches(ctx, FakeSearch(cards))}
    stages = [Stage(s.number, s.name, swapped.get(s.number, s.run), s.uses_linkedin) for s in STAGES if s.number <= 6]
    return start_run(paths, stages=stages)


def scores(paths, run_id):
    with Session(make_engine(paths.db_path)) as session:
        rows = session.exec(select(Score, Lead).join(Lead).where(Score.run_id == run_id)).all()
        return {lead.full_name: score for score, lead in rows}


def criteria(score):
    return {c["criterion"]: c for c in score.match_breakdown["criteria"]}


def test_lead_page_parser():
    profile = parse_lead_page(lead_html())
    assert profile.headline == "VP of Operations | 3PL and cross-dock networks"
    assert profile.about.startswith("Twenty years in freight")
    assert [(r.title, r.start, r.end) for r in profile.experience] == [
        ("VP of Operations", "2025-03", None),
        ("Director of Operations", "2021-01", "2025-03"),
        ("Operations Manager", "2016", "2020"),
    ]
    assert profile.experience[0].company_url == "https://www.linkedin.com/sales/company/4242"
    assert profile.current_roles[0].start == "2025-03"
    assert profile.education == [{"school": "Texas State University", "degree": "B.B.A. Supply Chain Management"}]
    assert [p.text for p in profile.posts] == ["Peak season prep: what we changed at our Round Rock cross-dock."]


def test_company_page_parser_ignores_people_also_viewed():
    company = parse_company_page(fixture_html("company_page"))
    assert (company.industry, company.headcount, company.location) == (
        "Truck Transportation", 184, "Round Rock, Texas, United States")
    assert company.growth_6mo == pytest.approx(0.12)


def test_months_since():
    assert months_since("2021-01", datetime(2026, 9, 28)) == 68
    assert months_since("2016", datetime(2026, 9, 28)) == 128


def test_deep_read_rescores_on_confirmed_data(paths):
    run = run_with_deep_read(paths, [JORDAN, SAM])
    assert run.status == "completed"
    assert run.profiles_read == 2
    jordan = criteria(scores(paths, run.id)["Jordan Reyes"])
    # Tenure spans the promotion (Director 2021 -> VP 2025), not just the current title.
    assert (jordan["tenure"]["level"], jordan["tenure"]["source"]) == ("full", "profile")
    assert (jordan["activity"]["level"], jordan["activity"]["source"]) == ("full", "profile")
    assert (jordan["industry"]["value"], jordan["industry"]["source"]) == ("Truck Transportation", "company_page")
    assert jordan["keywords"]["level"] == "full"

    sam = scores(paths, run.id)["Sam Patel"]
    assert not sam.gates_passed  # the company page shows a media company, not logistics
    assert "industry" in sam.match_breakdown["gate_failures"][0]

    with Session(make_engine(paths.db_path)) as session:
        lead = session.exec(select(Lead).where(Lead.full_name == "Jordan Reyes")).one()
        assert lead.last_deep_read_at and len(lead.experience) == 3
        assert len(session.exec(select(Post).where(Post.lead_id == lead.id)).all()) == 1  # comments don't count
        company = session.exec(select(Company).where(Company.name == "Hill Country Freight")).one()
        assert (company.industry, company.headcount) == ("Truck Transportation", 184)
        assert company.domain == "hillcountryfreight.example"


def test_no_recent_posts_means_no_activity_credit(paths):
    quiet = {**PAGES, "https://www.linkedin.com/sales/lead/ACwAAAtest1,NAME_SEARCH,Ab1":
             lead_html().split('<section data-sn-view-name="feature-lead-relationship">')[0]
             + '<section data-sn-view-name="feature-lead-relationship"></section></body></html>'}
    run = run_with_deep_read(paths, [JORDAN], quiet)
    activity = criteria(scores(paths, run.id)["Jordan Reyes"])["activity"]
    assert (activity["level"], activity["points"], activity["value"]) == ("none", 0, "no recent posts")


def test_only_top_leads_are_read_up_to_the_cap(paths):
    icp_path = paths.config_dir / "icp.yaml"
    data = yaml.safe_load(icp_path.read_text())
    data["caps"]["deep_reads"] = 1
    icp_path.write_text(yaml.safe_dump(data))
    visited = []
    run = run_with_deep_read(paths, [JORDAN, SAM], visited=visited)
    assert run.profiles_read == 1
    assert visited[0] == "https://www.linkedin.com/sales/lead/ACwAAAtest2,NAME_SEARCH,Cd2"  # Sam scored higher


def test_recent_deep_reads_are_reused(paths):
    run_with_deep_read(paths, [JORDAN])
    visited = []
    second = run_with_deep_read(paths, [JORDAN], visited=visited)
    assert visited == [] and second.profiles_read == 0
    assert criteria(scores(paths, second.id)["Jordan Reyes"])["tenure"]["source"] == "profile"


def test_unparseable_lead_page_halts(paths):
    broken = {**PAGES, "https://www.linkedin.com/sales/lead/ACwAAAtest1,NAME_SEARCH,Ab1": "<html><body>?</body></html>"}
    run = run_with_deep_read(paths, [JORDAN], broken)
    assert run.status == "halted"
    assert run.halt_reason == "could not parse the lead page: lead page has no top card"


@pytest.mark.skipif(not (CAPTURED / "lead.html").exists(), reason="no real lead capture")
def test_real_lead_capture_parses():
    profile = parse_lead_page((CAPTURED / "lead.html").read_text())
    assert profile.experience and profile.current_roles and profile.activity_shown
    assert all(role.start for role in profile.current_roles)


@pytest.mark.skipif(not (CAPTURED / "company.html").exists(), reason="no real company capture")
def test_real_company_capture_parses():
    company = parse_company_page((CAPTURED / "company.html").read_text())
    assert company.industry and company.headcount and company.location
