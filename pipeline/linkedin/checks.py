"""Decide from a loaded page whether the run must halt. Pure functions, so they're tested on saved pages.

When in doubt this halts: a false halt costs one run, a missed warning can cost the account.
Signals come from public descriptions of LinkedIn's login, checkpoint, and restriction pages; refine them
with real captures (every halt saves the page's HTML next to its screenshot).
"""

import re
from urllib.parse import urlparse

# Checked in order against the URL path; the first match wins.
HALT_PATHS: list[tuple[str, str]] = [
    ("/checkpoint", "LinkedIn security checkpoint"),
    ("/authwall", "logged out (authwall)"),
    ("/sales/login", "logged out of Sales Navigator"),
    ("/uas/login", "logged out (login page)"),
    ("/login", "logged out (login page)"),
    ("/signup", "logged out (sign-up page)"),
    ("/sales/contract-chooser", "Sales Navigator asked to choose a contract (seat or subscription problem)"),
    ("/premium/", "redirected to Premium (Sales Navigator subscription problem)"),
]

HALT_STATUS: dict[int, str] = {
    429: "rate limited (HTTP 429)",
    999: "LinkedIn refused the request (HTTP 999)",
}

# Matched case-insensitively against the page's visible text.
HALT_PHRASES: list[tuple[str, str]] = [
    ("sign in to sales navigator", "logged out of Sales Navigator"),
    ("two-step verification is now required", "LinkedIn requires two-step verification for Sales Navigator"),
    ("let's do a quick security check", "security verification prompt"),
    ("please complete this security check", "security verification prompt"),
    ("verify your identity", "identity verification prompt"),
    ("your account has been restricted", "account restricted"),
    ("your account is temporarily restricted", "account restricted"),
    ("we've restricted your account", "account restricted"),
    ("noticed some unusual activity", "unusual-activity warning"),
    ("noticed unusual activity", "unusual-activity warning"),
    ("prohibited software", "automation warning"),
    ("automate activity", "automation warning"),
    ("commercial use limit", "commercial use limit reached"),
    ("too many requests", "rate limited"),
]

CAPTCHA_FRAME = re.compile(r"<iframe[^>]+(captcha|challenge|arkose)", re.IGNORECASE)


def detect_problem(url: str, status: int | None, text: str, html: str = "") -> str | None:
    """Return a halt reason, or None if the page looks like normal browsing."""
    if status in HALT_STATUS:
        return HALT_STATUS[status]
    path = urlparse(url).path.lower()
    for fragment, reason in HALT_PATHS:
        if fragment in path:
            return reason
    lowered = _normalize(text)
    for phrase, reason in HALT_PHRASES:
        if phrase in lowered:
            return reason
    if CAPTCHA_FRAME.search(html):
        return "captcha on page"
    return None


# Only a logged-in Sales Navigator page renders its global header and search box (seen in a real capture).
LOGGED_IN_MARKER = re.compile(r'id="global-typeahead-search-input"|data-x--global-application-header')


def require_sales_navigator(url: str, html: str | None = None) -> str | None:
    """After the session check loads, we must be inside Sales Navigator, with its logged-in app rendered."""
    path = urlparse(url).path
    if not path.startswith("/sales/"):
        return f"expected Sales Navigator, landed on {path or '/'}"
    if html is not None and not LOGGED_IN_MARKER.search(html):
        return "Sales Navigator didn't render its logged-in header"
    return None


SESSION_COOKIE = "li_at"  # LinkedIn's login session cookie; absent means logged out


def has_session_cookie(cookies: list[dict]) -> bool:
    return any(cookie.get("name") == SESSION_COOKIE for cookie in cookies)


def _normalize(text: str) -> str:
    return " ".join(text.replace("’", "'").lower().split())
