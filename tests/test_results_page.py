from pathlib import Path

import pytest

from pipeline.linkedin.cards import CHANGED_JOBS, POSTED_RECENTLY, canonical_lead_url, tenure_months
from pipeline.linkedin.results_page import has_next_page, parse_results
from pipeline.linkedin.searches import saved_searches_in_panel
from tests.linkedin_pages import FIXTURES, fixture_html

CAPTURED = FIXTURES.parent / "captured"


def test_parses_every_card_field():
    first, second = parse_results(fixture_html("search_results"))
    assert canonical_lead_url(first.profile_url) == "https://www.linkedin.com/sales/lead/ACwAAAtest1"
    assert (first.full_name, first.title, first.company_name) == ("Jordan Reyes", "VP of Operations", "Hill Country Freight")
    assert first.company_url == "https://www.linkedin.com/sales/company/4242?_ntb=x"
    assert first.location == "Round Rock, Texas, United States"
    assert tenure_months(first.time_in_role) == 8
    assert tenure_months(first.time_in_company) == 38
    assert (first.connection_degree, first.mutual_connections) == (2, 1204)
    assert first.spotlights == sorted([CHANGED_JOBS, POSTED_RECENTLY, "second_degree_connection"])
    assert first.about.startswith("Running a 3PL network")  # full text from the blurb's title, not the truncation


def test_company_without_a_page_and_under_a_year_tenure():
    second = parse_results(fixture_html("search_results"))[1]
    assert (second.company_name, second.company_url) == ("Pecan Street Logistics", None)
    assert second.connection_degree == 3
    assert tenure_months(second.time_in_company) < 12


def test_unrendered_rows_are_skipped():
    assert len(parse_results(fixture_html("search_results"))) == 2


def test_next_page_button():
    html = fixture_html("search_results")
    assert has_next_page(html)
    assert not has_next_page(html.replace('<button aria-label="Next">', '<button aria-label="Next" disabled>'))


def test_saved_search_panel_maps_names_to_links():
    assert saved_searches_in_panel(fixture_html("saved_searches_panel")) == {
        "S1-strict": "https://www.linkedin.com/sales/search/people?savedSearchId=111",
        "S2-no-activity": "https://www.linkedin.com/sales/search/people?savedSearchId=222",
    }


@pytest.mark.skipif(not (CAPTURED / "results-p1.html").exists(), reason="no real results capture")
def test_real_results_capture_parses_completely():
    cards = parse_results((CAPTURED / "results-p1.html").read_text())
    assert len(cards) >= 20
    for field in ("title", "company_name", "location", "time_in_company", "connection_degree"):
        assert all(getattr(c, field) is not None for c in cards), field


@pytest.mark.skipif(not (CAPTURED / "saved-searches.html").exists(), reason="no real panel capture")
def test_real_saved_searches_panel_parses():
    saved = saved_searches_in_panel((CAPTURED / "saved-searches.html").read_text())
    assert saved and all("savedSearchId=" in url for url in saved.values())
    assert not any(name == "View" or "new result" in name for name in saved)
