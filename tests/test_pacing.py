import logging
import random
from dataclasses import dataclass
from pathlib import Path

import pytest

from db.models import Run
from pipeline.config import load_config
from pipeline.context import Budget, CapReached, Halt
from pipeline.linkedin.pacing import Pacer
from tests.linkedin_pages import fixture_html, visible_text

HOME = "https://www.linkedin.com/sales/home"
SEARCH = "https://www.linkedin.com/sales/search/people?savedSearchId=1"
LOGIN = "https://www.linkedin.com/login"


@dataclass
class FakeResponse:
    status: int


class FakeContext:
    def __init__(self, cookies):
        self._cookies = cookies

    def cookies(self, url=None):
        return self._cookies


class FakePage:
    """Serves fixtures by URL. `routes` maps a requested URL to (final URL after redirects, status, fixture)."""

    def __init__(self, routes: dict[str, tuple[str, int, str]], logged_in: bool = True):
        self.routes = routes
        self.context = FakeContext([{"name": "li_at", "value": "x"}] if logged_in else [])
        self.url = "about:blank"
        self.html = ""
        self.visited: list[str] = []

    def goto(self, url, wait_until=None):
        final_url, status, fixture = self.routes[url]
        self.visited.append(url)
        self.url, self.html = final_url, fixture_html(fixture)
        return FakeResponse(status)

    def content(self):
        return self.html

    def inner_text(self, selector, timeout=None):
        return visible_text(self.html)

    def wait_for_load_state(self, state=None, timeout=None):
        pass

    def wait_for_timeout(self, ms):
        pass

    def screenshot(self, path, full_page=False):
        Path(path).write_bytes(b"png")


@pytest.fixture
def config(paths):
    return load_config(paths.config_dir)


@pytest.fixture
def budget(config):
    run = Run(started_at="2026-09-28T09:00:00-05:00", trigger="manual", status="running", icp_version="t@v1")
    return Budget(run, config.icp.caps)


def make_pacer(page, budget, tmp_path, sleeps):
    return Pacer(page, budget, (4, 12), tmp_path / "halts", logging.getLogger("test"), sleep=sleeps.append,
                 rng=random.Random(7))


def test_every_action_waits_a_random_human_delay(budget, tmp_path):
    sleeps = []
    page = FakePage({HOME: (HOME, 200, "sales_home"), SEARCH: (SEARCH, 200, "sales_home")})
    pacer = make_pacer(page, budget, tmp_path, sleeps)
    pacer.goto(HOME)
    pacer.goto(SEARCH)
    pacer.act(lambda: None)
    assert len(sleeps) == 3
    assert all(4 <= s <= 12 for s in sleeps)
    assert len(set(sleeps)) == 3


def test_redirect_to_login_halts_and_saves_screenshot_and_html(budget, tmp_path):
    page = FakePage({HOME: (LOGIN, 200, "login")})
    pacer = make_pacer(page, budget, tmp_path, [])
    with pytest.raises(Halt) as halted:
        pacer.goto(HOME)
    assert halted.value.reason == "logged out (login page)"
    screenshot = halted.value.screenshot_path
    assert screenshot.exists()
    assert "Sign in" in screenshot.with_suffix(".html").read_text()


def test_blocking_status_halts(budget, tmp_path):
    page = FakePage({HOME: (HOME, 999, "sales_home")})
    with pytest.raises(Halt, match="HTTP 999"):
        make_pacer(page, budget, tmp_path, []).goto(HOME)


def test_page_cost_is_spent_and_cap_stops_before_navigating(config, budget, tmp_path):
    sleeps = []
    page = FakePage({SEARCH: (SEARCH, 200, "sales_home")})
    pacer = make_pacer(page, budget, tmp_path, sleeps)
    for _ in range(config.icp.caps.result_pages):
        pacer.goto(SEARCH, cost="page")
    assert budget.run.pages_viewed == 30
    with pytest.raises(CapReached, match="result_pages"):
        pacer.goto(SEARCH, cost="page")
    assert len(page.visited) == 30
    assert len(sleeps) == 30


def test_deep_read_cost_uses_deep_read_cap(budget, tmp_path):
    page = FakePage({HOME: (HOME, 200, "sales_home")})
    make_pacer(page, budget, tmp_path, []).goto(HOME, cost="deep_read")
    assert (budget.run.pages_viewed, budget.run.profiles_read) == (0, 1)


def test_act_checks_the_page_after_the_action(budget, tmp_path):
    page = FakePage({HOME: (HOME, 200, "sales_home")})
    pacer = make_pacer(page, budget, tmp_path, [])
    pacer.goto(HOME)

    def click_next_page():
        page.url, page.html = HOME, fixture_html("restricted")

    with pytest.raises(Halt, match="account restricted"):
        pacer.act(click_next_page, cost="page")


def test_require_sales_navigator_halts_off_sales(budget, tmp_path):
    feed = "https://www.linkedin.com/feed/"
    page = FakePage({HOME: (feed, 200, "sales_home")})
    pacer = make_pacer(page, budget, tmp_path, [])
    pacer.goto(HOME)
    with pytest.raises(Halt, match="landed on /feed/"):
        pacer.require_sales_navigator()


def test_require_sales_navigator_halts_without_session_cookie(budget, tmp_path):
    page = FakePage({HOME: (HOME, 200, "sales_home")}, logged_in=False)
    pacer = make_pacer(page, budget, tmp_path, [])
    pacer.goto(HOME)
    with pytest.raises(Halt, match="no LinkedIn session cookie"):
        pacer.require_sales_navigator()
