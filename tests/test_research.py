"""Stage 7 with a fake Anthropic client that replays scripted responses. Never calls the API."""

from types import SimpleNamespace as NS

import pytest
from sqlmodel import Session, select

from db import make_engine
from db.models import Finding, Lead
from pipeline.linkedin.cards import Card
from pipeline.linkedin.searches import run_searches
from pipeline.research import (
    BLOCKED_DOMAINS,
    ReportedFinding,
    ResearchReport,
    clean_findings,
    published_email,
    research,
    research_lead,
)
from pipeline.config import load_config
from pipeline.run import start_run
from pipeline.stages import STAGES, Stage

JORDAN = Card(
    profile_url="https://www.linkedin.com/sales/lead/ACwAAAtest1,NAME_SEARCH,Ab1",
    full_name="Jordan Reyes",
    title="VP of Operations",
    company_name="Hill Country Freight",
    company_url="https://www.linkedin.com/sales/company/4242",
    location="Round Rock, Texas, United States",
    time_in_company="4 years in company",
)

FINDINGS = [
    {"kind": "email", "value": "Jordan.Reyes@hillcountryfreight.com", "source_url": "https://hillcountryfreight.com/team",
     "confidence": "likely", "date": None, "email_basis": "published"},
    {"kind": "email", "value": "jreyes@gmail.com", "source_url": "https://example.com/bio",
     "confidence": "likely", "date": None, "email_basis": "published"},
    {"kind": "phone", "value": "(512) 555-0100", "source_url": "https://hillcountryfreight.com/contact",
     "confidence": "likely", "date": None, "email_basis": None},
    {"kind": "news", "value": "Opened a second cross-dock in Hutto.", "source_url": "https://bizjournals.example/hutto",
     "confidence": "verified", "date": "2026-08-14", "email_basis": None},
    {"kind": "hiring", "value": "Hiring a Dispatch Operations Manager.", "source_url": "not a url",
     "confidence": "likely", "date": None, "email_basis": None},
    {"kind": "talk", "value": "Spoke on last-mile at the 2026 Austin Logistics Summit.",
     "source_url": "https://austinlogistics.example/speakers", "confidence": "likely", "date": "2026-04-02",
     "email_basis": None},
]


def usage(tokens_in=10_000, tokens_out=1_000, searches=3):
    return NS(input_tokens=tokens_in, output_tokens=tokens_out, cache_creation_input_tokens=0,
              cache_read_input_tokens=0, server_tool_use=NS(web_search_requests=searches))


def text(t):
    return NS(type="text", text=t)


def record(findings=FINDINGS, summary="Regional LTL and drayage carrier in Central Texas."):
    return NS(type="tool_use", name="record_findings", id="toolu_1",
              input={"findings": findings, "company_summary": summary})


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)

    def stream(self, **kwargs):
        response = self.create(**kwargs)

        class Stream:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def get_final_message(self):
                return response

        return Stream()


def response(*content, stop="tool_use", **u):
    return NS(content=list(content), stop_reason=stop, usage=usage(**u))


@pytest.fixture
def settings(paths):
    return load_config(paths.config_dir).icp.research


def test_research_lead_returns_report_and_cost(settings):
    client = FakeClient([response(text("Searching…"), record())])
    report, cost = research_lead(client, settings, "brief")
    assert len(report.findings) == len(FINDINGS)
    # Sonnet 5: 10k in x $2/M + 1k out x $10/M + 3 searches x $0.01
    assert cost == pytest.approx(0.02 + 0.01 + 0.03)
    tools = {t["name"]: t for t in client.calls[0]["tools"]}
    assert tools["web_search"]["type"] == "web_search_20260209"
    assert tools["web_search"]["max_uses"] == settings.max_searches
    assert tools["web_fetch"]["blocked_domains"] == BLOCKED_DOMAINS
    assert tools["web_fetch"]["max_content_tokens"] == settings.max_fetch_tokens
    assert tools["record_findings"]["strict"] is True


def test_pause_turn_resumes_without_a_new_user_message(settings):
    paused = response(text("…"), stop="pause_turn")
    client = FakeClient([paused, response(record())])
    report, _ = research_lead(client, settings, "brief")
    assert report is not None
    second = client.calls[1]["messages"]
    assert [m["role"] for m in second] == ["user", "assistant"]
    assert second[1]["content"] is paused.content


def test_nudges_once_then_gives_up(settings):
    client = FakeClient([response(text("Done."), stop="end_turn"), response(text("Still done."), stop="end_turn")])
    report, cost = research_lead(client, settings, "brief")
    assert report is None and len(client.calls) == 2 and cost > 0
    assert "record_findings" in client.calls[1]["messages"][-1]["content"]


def test_cleaning_enforces_the_rules():
    report = ResearchReport(findings=[ReportedFinding(**f) for f in FINDINGS] + [ReportedFinding(
        kind="email", value="ops@hillcountryfreight.com", source_url="https://x.example", confidence="verified",
        email_basis="pattern")])
    kept = {(f.kind, f.value): f for f in clean_findings(report, None)}
    assert ("email", "jordan.reyes@hillcountryfreight.com") in kept  # normalized
    assert not any("gmail" in v for _, v in kept)  # personal email dropped
    assert not any(k == "hiring" for k, _ in kept)  # no real source URL: dropped
    news = kept[("news", "Opened a second cross-dock in Hutto.")]
    assert (news.confidence, news.event_date) == ("verified", "2026-08-14")
    guessed = kept[("email", "ops@hillcountryfreight.com")]
    assert (guessed.confidence, guessed.source_provider) == ("likely", "claude_web:pattern")  # never verified


def test_published_email_counts_for_email_steps():
    report = ResearchReport(findings=[ReportedFinding(**f) for f in FINDINGS])
    email = published_email(clean_findings(report, None))
    assert email.value == "jordan.reyes@hillcountryfreight.com"
    pattern_only = ResearchReport(findings=[ReportedFinding(
        kind="email", value="jreyes@hillcountryfreight.com", source_url="https://x.example", confidence="likely",
        email_basis="pattern")])
    assert published_email(clean_findings(pattern_only, None)) is None


def run_with_research(paths, client, cards=(JORDAN,)):
    class Search:
        def open(self, name): pass
        def cards(self): return list(cards)
        def next_page(self): return False

    swapped = {2: lambda ctx: None, 3: lambda ctx: run_searches(ctx, Search()), 6: lambda ctx: None,
               7: lambda ctx: research(ctx, client)}
    stages = [Stage(s.number, s.name, swapped.get(s.number, s.run), s.uses_linkedin) for s in STAGES if s.number <= 7]
    return start_run(paths, stages=stages)


def findings(paths):
    with Session(make_engine(paths.db_path)) as session:
        return session.exec(select(Finding)).all()


def test_stage_stores_findings_and_cost(paths):
    run = run_with_research(paths, FakeClient([response(record())]))
    assert run.status == "completed"
    assert run.llm_cost_usd == pytest.approx(0.06)
    stored = findings(paths)
    kinds = sorted(f.kind for f in stored)
    assert kinds == ["company", "email", "news", "phone", "talk"]
    news = next(f for f in stored if f.kind == "news")
    assert news.company_id is not None and news.lead_id is not None and news.run_id == run.id
    with Session(make_engine(paths.db_path)) as session:
        assert session.exec(select(Lead)).one().last_researched_at is not None


def test_recent_research_is_reused(paths):
    run_with_research(paths, FakeClient([response(record())]))
    client = FakeClient([])
    run_with_research(paths, client)
    assert client.calls == []
    assert len(findings(paths)) == 5


def test_budget_stops_research(paths):
    import yaml
    icp_path = paths.config_dir / "icp.yaml"
    data = yaml.safe_load(icp_path.read_text())
    data["research"]["max_usd_per_run"] = 0.05
    icp_path.write_text(yaml.safe_dump(data))
    other = JORDAN.model_copy(update={"profile_url": "https://www.linkedin.com/sales/lead/ACwAAAtest2,N,x",
                                      "full_name": "Sam Patel"})
    client = FakeClient([response(record()), response(record())])
    run_with_research(paths, client, cards=(JORDAN, other))
    assert len(client.calls) == 1  # the first lead's $0.06 used the $0.05 budget


def test_skips_without_api_key(paths, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    swapped_client = None
    run = run_with_research(paths, swapped_client)
    assert run.status == "completed"
    assert findings(paths) == []


def test_record_findings_schema_has_no_null_inside_a_string_enum():
    """The API rejects an enum containing null on a ["string", "null"] field (found in the first real run)."""
    from pipeline.research import RECORD_FINDINGS

    def walk(node):
        if isinstance(node, dict):
            assert None not in node.get("enum", []), node
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(RECORD_FINDINGS["input_schema"])
