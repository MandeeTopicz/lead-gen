"""Stage 9 (drafting) with a fake Claude that follows the prompt format. No LinkedIn, no API."""

import re
from datetime import date
from types import SimpleNamespace as NS

import pytest
import yaml
from sqlmodel import Session, select

from db import make_engine
from db.models import Finding, Lead, OutreachStep, Score
from pipeline.config import load_config
from pipeline.drafting import CommunicationStyle, Draft, LeadWriteup, Reason, check_writeup, drafting, write_lead
from pipeline.linkedin.cards import POSTED_RECENTLY, Card
from pipeline.linkedin.searches import run_searches
from pipeline.outreach_plan import PlannedStep, call_opener, check_draft, email_footer, plan_sequence
from pipeline.research import research
from pipeline.run import start_run
from pipeline.stages import STAGES, Stage
from tests.test_research import FakeClient, record, response

SENDER = {
    "sender": {"name": "Riley Moss", "title": "Founder", "company": "Dockline Ops", "email": "riley@dockline.example",
               "phone": "", "physical_address": "100 Congress Ave, Austin, TX 78701", "local_to_austin": True},
    "offer": "Dock scheduling and yard visibility for 3PLs.",
    "proof_points": ["Cut detention fees 22% for a 150-person Texas 3PL"],
    "past_clients": [],
    "background": {"employers": [], "schools": ["Texas State University"], "communities": []},
}


@pytest.fixture
def config(paths):
    return load_config(paths.config_dir)


def with_sender(paths):
    (paths.config_dir / "sender.yaml").write_text(yaml.safe_dump(SENDER))
    return load_config(paths.config_dir)


# --- plan and checks


def full_sequence(config):
    seq = config.icp.sequence.model_copy(update={
        "linkedin": config.icp.sequence.linkedin.model_copy(update={"touches": 10}),
        "email": config.icp.sequence.email.model_copy(update={"touches": 2}),
        "calls": config.icp.sequence.calls.model_copy(update={"touches": 2}),
    })
    return config.model_copy(update={"icp": config.icp.model_copy(update={"sequence": seq})})


def test_shipped_config_drafts_one_linkedin_touch(config):
    plan = plan_sequence(date(2026, 9, 28), config, connected=False, local=True, has_email=True, has_phone=True)
    assert [(s.code, s.channel) for s in plan] == [("L1", "linkedin_connect")]


def test_plan_has_ten_linkedin_touches_then_email_and_calls(config):
    start = date(2026, 9, 28)  # a Monday
    plan = plan_sequence(start, full_sequence(config), connected=False, local=True, has_email=True, has_phone=True)
    codes = [s.code for s in plan]
    assert codes == [f"L{i}" for i in range(1, 11)] + ["E1", "E2", "C1", "C2"]
    assert plan[0].channel == "linkedin_connect" and plan[1].channel == "inmail"
    # InMail fallback after 5 days; that's a Saturday here, so it moves to Monday.
    assert plan[1].planned_date == date(2026, 10, 5)
    assert all(s.planned_date.weekday() < 5 for s in plan)
    assert plan[6].angle == "Coffee in Austin"
    assert plan[10].planned_date >= date(2026, 11, 2)  # email starts in week 6
    assert len({s.angle for s in plan if s.channel.startswith("linkedin") or s.channel == "inmail"}) == 10


def test_plan_without_contact_info_or_connection(config):
    plan = plan_sequence(date(2026, 9, 28), full_sequence(config), connected=True, local=False, has_email=False,
                         has_phone=False)
    assert [s.code for s in plan] == [f"L{i}" for i in range(1, 11)]
    assert plan[0].channel == "linkedin_message" and plan[1].channel == "linkedin_message"
    assert plan[6].angle == "A 15-minute call"


def step(code="L3", channel="linkedin_message"):
    return PlannedStep(code, channel, date(2026, 10, 5), "angle")


@pytest.mark.parametrize("channel, body, subject, problem", [
    ("linkedin_connect", "x" * 301, None, "301 chars, limit 300"),
    ("linkedin_message", "Hi [First Name], saw your post.", None, "unfilled placeholder '[First Name]'"),
    ("email", " ".join(["word"] * 121), "Quick question", "121 words, limit 120"),
    ("email", "Short note.", "A subject line that is far too long", "subject is 8 words"),
    ("call", "Opener: One. Two. Three. Four.\nVoicemail: Hi.", None, "4 sentences, limit 3"),
])
def test_draft_checks_catch_problems(channel, body, subject, problem):
    problems = check_draft(step("E1" if channel == "email" else "L3", channel), subject, body, [1], {1})
    assert any(problem in p for p in problems), problems


def test_draft_checks_require_real_citations():
    assert "cites no evidence" in check_draft(step(), None, "Fine.", [], {1})[0]
    assert "doesn't exist or isn't usable: [9]" in check_draft(step(), None, "Fine.", [9], {1})[0]
    assert check_draft(step(), None, "Fine.", [1], {1}) == []


def test_call_opener_and_footer(config):
    assert call_opener("Opener: Hi Sam. Quick one.\nVoicemail: Call me back.") == "Hi Sam. Quick one."
    sender = with_sender_obj()
    footer = email_footer(sender)
    assert "100 Congress Ave" in footer and "no thanks" in footer and "Riley Moss, Founder, Dockline Ops" in footer


def with_sender_obj():
    from pipeline.config import SenderConfig
    return SenderConfig.model_validate(SENDER)


# --- a fake Claude that follows the brief


class FakeWriter:
    """messages.parse: answers the style call, and writes one draft per planned step citing the first evidence id.
    `mutate` lets a test break the first writeup to exercise the retry."""

    def __init__(self, mutate=None):
        self.calls = []
        self.writeups = 0
        self.messages = self
        self.mutate = mutate

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        usage = NS(input_tokens=4000, output_tokens=2000, cache_creation_input_tokens=0, cache_read_input_tokens=0,
                   server_tool_use=None)
        if kwargs["output_format"] is CommunicationStyle:
            parsed = CommunicationStyle(formality="casual", typical_length="short", uses_emoji=False,
                                        recurring_topics=["peak season"])
            return NS(parsed_output=parsed, usage=usage, content=[])
        brief = kwargs["messages"][0]["content"]
        ids = [int(i) for i in re.findall(r"^\[(\d+)\]", brief, re.M)]
        steps = re.findall(r"^([LEC]\d+) \| (\w+) \| [\d-]+ \| ([^|]+) \|", brief, re.M)
        drafts = [Draft(step_code=code, angle=angle.strip(),
                        subject="Hutto cross-dock" if channel == "email" else None,
                        body="Opener: Saw the Hutto news.\nVoicemail: Riley here." if channel == "call"
                        else "Saw your post about peak season at Hill Country Freight. Worth comparing notes?",
                        cited_finding_ids=ids[:1]) for code, channel, angle in steps]
        writeup = LeadWriteup(person_summary="VP of Ops at a Round Rock carrier.", company_snapshot="Regional LTL.",
                              reasons=[Reason(text=f"Reason {i}", finding_ids=ids[:1]) for i in range(3)],
                              talking_points=["Peak season", "Detention fees", "Yard visibility"], drafts=drafts)
        self.writeups += 1
        if self.mutate and self.writeups == 1:
            writeup = self.mutate(writeup)
        return NS(parsed_output=writeup, usage=usage, content=[NS(type="text", text=writeup.model_dump_json())])


JORDAN = Card(profile_url="https://www.linkedin.com/sales/lead/ACwAAAtest1,NAME_SEARCH,Ab1", full_name="Jordan Reyes",
              title="VP of Operations", company_name="Hill Country Freight",
              company_url="https://www.linkedin.com/sales/company/4242", location="Round Rock, Texas, United States",
              time_in_company="8 months in company", connection_degree=2, mutual_connections=6,
              spotlights=[POSTED_RECENTLY])

EMAIL = {"kind": "email", "value": "jordan@hillcountryfreight.example", "source_url": "https://hcf.example/team",
         "confidence": "likely", "date": None, "email_basis": "published"}
PHONE = {"kind": "phone", "value": "(512) 555-0100", "source_url": "https://hcf.example/contact",
         "confidence": "likely", "date": None, "email_basis": None}


def run_pipeline(paths, writer, findings=(EMAIL, PHONE)):
    class Search:
        def open(self, name): pass
        def cards(self): return [JORDAN]
        def next_page(self): return False

    researcher = FakeClient([response(record(list(findings)), searches=1)])
    swapped = {2: lambda ctx: None, 3: lambda ctx: run_searches(ctx, Search()), 6: lambda ctx: None,
               7: lambda ctx: research(ctx, researcher), 9: lambda ctx: drafting(ctx, writer)}
    return start_run(paths, stages=[Stage(s.number, s.name, swapped.get(s.number, s.run), s.uses_linkedin)
                                    for s in STAGES])


def stored(paths, run):
    with Session(make_engine(paths.db_path)) as session:
        steps = session.exec(select(OutreachStep).where(OutreachStep.run_id == run.id)
                             .order_by(OutreachStep.id)).all()
        score = session.exec(select(Score).where(Score.run_id == run.id)).one()
        lead = session.exec(select(Lead)).one()
        findings = session.exec(select(Finding)).all()
        return steps, score, lead, findings


def test_stage_drafts_the_full_sequence_with_sources(paths, full_sequence):
    with_sender(paths)
    run = run_pipeline(paths, FakeWriter())
    assert run.status == "completed"
    steps, score, lead, findings = stored(paths, run)
    assert [s.step_code for s in steps] == [f"L{i}" for i in range(1, 11)] + ["E1", "E2", "C1", "C2"]
    assert all(s.checks_passed for s in steps)
    email = next(s for s in steps if s.step_code == "E1")
    assert email.subject == "Hutto cross-dock" and "100 Congress Ave" in email.body and "no thanks" in email.body
    assert len(score.reasons) == 3 and score.writeup["check_failures"] == []
    # No posts or About here (the deep read is skipped), so no style call: the default style is used.
    assert lead.communication_style["formality"] == "balanced"
    # LinkedIn facts became citable findings alongside the research ones.
    assert {"role", "location", "affinity", "email", "phone"} <= {f.kind for f in findings}
    cited = {i for s in steps for i in s.cited_finding_ids}
    assert cited <= {f.id for f in findings if f.confidence != "unverified"}
    assert run.llm_cost_usd > 0


def test_no_email_or_call_steps_without_published_contact_info(paths, full_sequence):
    with_sender(paths)
    guessed = {**EMAIL, "email_basis": "pattern"}
    run = run_pipeline(paths, FakeWriter(), findings=(guessed,))
    steps, *_ = stored(paths, run)
    assert [s.step_code for s in steps] == [f"L{i}" for i in range(1, 11)]


def test_failing_writeup_is_retried_with_the_problems(paths):
    with_sender(paths)

    def too_long(writeup):
        writeup.drafts[0].body = "x" * 400  # L1 is a connection note: 300-character limit
        return writeup

    writer = FakeWriter(mutate=too_long)
    run = run_pipeline(paths, writer)
    steps, score, *_ = stored(paths, run)
    assert writer.writeups == 2  # first writeup, then the retry
    assert "L1: 400 chars, limit 300" in writer.calls[-1]["messages"][-1]["content"]
    assert score.writeup["check_failures"] == [] and all(s.checks_passed for s in steps)


def test_still_failing_after_retry_is_flagged_not_dropped(paths):
    with_sender(paths)

    class Stubborn(FakeWriter):
        def parse(self, **kwargs):
            result = super().parse(**kwargs)
            if kwargs["output_format"] is LeadWriteup:
                result.parsed_output.drafts[0].body = "Hi [First Name]!"
            return result

    run = run_pipeline(paths, Stubborn())
    steps, score, *_ = stored(paths, run)
    l1 = next(s for s in steps if s.step_code == "L1")
    assert not l1.checks_passed and l1.body == "Hi [First Name]!"
    assert any("placeholder" in p for p in score.writeup["check_failures"])
    assert all(s.checks_passed for s in steps if s.step_code != "L1")


def test_without_sender_details_only_reasons_are_written(paths):
    (paths.config_dir / "sender.yaml").write_text("sender: {}\n")
    run = run_pipeline(paths, FakeWriter())
    steps, score, *_ = stored(paths, run)
    assert steps == []
    assert len(score.reasons) == 3


def test_check_writeup_counts_and_codes():
    plan = [step("L1", "linkedin_connect"), step("L2", "inmail")]
    writeup = LeadWriteup(person_summary="", company_snapshot="", reasons=[Reason(text="r", finding_ids=[])],
                          talking_points=[], drafts=[Draft(step_code="L1", angle="a", subject=None, body="Hi.",
                                                           cited_finding_ids=[1])])
    problems = check_writeup(writeup, plan, {1})
    assert "write 3-5 reasons, not 1" in problems
    assert "reason 1 must cite existing evidence ids" in problems
    assert "missing drafts for L2" in problems


def test_style_is_tagged_from_posts(config):
    writer = FakeWriter()
    lead = Lead(profile_url="x", full_name="Jordan Reyes", about="Freight nerd.")
    from pipeline.drafting import tag_style
    style, calls = tag_style(writer, config, lead, ["Peak season prep at our cross-dock!"])
    assert style.formality == "casual" and calls[0].cost_usd > 0 and calls[0].model == "claude-haiku-4-5"
    assert writer.calls[0]["model"] == "claude-haiku-4-5"
    assert "Peak season prep" in writer.calls[0]["messages"][0]["content"]


def test_unchanged_leads_are_not_redrafted(paths):
    from pipeline.run import resume_run
    with_sender(paths)
    run = run_pipeline(paths, FakeWriter())
    writer = FakeWriter()
    from pipeline.stages import STAGES, Stage
    stages = [Stage(s.number, s.name, (lambda ctx: drafting(ctx, writer)) if s.number == 9 else s.run, s.uses_linkedin)
              for s in STAGES]
    resume_run(paths, run_id=run.id, from_stage=8, stages=stages)
    assert writer.calls == []  # same evidence, drafts kept, style reused
