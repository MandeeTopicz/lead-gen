import pytest

from pipeline.config import load_config
from pipeline.scoring.match import LeadFacts, score_match


@pytest.fixture
def icp(paths):
    return load_config(paths.config_dir).icp


def lead(**overrides) -> LeadFacts:
    """A strong Austin ops leader seen only on a result card (no company data yet)."""
    facts = dict(
        title="VP of Operations",
        company_name="Hill Country Freight",
        location="Austin, Texas, United States",
        months_at_company=6,
        posted_within_days=30,
        keyword_text="VP of Operations Hill Country Freight",
        searches={"S1"},
    )
    facts.update(overrides)
    return LeadFacts(**facts)


def criteria(result):
    return {c.criterion: c for c in result.criteria}


def test_strict_search_lead_scores_100_with_sources(icp):
    result = score_match(lead(keyword_text="VP of Operations Hill Country Freight 3PL"), icp)
    assert result.gates_passed
    assert result.score == 100
    by = criteria(result)
    assert by["role"].source == "card"
    assert by["industry"].source == "search:S1"
    assert by["keywords"].value == "3PL, freight"


def test_one_keyword_on_card_is_partial_credit(icp):
    by = criteria(score_match(lead(), icp))
    assert (by["keywords"].level, by["keywords"].points) == ("partial", 2)


@pytest.mark.parametrize(
    "title, level, points",
    [
        ("Chief Operating Officer", "full", 20),
        ("COO & Co-Founder", "full", 20),
        ("Vice President, Operations", "full", 20),
        ("Senior Vice President of Operations", "full", 20),
        ("Head of Operations", "full", 20),
        ("VP Supply Chain", "partial", 12),
        ("Vice President, Logistics", "partial", 12),
        ("SVP Ops", "full", 20),
        ("Vice President", "partial", 12),
        ("VP", "partial", 12),
        ("Senior Vice President", "partial", 12),
    ],
)
def test_role_levels(icp, title, level, points):
    by = criteria(score_match(lead(title=title), icp))
    assert (by["role"].level, by["role"].points) == (level, points)


@pytest.mark.parametrize(
    "title, reason",
    [
        ("Director of Operations", "director-level or below"),
        ("Operations Manager", "director-level or below"),
        ("Coordinator, Dispatch", "isn't an operations leader"),
        ("Member | Board of Directors", "isn't an operations leader"),
        ("VP of Sales", "isn't an operations leader"),
        ("Vice President, Marketing", "isn't an operations leader"),
    ],
)
def test_role_gate_drops_non_leaders(icp, title, reason):
    result = score_match(lead(title=title), icp)
    assert not result.gates_passed
    assert reason in result.gate_failures[0]


@pytest.mark.parametrize(
    "title, term",
    [("Executive Assistant to the COO", "assistant"), ("Fractional COO", "fractional"), ("COO Advisor", "advisor")],
)
def test_title_exclusions(icp, title, term):
    result = score_match(lead(title=title), icp)
    assert not result.gates_passed
    assert result.excluded == f"title contains '{term}'"


def test_public_and_oversized_companies_are_excluded(icp):
    assert score_match(lead(company_type="Public Company"), icp).excluded == "company type is Public Company"
    assert "over 1000" in score_match(lead(headcount=1500), icp).excluded


def test_no_current_position_is_excluded(icp):
    assert score_match(lead(title=None, company_name=None), icp).excluded == "no current position"


@pytest.mark.parametrize(
    "location, level",
    [
        ("Greater Austin Area", "full"),
        ("Round Rock, Texas, United States", "full"),
        ("Austin, Texas Metropolitan Area", "full"),
        ("Dallas, Texas, United States", "partial"),
        ("Houston, TX", "partial"),
        ("Austin, Minnesota, United States", "none"),
        ("Denver, Colorado, United States", "none"),
    ],
)
def test_geography(icp, location, level):
    assert criteria(score_match(lead(location=location), icp))["geography"].level == level


@pytest.mark.parametrize("months, level, points", [(6, "full", 12), (18, "none", 0), (36, "full", 12), (120, "full", 12)])
def test_tenure(icp, months, level, points):
    by = criteria(score_match(lead(months_at_company=months), icp))
    assert (by["tenure"].level, by["tenure"].points) == (level, points)


def test_card_evidence_beats_search_guarantee(icp):
    # S1 guarantees Greater Austin, but the card says Dallas: trust the card.
    by = criteria(score_match(lead(location="Dallas, Texas, United States"), icp))
    assert (by["geography"].level, by["geography"].source) == ("partial", "card")


def test_best_guarantee_across_searches(icp):
    # Returned by S4 (near-miss size) and S2 (full size): full size credit from S2.
    by = criteria(score_match(lead(searches={"S4", "S2"}), icp))
    assert (by["size"].level, by["size"].source) == ("full", "search:S2")


def test_unknowns_score_zero_without_a_guarantee(icp):
    # S2 drops the activity filter; no spotlight on the card means activity is unknown, not "none".
    by = criteria(score_match(lead(posted_within_days=None, searches={"S2"}), icp))
    assert (by["activity"].level, by["activity"].points) == ("unknown", 0)


def test_wider_searches_give_partial_credit(icp):
    by = criteria(score_match(lead(searches={"S4"}), icp))
    assert (by["size"].level, by["size"].points) == ("partial", 4)


def test_known_company_data_scores_directly(icp):
    facts = lead(industry="Warehousing and Storage", headcount=700, company_type="Privately Held", searches=set())
    by = criteria(score_match(facts, icp))
    assert (by["industry"].level, by["size"].level, by["size"].points) == ("full", "partial", 4)


def test_adjacent_industry_needs_a_logistics_keyword(icp):
    with_keyword = score_match(lead(industry="Manufacturing", searches=set()), icp)
    assert criteria(with_keyword)["industry"].level == "partial"
    without = score_match(lead(industry="Manufacturing", keyword_text="VP of Operations Acme", searches=set()), icp)
    assert not without.gates_passed
    assert "industry" in without.gate_failures[0]


def test_last_mile_spellings_count_once(icp):
    by = criteria(score_match(lead(keyword_text="last mile and last-mile delivery"), icp))
    assert by["keywords"].value == "last mile"
