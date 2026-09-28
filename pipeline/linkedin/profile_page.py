"""Parse Sales Navigator lead and company pages. Pure functions over rendered HTML.

Hooks come from real captures: page sections carry `data-sn-view-name` (feature-lead-top-card,
feature-about-lead, feature-lead-relationship, feature-lead-experience, feature-lead-education,
feature-account-growth-insights, ...) and fields carry `data-anonymize`. Posts have exact `<time datetime>`.
"""

import re
from datetime import datetime

from bs4 import BeautifulSoup, Tag
from pydantic import BaseModel

from pipeline.linkedin.results_page import ParseError

MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


class Role(BaseModel):
    title: str | None = None
    company_name: str | None = None
    company_url: str | None = None
    start: str | None = None  # "YYYY-MM" (or "YYYY")
    end: str | None = None  # None = present
    location: str | None = None
    description: str | None = None


class Post(BaseModel):
    posted_at: str  # ISO timestamp
    text: str | None = None


class LeadProfile(BaseModel):
    headline: str | None = None
    about: str | None = None
    current_roles: list[Role] = []
    experience: list[Role] = []
    education: list[dict] = []
    posts: list[Post] = []
    activity_shown: bool = False  # the Recent activity section rendered (so no posts means none recently)


class CompanyProfile(BaseModel):
    website: str | None = None
    industry: str | None = None
    headcount: int | None = None
    revenue: str | None = None
    location: str | None = None
    description: str | None = None
    growth_6mo: float | None = None  # -0.07 = shrank 7% over six months


def section(soup: BeautifulSoup | Tag, name: str) -> Tag | None:
    return soup.select_one(f'[data-sn-view-name="{name}"]')


def parse_lead_page(html: str) -> LeadProfile:
    soup = BeautifulSoup(html, "html.parser")
    top = section(soup, "feature-lead-top-card")
    if top is None:
        raise ParseError("lead page has no top card")
    about = section(soup, "feature-about-lead")
    activity = section(soup, "feature-lead-relationship")
    return LeadProfile(
        headline=_text(top.select_one('[data-anonymize="headline"]')),
        about=_blurb(about.select_one('[data-anonymize="person-blurb"]')) if about else None,
        current_roles=[_current_role(li) for li in top.select('[data-sn-view-name="lead-current-role"] li')],
        experience=_experience(section(soup, "feature-lead-experience")),
        education=_education(section(soup, "feature-lead-education")),
        posts=_posts(activity),
        activity_shown=activity is not None,
    )


def parse_company_page(html: str) -> CompanyProfile:
    soup = BeautifulSoup(html, "html.parser")
    if section(soup, "feature-account-top-card") is None:
        raise ParseError("company page has no top card")
    # "People also viewed" repeats industry/location for other companies; read the account's own panel only.
    for other in soup.select('[data-sn-view-name="feature-account-people-also-viewed"]'):
        other.decompose()

    def first(attr: str) -> str | None:
        return _text(soup.select_one(f'[data-anonymize="{attr}"]'))

    size = first("company-size")
    count = re.search(r"([\d,]+)", size or "")
    website = soup.select_one('a[data-control-name="visit_company_website"][href^="http"]')
    return CompanyProfile(
        website=website["href"] if website is not None else None,
        industry=first("industry"),
        headcount=int(count.group(1).replace(",", "")) if count else None,
        revenue=first("revenue"),
        location=first("location"),
        description=_blurb(soup.select_one('[data-anonymize="company-blurb"]')),
        growth_6mo=_growth(section(soup, "feature-account-growth-insights")),
    )


def months_since(start: str | None, today: datetime | None = None) -> int | None:
    """'2023-05' -> whole months until today."""
    if not start:
        return None
    today = today or datetime.now()
    year, _, month = start.partition("-")
    return max(0, (today.year - int(year)) * 12 + today.month - int(month or 1))


# --- lead page pieces


def _current_role(li: Tag) -> Role:
    company = li.select_one('a[data-anonymize="company-name"]')
    start, end = _date_range(" ".join(p.get_text(" ") for p in li.select("p")))
    return Role(
        title=_text(li.select_one('[data-anonymize="job-title"]')),
        company_name=_text(company),
        company_url=_company_url(company),
        start=start,
        end=end,
    )


def _experience(sec: Tag | None) -> list[Role]:
    if sec is None:
        return []
    roles = []
    for li in sec.select(":scope ul > li"):
        title = li.select_one('[data-anonymize="job-title"]')
        if title is None:
            continue
        company = li.select_one('[data-anonymize="company-name"]')
        link = company.find_parent("a") if company is not None else None
        paragraphs = [_text(p) for p in li.select("p") if p.get("data-anonymize") != "company-name"]
        start, end = _date_range(" ".join(p for p in paragraphs if p))
        location = next((p for p in paragraphs if p and not _DATES.search(p)), None)
        roles.append(
            Role(
                title=_text(title),
                company_name=_text(company),
                company_url=_company_url(link),
                start=start,
                end=end,
                location=location,
                description=_blurb(li.select_one('[data-anonymize="person-blurb"]')),
            )
        )
    return roles


def _education(sec: Tag | None) -> list[dict]:
    if sec is None:
        return []
    schools = []
    for li in sec.select("li"):
        name = _text(li.select_one('[data-anonymize="education-name"]'))
        if not name:
            continue
        detail = li.select_one("p")
        schools.append({"school": name, "degree": _text(detail)})
    return schools


def _posts(sec: Tag | None) -> list[Post]:
    if sec is None:
        return []
    posts = []
    for article in sec.select("article"):
        time = article.select_one("time[datetime]")
        action = _text(article.select_one("header h4 span")) or ""
        # Only the lead's own posts count as activity; comments and news mentions don't.
        if time is None or not re.search(r"\b(shared|posted|reposted)\b", action):
            continue
        posts.append(Post(posted_at=time["datetime"], text=_post_text(article)))
    return posts


def _post_text(article: Tag) -> str | None:
    """The article opens with a summary: [action, age, text, reactions, comments]. A body span with a title
    attribute holds the full untruncated text when LinkedIn provides one."""
    full = article.select_one("span[title]")
    if full is not None:
        return _blurb(full)
    summary = article.find("span", recursive=False)
    parts = summary.find_all("span", recursive=False) if summary else []
    return _text(parts[2]) if len(parts) > 2 else None


def _growth(sec: Tag | None) -> float | None:
    if sec is None:
        return None
    match = re.search(r"(\d+(?:\.\d+)?) percent growth (increase|decrease) over the last six months", _text(sec) or "")
    if not match:
        return None
    value = float(match.group(1)) / 100
    return -value if match.group(2) == "decrease" else value


# --- helpers

_DATES = re.compile(r"((?:[A-Z][a-z]{2}\s+)?\d{4})\s*[–-]\s*(Present|(?:[A-Z][a-z]{2}\s+)?\d{4})")


def _date_range(text: str) -> tuple[str | None, str | None]:
    match = _DATES.search(text)
    if not match:
        return None, None
    end = None if match.group(2) == "Present" else _month(match.group(2))
    return _month(match.group(1)), end


def _month(text: str) -> str:
    parts = text.split()
    if len(parts) == 2 and parts[0][:3].lower() in MONTHS:
        return f"{parts[1]}-{MONTHS[parts[0][:3].lower()]:02d}"
    return parts[-1]


def _company_url(link: Tag | None) -> str | None:
    if link is None or not link.has_attr("href"):
        return None
    match = re.search(r"/sales/company/(\d+)", link["href"])
    return f"https://www.linkedin.com/sales/company/{match.group(1)}" if match else None


def _blurb(tag: Tag | None) -> str | None:
    """Truncated blurbs keep the full text in their title attribute."""
    if tag is None:
        return None
    return " ".join((tag.get("title") or tag.get_text(" ")).split()) or None


def _text(tag: Tag | None) -> str | None:
    if tag is None:
        return None
    text = " ".join(tag.get_text(" ").split())
    return text or None
