import pytest

from pipeline.linkedin.checks import detect_problem, require_sales_navigator
from tests.linkedin_pages import FIXTURES, fixture_html, visible_text

HOME = "https://www.linkedin.com/sales/home"


def check(url, name, status=200):
    html = fixture_html(name)
    return detect_problem(url, status, visible_text(html), html)


def test_normal_sales_navigator_page_passes():
    # The page includes a lead's post that says "restricted", which must not trigger a halt.
    assert check(HOME, "sales_home") is None


@pytest.mark.parametrize(
    "url, fixture, reason",
    [
        ("https://www.linkedin.com/login?session_redirect=%2Fsales%2Fhome", "login", "logged out (login page)"),
        ("https://www.linkedin.com/sales/login", "login", "logged out of Sales Navigator"),
        ("https://www.linkedin.com/uas/login?trk=authwall", "login", "logged out (login page)"),
        ("https://www.linkedin.com/authwall?trk=bf", "login", "logged out (authwall)"),
        ("https://www.linkedin.com/checkpoint/challenge/AgE", "checkpoint", "LinkedIn security checkpoint"),
    ],
)
def test_redirects_off_sales_navigator_halt(url, fixture, reason):
    assert check(url, fixture) == reason


def test_checkpoint_text_halts_even_on_a_sales_url():
    assert check(HOME, "checkpoint") == "security verification prompt"


def test_sales_navigator_sign_in_text_halts_on_any_url():
    # Text from a real capture of the Sales Navigator sign-in page.
    assert detect_problem(HOME, 200, "Sign in to Sales Navigator You can use the same email address") == (
        "logged out of Sales Navigator"
    )


def test_restriction_notice_halts():
    assert check(HOME, "restricted") == "account restricted"


def test_captcha_frame_halts():
    html = fixture_html("captcha")
    assert detect_problem(HOME, 200, "Please wait", html) == "captcha on page"


@pytest.mark.parametrize("status, reason", [(429, "rate limited (HTTP 429)"), (999, "LinkedIn refused the request (HTTP 999)")])
def test_blocking_status_codes_halt(status, reason):
    assert check(HOME, "sales_home", status=status) == reason


def test_curly_apostrophes_are_normalized():
    assert detect_problem(HOME, 200, "We’ve restricted your account") == "account restricted"


def test_session_check_requires_sales_navigator():
    assert require_sales_navigator(HOME, fixture_html("sales_home")) is None
    assert require_sales_navigator("https://www.linkedin.com/feed/") == "expected Sales Navigator, landed on /feed/"


def test_session_check_rejects_the_loading_spinner():
    # Sales Navigator's HTML arrives as a spinner; only the rendered app counts as logged in.
    assert require_sales_navigator(HOME, fixture_html("sales_loading")) == (
        "Sales Navigator didn't render its logged-in header"
    )


CAPTURED_HOME = FIXTURES.parent / "captured" / "sales-home.html"


@pytest.mark.skipif(not CAPTURED_HOME.exists(), reason="run `leadgen capture https://www.linkedin.com/sales/home sales-home`")
def test_real_sales_navigator_home_passes_every_check():
    html = CAPTURED_HOME.read_text()
    assert require_sales_navigator(HOME, html) is None
    assert detect_problem(HOME, 200, visible_text(html), html) is None
