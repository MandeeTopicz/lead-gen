"""Every LinkedIn action goes through the Pacer: spend from the budget, wait at human pace, act, then check."""

import logging
import random
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Literal, TypeVar

from playwright.sync_api import Page

from pipeline.context import Budget, Halt
from pipeline.linkedin.checks import detect_problem, has_session_cookie, require_sales_navigator

Cost = Literal["page", "deep_read"] | None
T = TypeVar("T")


class Pacer:
    def __init__(
        self,
        page: Page,
        budget: Budget | None,
        delay_seconds: tuple[float, float],
        halt_dir: Path,
        log: logging.Logger,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
        settle_seconds: float = 3,
    ):
        self.page = page
        self.budget = budget
        self.delay_seconds = delay_seconds
        self.halt_dir = halt_dir
        self.log = log
        self._sleep = sleep
        self._rng = rng or random.Random()
        self.settle_seconds = settle_seconds

    def goto(self, url: str, cost: Cost = None) -> None:
        self._spend(cost)
        self.pause()
        response = self.page.goto(url, wait_until="domcontentloaded")
        self._settle()
        self.check(response.status if response else None)

    def act(self, action: Callable[[], T], cost: Cost = None) -> T:
        """Run a page interaction (a click, a scroll to the next page) at human pace, then check the result."""
        self._spend(cost)
        self.pause()
        result = action()
        self._settle()
        self.check()
        return result

    def pause(self) -> None:
        self._sleep(self._rng.uniform(*self.delay_seconds))

    def check(self, status: int | None = None) -> None:
        problem = detect_problem(self.page.url, status, self._visible_text(), self.page.content())
        if problem:
            self.halt(problem)

    def require_sales_navigator(self) -> None:
        problem = require_sales_navigator(self.page.url, self.page.content())
        if not problem and not has_session_cookie(self.page.context.cookies(self.page.url)):
            problem = "no LinkedIn session cookie; run `leadgen login`"
        if problem:
            self.halt(problem)

    def halt(self, reason: str) -> None:
        raise Halt(reason, self._capture())

    def _settle(self) -> None:
        """Sales Navigator is a single-page app: the HTML arrives as a loading spinner, then scripts render
        the real page or redirect to sign-in. Check what a person would see, not the spinner."""
        try:
            self.page.wait_for_load_state("load", timeout=15_000)
        except Exception:
            pass  # slow scripts; the checks below still run on whatever rendered
        self.page.wait_for_timeout(self.settle_seconds * 1000)

    def _spend(self, cost: Cost) -> None:
        # Spend before acting, so a cap stops us before the page is viewed, not after.
        if self.budget is None or cost is None:
            return
        if cost == "page":
            self.budget.use_page()
        else:
            self.budget.use_deep_read()

    def _visible_text(self) -> str:
        try:
            return self.page.inner_text("body", timeout=5_000)
        except Exception:
            return ""

    def _capture(self) -> Path | None:
        """Save a screenshot and the page HTML. The HTML becomes a test fixture for the halt checks."""
        stem = self.halt_dir / f"halt-{datetime.now():%H%M%S}"
        try:
            self.halt_dir.mkdir(parents=True, exist_ok=True)
            stem.with_suffix(".html").write_text(self.page.content())
            self.page.screenshot(path=stem.with_suffix(".png"), full_page=True)
            return stem.with_suffix(".png")
        except Exception:
            self.log.exception("could not capture the halt page")
            return None
