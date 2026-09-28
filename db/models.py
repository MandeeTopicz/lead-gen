"""SQLite schema. Timestamps are ISO 8601 strings in local time with offset.

Leads and companies persist across runs; scores, plans, and dossiers belong to the run that produced them.
"""

from typing import Any

from sqlalchemy import JSON, CheckConstraint, Column
from sqlmodel import Field, SQLModel


def json_column() -> Any:
    return Field(default=None, sa_column=Column(JSON))


class Run(SQLModel, table=True):
    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint("trigger IN ('cron','manual')", name="ck_runs_trigger"),
        CheckConstraint("status IN ('running','completed','halted','failed')", name="ck_runs_status"),
    )

    id: int | None = Field(default=None, primary_key=True)
    started_at: str
    finished_at: str | None = None
    trigger: str
    status: str
    dry_run: bool = False
    halt_reason: str | None = None
    halt_screenshot_path: str | None = None
    pages_viewed: int = 0
    profiles_read: int = 0
    leads_found: int = 0
    leads_qualified: int = 0
    llm_cost_usd: float = 0
    enrichment_cost_usd: float = 0
    icp_version: str


class RunStage(SQLModel, table=True):
    """One row per stage attempt's latest state, so a halted run can resume where it stopped."""

    __tablename__ = "run_stages"
    __table_args__ = (
        CheckConstraint("status IN ('running','completed','halted','failed')", name="ck_run_stages_status"),
    )

    run_id: int = Field(foreign_key="runs.id", primary_key=True)
    stage: int = Field(primary_key=True)
    name: str
    status: str
    started_at: str
    finished_at: str | None = None
    error: str | None = None


class Company(SQLModel, table=True):
    __tablename__ = "companies"

    id: int | None = Field(default=None, primary_key=True)
    linkedin_url: str | None = Field(default=None, unique=True)
    name: str
    domain: str | None = None
    industry: str | None = None
    company_type: str | None = None
    headcount: int | None = None
    headcount_growth_6mo: float | None = None
    hq_location: str | None = None
    open_roles: list | None = json_column()
    news: list | None = json_column()
    description: str | None = None
    updated_at: str | None = None


class Lead(SQLModel, table=True):
    __tablename__ = "leads"

    id: int | None = Field(default=None, primary_key=True)
    profile_url: str = Field(unique=True)
    full_name: str
    headline: str | None = None
    current_title: str | None = None
    company_id: int | None = Field(default=None, foreign_key="companies.id")
    location: str | None = None
    connection_degree: int | None = None
    started_current_role: str | None = None  # ISO date
    experience: list | None = json_column()
    education: list | None = json_column()
    about: str | None = None
    communication_style: dict | None = json_column()  # formality, length, emoji use
    first_seen_run_id: int | None = Field(default=None, foreign_key="runs.id")
    last_deep_read_at: str | None = None
    last_researched_at: str | None = None
    last_in_digest_run_id: int | None = Field(default=None, foreign_key="runs.id")


class SearchHit(SQLModel, table=True):
    __tablename__ = "search_hits"

    run_id: int = Field(foreign_key="runs.id", primary_key=True)
    lead_id: int = Field(foreign_key="leads.id", primary_key=True)
    search_code: str = Field(primary_key=True)  # S1..S7
    card_data: dict | None = json_column()


class Post(SQLModel, table=True):
    __tablename__ = "posts"

    id: int | None = Field(default=None, primary_key=True)
    lead_id: int = Field(foreign_key="leads.id")
    url: str | None = Field(default=None, unique=True)
    posted_at: str | None = None
    text: str | None = None
    topics: list | None = json_column()


class Finding(SQLModel, table=True):
    """A research or enrichment fact. Drafts may only cite findings that aren't unverified."""

    __tablename__ = "findings"
    __table_args__ = (
        CheckConstraint("confidence IN ('verified','likely','unverified')", name="ck_findings_confidence"),
    )

    id: int | None = Field(default=None, primary_key=True)
    lead_id: int | None = Field(default=None, foreign_key="leads.id")
    company_id: int | None = Field(default=None, foreign_key="companies.id")
    kind: str  # email, phone, platform, talk, article, hiring, news, local_tie, affinity, company
    value: str
    source_url: str | None = None
    source_provider: str | None = None
    confidence: str
    found_at: str
    run_id: int | None = Field(default=None, foreign_key="runs.id")


class Score(SQLModel, table=True):
    __tablename__ = "scores"

    run_id: int = Field(foreign_key="runs.id", primary_key=True)
    lead_id: int = Field(foreign_key="leads.id", primary_key=True)
    gates_passed: bool
    match_score: float | None = None
    match_breakdown: dict | None = json_column()
    response_score: float | None = None
    response_breakdown: dict | None = json_column()
    response_label: str | None = None
    priority_score: float | None = None
    reasons: list | None = json_column()  # [{text, finding_ids}]


class OutreachStep(SQLModel, table=True):
    __tablename__ = "outreach_steps"
    __table_args__ = (
        CheckConstraint(
            "channel IN ('linkedin_connect','linkedin_message','inmail','email','call')",
            name="ck_outreach_steps_channel",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="runs.id")
    lead_id: int = Field(foreign_key="leads.id")
    step_code: str  # L1..L10, E1, E2, C1, C2
    channel: str
    planned_date: str | None = None
    angle: str | None = None
    subject: str | None = None
    body: str | None = None
    cited_finding_ids: list | None = json_column()
    checks_passed: bool = False


class Dossier(SQLModel, table=True):
    __tablename__ = "dossiers"

    run_id: int = Field(foreign_key="runs.id", primary_key=True)
    lead_id: int = Field(foreign_key="leads.id", primary_key=True)
    rank: int
    md_path: str | None = None
    docx_path: str | None = None
    pdf_path: str | None = None


class Review(SQLModel, table=True):
    __tablename__ = "reviews"
    __table_args__ = (
        CheckConstraint("fit IN ('good','not_fit')", name="ck_reviews_fit"),
        CheckConstraint(
            "sequence_status IN ('active','replied','stopped','done')", name="ck_reviews_sequence_status"
        ),
    )

    lead_id: int = Field(foreign_key="leads.id", primary_key=True)
    fit: str | None = None
    contacted: bool = False
    sequence_status: str | None = None
    notes: str | None = None
    updated_at: str | None = None
