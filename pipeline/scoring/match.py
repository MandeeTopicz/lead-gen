"""Gates, exclusions, and the ICP match score (0-100).

Every criterion lands at a level (full, partial, none, unknown) with the evidence behind it, so each point
in the breakdown traces to a source: the result card, a saved search's filters, or (from iteration 4) the
profile and company pages. Direct evidence always beats a search's guarantee.
"""

import re
from dataclasses import dataclass, field
from typing import Literal

from pipeline.config import CRITERIA, Criterion, IcpConfig

Level = Literal["full", "partial", "none", "unknown"]
LEVEL_RANK = {"full": 2, "partial": 1}


@dataclass
class LeadFacts:
    """What is known about a lead, each value paired with where it came from (see `sources`)."""

    title: str | None = None
    company_name: str | None = None
    location: str | None = None
    months_at_company: int | None = None
    posted_within_days: int | None = None
    industry: str | None = None
    headcount: int | None = None
    company_type: str | None = None
    keyword_text: str = ""
    searches: set[str] = field(default_factory=set)
    sources: dict[str, str] = field(default_factory=dict)


@dataclass
class CriterionScore:
    criterion: Criterion
    level: Level
    points: int
    max_points: int
    value: str | None
    source: str | None

    def as_dict(self) -> dict:
        return {
            "criterion": self.criterion,
            "level": self.level,
            "points": self.points,
            "max": self.max_points,
            "value": self.value,
            "source": self.source,
        }


@dataclass
class MatchResult:
    gates_passed: bool
    score: float
    criteria: list[CriterionScore]
    excluded: str | None = None
    gate_failures: list[str] = field(default_factory=list)

    def breakdown(self) -> dict:
        return {
            "criteria": [c.as_dict() for c in self.criteria],
            "excluded": self.excluded,
            "gate_failures": self.gate_failures,
        }


def score_match(facts: LeadFacts, icp: IcpConfig) -> MatchResult:
    direct = {
        "role": _role(facts, icp),
        "industry": _industry(facts, icp),
        "geography": _geography(facts, icp),
        "activity": _activity(facts, icp),
        "tenure": _tenure(facts, icp),
        "size": _size(facts, icp),
        "company_type": _company_type(facts, icp),
        "keywords": _keywords(facts, icp),
    }
    guaranteed = _search_guarantees(facts, icp)
    criteria = [_resolve(c, direct[c], guaranteed.get(c), facts, icp) for c in CRITERIA]
    by_name = {c.criterion: c for c in criteria}

    excluded = _exclusion(facts, icp)
    gate_failures = []
    if by_name["role"].level not in ("full", "partial"):
        gate_failures.append(_role_gate_reason(facts))
    if by_name["industry"].level not in ("full", "partial"):
        gate_failures.append(f"industry not logistics or supply chain ({facts.industry or 'unknown'})")

    return MatchResult(
        gates_passed=excluded is None and not gate_failures,
        score=float(sum(c.points for c in criteria)),
        criteria=criteria,
        excluded=excluded,
        gate_failures=gate_failures,
    )


# --- evidence per criterion: (level, value shown in the breakdown) or None when there is no direct evidence


def _role(facts: LeadFacts, icp: IcpConfig) -> tuple[Level, str] | None:
    if not facts.title:
        return None
    title = _normalize_title(facts.title)
    if any(_contains_phrase(title, _normalize_title(t)) for t in icp.icp.gates.titles):
        return "full", facts.title
    if any(_contains_phrase(title, _normalize_title(t)) for t in icp.icp.gates.related_titles):
        return "partial", facts.title
    return "none", facts.title


def _industry(facts: LeadFacts, icp: IcpConfig) -> tuple[Level, str] | None:
    if not facts.industry:
        return None
    gates = icp.icp.gates
    if facts.industry in gates.industries:
        return "full", facts.industry
    if facts.industry in gates.adjacent_industries and _keyword_matches(facts, icp):
        return "partial", facts.industry
    return "none", facts.industry


def _geography(facts: LeadFacts, icp: IcpConfig) -> tuple[Level, str] | None:
    if not facts.location:
        return None
    location = facts.location.lower()
    in_texas = "texas" in location or re.search(r"\btx\b", location) is not None
    geo = icp.icp.geography
    if geo.primary.lower() in location:
        return "full", facts.location
    if in_texas and any(_contains_phrase(location, place.lower()) for place in geo.primary_places):
        return "full", facts.location
    if in_texas:
        return "partial", facts.location
    return "none", facts.location


def _activity(facts: LeadFacts, icp: IcpConfig) -> tuple[Level, str] | None:
    if facts.posted_within_days is None:
        return None
    rules = icp.match_points.activity
    value = f"posted within {facts.posted_within_days} days"
    if facts.posted_within_days <= rules.full_within_days:
        return "full", value
    if facts.posted_within_days <= rules.partial_within_days:
        return "partial", value
    return "none", value


def _tenure(facts: LeadFacts, icp: IcpConfig) -> tuple[Level, str] | None:
    months = facts.months_at_company
    if months is None:
        return None
    rules = icp.icp.tenure
    value = f"{months // 12}y {months % 12}m at company"
    if months < rules.full_if_months_lt or months >= rules.full_if_years_gte * 12:
        return "full", value
    return "none", value


def _size(facts: LeadFacts, icp: IcpConfig) -> tuple[Level, str] | None:
    if facts.headcount is None:
        return None
    company = icp.icp.company
    value = f"{facts.headcount} employees"
    low, high = company.size_full
    if low <= facts.headcount <= high:
        return "full", value
    if any(lo <= facts.headcount <= hi for lo, hi in company.size_partial):
        return "partial", value
    return "none", value


def _company_type(facts: LeadFacts, icp: IcpConfig) -> tuple[Level, str] | None:
    if not facts.company_type:
        return None
    return ("full" if facts.company_type == icp.icp.company.type else "none"), facts.company_type


def _keywords(facts: LeadFacts, icp: IcpConfig) -> tuple[Level, str] | None:
    matches = _keyword_matches(facts, icp)
    if not matches:
        return None  # the card shows little text; absence here isn't evidence of no keywords
    value = ", ".join(matches)
    if len(matches) >= icp.match_points.keywords.full_min_matches:
        return "full", value
    return "partial", value


# --- combining evidence into points


def _search_guarantees(facts: LeadFacts, icp: IcpConfig) -> dict[Criterion, tuple[str, str]]:
    """Best level any returning search guarantees per criterion, with the search that guarantees it."""
    best: dict[Criterion, tuple[str, str]] = {}
    for code in sorted(facts.searches):
        if code not in icp.searches:
            continue
        for criterion, level in icp.guarantees(code).items():
            current = best.get(criterion)
            if current is None or LEVEL_RANK[level] > LEVEL_RANK[current[0]]:
                best[criterion] = (level, code)
    return best


def _resolve(
    criterion: Criterion,
    direct: tuple[Level, str] | None,
    guaranteed: tuple[str, str] | None,
    facts: LeadFacts,
    icp: IcpConfig,
) -> CriterionScore:
    max_points = getattr(icp.weights.match, criterion)
    if direct is not None:
        level, value = direct
        # Keywords: the card shows only a slice of the profile, so a search's keyword filter can add to it.
        if criterion == "keywords" and guaranteed and LEVEL_RANK[guaranteed[0]] > LEVEL_RANK.get(level, 0):
            level, source = guaranteed[0], f"search:{guaranteed[1]}"
        else:
            source = facts.sources.get(criterion, "card")
    elif guaranteed is not None:
        level, value, source = guaranteed[0], None, f"search:{guaranteed[1]}"
    else:
        level, value, source = "unknown", None, None
    return CriterionScore(criterion, level, _points(criterion, level, max_points, icp), max_points, value, source)


def _points(criterion: Criterion, level: Level, max_points: int, icp: IcpConfig) -> int:
    if level == "full":
        return max_points
    if level == "unknown":
        return 0
    mp = icp.match_points
    partial = {
        "role": mp.role_related,
        "industry": mp.industry_adjacent,
        "geography": mp.geography_partial,
        "activity": mp.activity.partial,
        "size": mp.size_partial,
        "keywords": mp.keywords.one_match,
        "tenure": 0,
        "company_type": 0,
    }
    none = {"tenure": mp.tenure_1_to_3_years, "company_type": mp.company_type_other}
    return partial[criterion] if level == "partial" else none.get(criterion, 0)


# --- exclusions and gates


def _exclusion(facts: LeadFacts, icp: IcpConfig) -> str | None:
    exclude = icp.icp.exclude
    if not facts.title and not facts.company_name:
        return "no current position"
    title = (facts.title or "").lower()
    for term in exclude.title_terms:
        if _contains_phrase(title, term.lower()):
            return f"title contains '{term}'"
    if facts.company_type and facts.company_type in exclude.company_types:
        return f"company type is {facts.company_type}"
    if facts.headcount is not None and facts.headcount > exclude.max_headcount:
        return f"company has {facts.headcount} employees (over {exclude.max_headcount})"
    return None


def _role_gate_reason(facts: LeadFacts) -> str:
    title = (facts.title or "").lower()
    if not title:
        return "no title"
    if "director" in title or "manager" in title:
        return f"director-level or below ({facts.title})"
    return f"title isn't an operations leader ({facts.title})"


# --- text helpers

_TITLE_NOISE = re.compile(r"[,&/()|\-–—.]")
_TITLE_FILLER = {"of", "the", "and"}
_TITLE_ABBREVIATIONS = {
    "vp": "vice president",
    "svp": "senior vice president",
    "evp": "executive vice president",
    "sr": "senior",
    "coo": "chief operating officer",
    "ops": "operations",
}


def _normalize_title(title: str) -> str:
    """'VP, Operations', 'Vice President of Operations', and 'VP Ops' all normalize the same."""
    words = _TITLE_NOISE.sub(" ", title.lower()).split()
    return " ".join(_TITLE_ABBREVIATIONS.get(w, w) for w in words if w not in _TITLE_FILLER)


def _contains_phrase(text: str, phrase: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text) is not None


def _keyword_matches(facts: LeadFacts, icp: IcpConfig) -> list[str]:
    text = facts.keyword_text.lower()
    found: list[str] = []
    for keyword in icp.icp.keywords:
        if _contains_phrase(text, keyword.lower()) and keyword.lower() not in {f.lower() for f in found}:
            found.append(keyword)
    # "last mile" and "last-mile" are the same keyword; count it once.
    if "last mile" in [f.lower() for f in found] and "last-mile" in [f.lower() for f in found]:
        found = [f for f in found if f.lower() != "last-mile"]
    return found
