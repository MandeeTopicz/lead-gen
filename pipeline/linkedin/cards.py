"""A Sales Navigator people-search result card, as the parser hands it to the rest of the pipeline.

The HTML parser (built against captured result pages) fills these fields; everything downstream works
from this model, so scoring and storage are tested without LinkedIn.
"""

import re
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict

# Normalized spotlight badges the parser emits.
POSTED_RECENTLY = "posted_on_linkedin"  # "N recent posts on LinkedIn" = posted in the past 30 days
CHANGED_JOBS = "changed_jobs"  # Sales Navigator's "recently hired": changed jobs in the past 90 days
POSTED_RECENTLY_DAYS = 30


class Card(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_url: str
    full_name: str
    title: str | None = None
    company_name: str | None = None
    company_url: str | None = None
    location: str | None = None
    time_in_role: str | None = None  # raw card text, e.g. "2 years 3 months in role"
    time_in_company: str | None = None  # e.g. "5 years 1 month in company"
    connection_degree: int | None = None
    mutual_connections: int | None = None
    spotlights: list[str] = []
    about: str | None = None


LEAD_PATH = re.compile(r"^/sales/lead/([^,/]+)")
COMPANY_PATH = re.compile(r"^/sales/company/(\d+)")


def canonical_lead_url(url: str) -> str:
    """Sales Navigator lead links carry per-search tokens (`/sales/lead/<id>,NAME_SEARCH,<token>?...`).
    Keep only the stable member id, so the same person dedupes across searches and runs."""
    path = urlparse(url).path
    match = LEAD_PATH.match(path)
    if not match:
        raise ValueError(f"not a Sales Navigator lead URL: {url}")
    return f"https://www.linkedin.com/sales/lead/{match.group(1)}"


def canonical_company_url(url: str | None) -> str | None:
    if not url:
        return None
    match = COMPANY_PATH.match(urlparse(url).path)
    return f"https://www.linkedin.com/sales/company/{match.group(1)}" if match else None


_YEARS = re.compile(r"(\d+)\s*(?:years?|yrs?)", re.IGNORECASE)
_MONTHS = re.compile(r"(\d+)\s*(?:months?|mos?)", re.IGNORECASE)
_UNDER_A_YEAR = re.compile(r"less than (?:a|1|one) year", re.IGNORECASE)


def tenure_months(text: str | None) -> int | None:
    """'3 years 2 months in company' -> 38. None when the card doesn't say."""
    if not text:
        return None
    if _UNDER_A_YEAR.search(text):
        return 6  # the card only says "under a year"; any value under 12 scores the same
    years = _YEARS.search(text)
    months = _MONTHS.search(text)
    if not years and not months:
        return None
    return (int(years.group(1)) if years else 0) * 12 + (int(months.group(1)) if months else 0)
