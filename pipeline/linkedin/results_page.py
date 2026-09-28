"""Parse a rendered Sales Navigator people-search results page into Cards. Pure functions over HTML.

Hooks come from a real capture: each result is `[data-x-search-result="LEAD"]`, and its fields carry
`data-anonymize` attributes (person-name, title, company-name, location, job-title, person-blurb).
Spotlight badges are buttons named `search_spotlight_<kind>`.
"""

import re

from bs4 import BeautifulSoup, Tag

from pipeline.linkedin.cards import CHANGED_JOBS, POSTED_RECENTLY, Card

RESULT = '[data-x-search-result="LEAD"]'

# Sales Navigator spotlight kinds -> the names the pipeline uses. Unlisted kinds are kept as-is.
SPOTLIGHTS = {
    "posted_on_linkedin": POSTED_RECENTLY,
    "recently_hired": CHANGED_JOBS,
}

_DEGREE = re.compile(r"·\s*(1st|2nd|3rd)")
_TENURE = re.compile(r"((?:less than (?:a|1|one) year)|(?:\d+\s+(?:years?|months?)\s*)+)\s*in\s+(role|company)", re.I)
_COUNT = re.compile(r"(\d[\d,]*)")


class ParseError(Exception):
    pass


def parse_results(html: str) -> list[Card]:
    soup = BeautifulSoup(html, "html.parser")
    results = soup.select(RESULT)
    cards = [card for result in results if (card := _parse_result(result)) is not None]
    if results and not cards:
        raise ParseError(f"{len(results)} results on the page but none could be parsed")
    return cards


def rendered_result_count(html: str) -> int:
    return len(BeautifulSoup(html, "html.parser").select(RESULT))


def has_next_page(html: str) -> bool:
    button = BeautifulSoup(html, "html.parser").select_one('button[aria-label="Next"]')
    return button is not None and not button.has_attr("disabled")


def is_empty_search(html: str) -> bool:
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True).lower()
    return "no leads matched your search" in text or "no results found" in text


def _parse_result(result: Tag) -> Card | None:
    name_link = result.select_one('a[data-control-name="view_lead_panel_via_search_lead_name"]') or result.select_one(
        'a[href*="/sales/lead/"]'
    )
    name = _text(result.select_one('[data-anonymize="person-name"]'))
    if name_link is None or not name:
        return None  # an unrendered or out-of-network row; skipped rather than guessed

    company = result.select_one('[data-anonymize="company-name"]')
    tenure = {kind.lower(): f"{amount.strip()} in {kind.lower()}" for amount, kind in _TENURE.findall(
        _text(result.select_one('[data-anonymize="job-title"]')) or ""
    )}
    blurb = result.select_one('[data-anonymize="person-blurb"]')
    degree = _DEGREE.search(result.get_text(" "))

    spotlights, mutual = [], None
    for button in result.select('[data-control-name^="search_spotlight_"]'):
        kind = button["data-control-name"].removeprefix("search_spotlight_")
        spotlights.append(SPOTLIGHTS.get(kind, kind))
        if kind == "second_degree_connection":
            count = _COUNT.search(button.get_text(" "))
            mutual = int(count.group(1).replace(",", "")) if count else None

    return Card(
        profile_url=_absolute(name_link["href"]),
        full_name=name,
        title=_text(result.select_one('[data-anonymize="title"]')),
        company_name=_text(company),
        company_url=_absolute(company["href"]) if company is not None and company.has_attr("href") else None,
        location=_text(result.select_one('[data-anonymize="location"]')),
        time_in_role=tenure.get("role"),
        time_in_company=tenure.get("company"),
        connection_degree=int(degree.group(1)[0]) if degree else None,
        mutual_connections=mutual,
        spotlights=sorted(set(spotlights)),
        about=(blurb.get("title") or _text(blurb)) if blurb is not None else None,
    )


def _text(tag: Tag | None) -> str | None:
    if tag is None:
        return None
    text = " ".join(tag.get_text(" ").split())
    return text or None


def _absolute(href: str) -> str:
    return href if href.startswith("http") else f"https://www.linkedin.com{href}"
