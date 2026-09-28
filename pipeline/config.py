"""Load and validate config/icp.yaml and config/sender.yaml."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- icp.yaml ---------------------------------------------------------------


class Gates(Strict):
    titles: list[str] = Field(min_length=1)
    related_titles: list[str] = []
    bare_titles: list[str] = []
    industries: list[str] = Field(min_length=1)
    adjacent_industries: list[str] = []


class Exclude(Strict):
    title_terms: list[str] = []
    company_types: list[str] = []
    max_headcount: int = Field(gt=0)


class Geography(Strict):
    primary: str
    primary_places: list[str] = []
    partial: str


class Tenure(Strict):
    full_if_months_lt: int = Field(gt=0)
    full_if_years_gte: int = Field(gt=0)


class Company(Strict):
    size_full: tuple[int, int]
    size_partial: list[tuple[int, int]] = []
    type: str


class IcpDefinition(Strict):
    name: str
    version: int = Field(ge=1)
    gates: Gates
    exclude: Exclude
    geography: Geography
    tenure: Tenure
    company: Company
    keywords: list[str] = []


Criterion = Literal["role", "industry", "geography", "activity", "tenure", "size", "company_type", "keywords"]
CRITERIA: tuple[Criterion, ...] = (
    "role", "industry", "geography", "activity", "tenure", "size", "company_type", "keywords",
)
Level = Literal["full", "partial"]


class SavedSearch(Strict):
    name: str = Field(min_length=1)
    enabled: bool = True
    drops: list[Criterion] = []
    widens: list[Criterion] = []


class MatchWeights(Strict):
    role: int = Field(ge=0)
    industry: int = Field(ge=0)
    geography: int = Field(ge=0)
    activity: int = Field(ge=0)
    tenure: int = Field(ge=0)
    size: int = Field(ge=0)
    company_type: int = Field(ge=0)
    keywords: int = Field(ge=0)


class ResponseWeights(Strict):
    activity: int = Field(ge=0)
    affinity: int = Field(ge=0)
    local: int = Field(ge=0)
    trigger: int = Field(ge=0)
    reach: int = Field(ge=0)
    new_in_role: int = Field(ge=0)


class PriorityWeights(Strict):
    match: float = Field(ge=0, le=1)
    response: float = Field(ge=0, le=1)


class Weights(Strict):
    match: MatchWeights
    response: ResponseWeights
    priority: PriorityWeights

    @model_validator(mode="after")
    def _totals(self):
        if sum(self.match.model_dump().values()) != 100:
            raise ValueError("weights.match must sum to 100")
        if sum(self.response.model_dump().values()) != 100:
            raise ValueError("weights.response must sum to 100")
        if abs(self.priority.match + self.priority.response - 1) > 1e-9:
            raise ValueError("weights.priority must sum to 1")
        return self


class ActivityMatchPoints(Strict):
    full_within_days: int = Field(gt=0)
    partial_within_days: int = Field(gt=0)
    partial: int = Field(ge=0)


class KeywordMatchPoints(Strict):
    full_min_matches: int = Field(ge=1)
    one_match: int = Field(ge=0)


class MatchPoints(Strict):
    role_related: int = Field(ge=0)
    industry_adjacent: int = Field(ge=0)
    geography_partial: int = Field(ge=0)
    activity: ActivityMatchPoints
    tenure_1_to_3_years: int = Field(ge=0)
    size_partial: int = Field(ge=0)
    company_type_other: int = Field(ge=0)
    keywords: KeywordMatchPoints


class ActivityResponsePoints(Strict):
    within_7_days: int = Field(ge=0)
    within_30_days: int = Field(ge=0)
    within_90_days: int = Field(ge=0)


class AffinityPoints(Strict):
    shared_connections: int = Field(ge=0)
    shared_background: int = Field(ge=0)


class TriggerPoints(Strict):
    hiring: int = Field(ge=0)
    news: int = Field(ge=0)
    news_within_days: int = Field(gt=0)


class ReachPoints(Strict):
    verified_email: int = Field(ge=0)
    phone: int = Field(ge=0)
    connection_or_open_profile: int = Field(ge=0)


class NewInRolePoints(Strict):
    points: int = Field(ge=0)
    within_months: int = Field(gt=0)


class ResponseLabels(Strict):
    high: int = Field(ge=0, le=100)
    medium: int = Field(ge=0, le=100)


class ResponsePoints(Strict):
    activity: ActivityResponsePoints
    affinity: AffinityPoints
    local: int = Field(ge=0)
    trigger: TriggerPoints
    reach: ReachPoints
    new_in_role: NewInRolePoints
    labels: ResponseLabels


class QualityBar(Strict):
    min_match: int = Field(ge=0, le=100)
    max_leads: int = Field(gt=0)
    warn_below: int = Field(ge=0)
    suppress_days: int = Field(ge=0)


class Caps(Strict):
    result_pages: int = Field(ge=0)
    pages_per_search: int = Field(gt=0)
    deep_reads: int = Field(ge=0)
    deep_read_refresh_days: int = Field(gt=0)
    scheduled_runs_per_day: int = Field(ge=0)
    manual_runs_per_day: int = Field(ge=0)
    delay_seconds: tuple[float, float]

    @model_validator(mode="after")
    def _delay_range(self):
        low, high = self.delay_seconds
        if not 0 < low <= high:
            raise ValueError("caps.delay_seconds must be [min, max] with 0 < min <= max")
        return self


class Browser(Strict):
    channel: Literal["chrome", "chromium"]
    headless: bool
    start_url: str
    page_timeout_seconds: int = Field(gt=0)


class Llm(Strict):
    fast_model: str = Field(min_length=1)
    writer_model: str = Field(min_length=1)


class Research(Strict):
    model: str = Field(min_length=1)
    max_leads: int = Field(ge=0)
    max_searches: int = Field(gt=0)
    max_fetches: int = Field(ge=0)
    refresh_days: int = Field(gt=0)
    max_usd_per_run: float = Field(ge=0)


class Drafting(Strict):
    max_usd_per_run: float = Field(ge=0)


class LinkedInSequence(Strict):
    touches: int = Field(ge=0)
    per_week: int = Field(gt=0)
    inmail_fallback_after_days: int = Field(gt=0)


class FollowUpSequence(Strict):
    touches: int = Field(ge=0)
    start_week: int = Field(gt=0)


class Sequence(Strict):
    linkedin: LinkedInSequence
    email: FollowUpSequence
    calls: FollowUpSequence


class IcpConfig(Strict):
    icp: IcpDefinition
    search_baseline: dict[Criterion, Level]
    searches: dict[str, SavedSearch] = Field(min_length=1)
    weights: Weights
    match_points: MatchPoints
    response_points: ResponsePoints
    quality_bar: QualityBar
    caps: Caps
    browser: Browser
    llm: Llm
    research: Research
    drafting: Drafting
    sequence: Sequence

    @model_validator(mode="after")
    def _points_within_weights(self):
        w, mp, rp = self.weights.match, self.match_points, self.response_points
        partials = {
            "role_related": (mp.role_related, w.role),
            "industry_adjacent": (mp.industry_adjacent, w.industry),
            "geography_partial": (mp.geography_partial, w.geography),
            "activity.partial": (mp.activity.partial, w.activity),
            "tenure_1_to_3_years": (mp.tenure_1_to_3_years, w.tenure),
            "size_partial": (mp.size_partial, w.size),
            "company_type_other": (mp.company_type_other, w.company_type),
            "keywords.one_match": (mp.keywords.one_match, w.keywords),
        }
        for name, (points, cap) in partials.items():
            if points > cap:
                raise ValueError(f"match_points.{name} ({points}) exceeds its weight ({cap})")
        if max(rp.activity.model_dump().values()) > self.weights.response.activity:
            raise ValueError("response_points.activity exceeds weights.response.activity")
        if rp.local > self.weights.response.local:
            raise ValueError("response_points.local exceeds weights.response.local")
        if rp.new_in_role.points > self.weights.response.new_in_role:
            raise ValueError("response_points.new_in_role exceeds weights.response.new_in_role")
        if rp.labels.medium > rp.labels.high:
            raise ValueError("response_points.labels.medium must not exceed labels.high")
        return self

    @model_validator(mode="after")
    def _baseline_covers_every_criterion(self):
        missing = [c for c in CRITERIA if c not in self.search_baseline]
        if missing:
            raise ValueError(f"search_baseline is missing: {', '.join(missing)}")
        return self

    @model_validator(mode="after")
    def _search_codes(self):
        for code in self.searches:
            if not (code.startswith("S") and code[1:].isdigit()):
                raise ValueError(f"search code {code!r} must look like S1, S2, ...")
        return self

    def guarantees(self, search_code: str) -> dict[Criterion, Level]:
        """What a lead returned by this saved search is known to satisfy, from the search's filters alone."""
        search = self.searches[search_code]
        levels = {c: level for c, level in self.search_baseline.items() if c not in search.drops}
        for criterion in search.widens:
            levels[criterion] = "partial"
        return levels

    @property
    def enabled_searches(self) -> dict[str, SavedSearch]:
        return {code: search for code, search in self.searches.items() if search.enabled}

    @property
    def version_tag(self) -> str:
        return f"{self.icp.name}@v{self.icp.version}"


# --- sender.yaml ------------------------------------------------------------


class SenderIdentity(Strict):
    name: str = ""
    title: str = ""
    company: str = ""
    email: str = ""
    phone: str = ""
    physical_address: str = ""
    local_to_austin: bool = True


class Background(Strict):
    employers: list[str] = []
    schools: list[str] = []
    communities: list[str] = []


class SenderConfig(Strict):
    demo: bool = False  # a fictional sender for testing: outputs carry a "do not send" banner
    sender: SenderIdentity
    offer: str = ""
    proof_points: list[str] = []
    past_clients: list[str] = []
    background: Background = Background()

    def missing_for_drafting(self) -> list[str]:
        """Fields drafting needs before it can write a single message."""
        required = {
            "sender.name": self.sender.name,
            "sender.company": self.sender.company,
            "sender.physical_address": self.sender.physical_address,
            "offer": self.offer,
        }
        return [name for name, value in required.items() if not value.strip()]


# --- loading ----------------------------------------------------------------


class Config(Strict):
    icp: IcpConfig
    sender: SenderConfig


def _read_yaml(path: Path) -> dict:
    with path.open() as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return data


def load_config(config_dir: Path) -> Config:
    return Config(
        icp=IcpConfig.model_validate(_read_yaml(config_dir / "icp.yaml")),
        sender=SenderConfig.model_validate(_read_yaml(config_dir / "sender.yaml")),
    )
