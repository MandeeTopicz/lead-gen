"""Stage 3: open each saved search by name and page through its results at human pace.

The page mechanics (finding a saved search, loading every card, the next-page control) sit behind the
`SavedSearchPages` interface, so the runner is tested with a fake; `SalesNavigatorSearches` is the real one,
built against captured Sales Navigator pages. Read-only: it never edits or saves a search.
"""

import random
import re
from typing import Protocol

from bs4 import BeautifulSoup

from pipeline.collect import record_card
from pipeline.context import CapReached, RunContext
from pipeline.linkedin.cards import Card
from pipeline.linkedin.pacing import Pacer
from pipeline.linkedin.results_page import (
    RESULT,
    ParseError,
    has_next_page,
    is_empty_search,
    parse_results,
)


class SavedSearchPages(Protocol):
    def open(self, name: str) -> None:
        """Open a saved search's first results page. Halts if no saved search has this exact name."""

    def cards(self) -> list[Card]:
        """Parse every result card on the current page. Halts if the page has results it can't parse."""

    def next_page(self) -> bool:
        """Go to the next results page. False when this was the last page."""


class SalesNavigatorSearches:
    """The real SavedSearchPages: Sales Navigator's saved-searches panel, lazy result lists, and pagination."""

    PANEL_LINK = "[data-x--link--saved-searches]"
    RESULTS = "[data-x--search-results-container]"

    def __init__(self, pacer: Pacer, rng: random.Random | None = None):
        self.pacer = pacer
        self.page = pacer.page
        self._rng = rng or random.Random()
        self._saved: dict[str, str] | None = None

    def open(self, name: str) -> None:
        saved = self._saved_searches()
        if name not in saved:
            known = ", ".join(sorted(saved)) or "none"
            self.pacer.halt(f"no saved search named {name!r} (found: {known})")
        self.pacer.goto(saved[name], cost="page")

    def cards(self) -> list[Card]:
        html = self._render_all_results()
        if is_empty_search(html):
            return []
        try:
            return parse_results(html)
        except ParseError as exc:
            self.pacer.halt(f"could not parse search results: {exc}")

    def next_page(self) -> bool:
        if not has_next_page(self.page.content()):
            return False
        before = self.page.url
        self.pacer.act(lambda: self.page.locator('button[aria-label="Next"]').click(), cost="page")
        if self.page.url == before:
            self.pacer.halt("clicked Next but the results page didn't change")
        return True

    def _saved_searches(self) -> dict[str, str]:
        """Saved lead searches by exact name, read once per run from the panel."""
        if self._saved is None:
            link = self.page.locator(self.PANEL_LINK).first
            if link.count() == 0:
                self.pacer.halt("Sales Navigator's Saved searches link wasn't found (page layout changed?)")
            self.pacer.act(link.click)
            try:
                self.page.wait_for_selector('a[href*="savedSearchId="]', timeout=15_000)
            except Exception:
                self.pacer.halt("the Saved searches panel opened without any saved lead searches")
            self._saved = saved_searches_in_panel(self.page.content())
            self.page.keyboard.press("Escape")
        return self._saved

    def _render_all_results(self) -> str:
        """Results render as they scroll into view. Scroll in reader-sized steps until the count stops growing."""
        try:
            self.page.wait_for_selector(f"{self.RESULTS}, :text('No leads matched')", timeout=15_000)
        except Exception:
            self.pacer.halt("search results never loaded")
        box = self.page.locator(self.RESULTS).first.bounding_box()
        if box:
            self.page.mouse.move(box["x"] + box["width"] / 2, box["y"] + min(box["height"] / 2, 400))
        seen, stable = -1, 0
        for _ in range(40):
            count = self.page.locator(RESULT).count()
            stable = stable + 1 if count == seen else 0
            if stable >= 3:
                break
            seen = count
            self.page.mouse.wheel(0, self._rng.randint(450, 750))
            self.page.wait_for_timeout(self._rng.randint(500, 1100))
        return self.page.content()


def saved_searches_in_panel(html: str) -> dict[str, str]:
    """Map each saved lead search's name to its link, from the saved-searches panel HTML."""
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, str] = {}
    for link in soup.select('a[href*="/sales/search/people?"][href*="savedSearchId="]'):
        name = " ".join(link.get_text(" ").split())
        # Each entry also has "View", "N new results…", and "+N more filters" links to the same search.
        if not name or name == "View" or "new result" in name or re.fullmatch(r"\+\d+ more filters?", name):
            continue
        search_id = re.search(r"savedSearchId=(\d+)", link["href"]).group(1)
        found.setdefault(name, f"https://www.linkedin.com/sales/search/people?savedSearchId={search_id}")
    return found


def searches(ctx: RunContext) -> None:
    """Stage 3."""
    if ctx.dry_run:
        ctx.log.info("dry run: not opening LinkedIn")
        return
    run_searches(ctx, SalesNavigatorSearches(ctx.state["pacer"]))


def run_searches(ctx: RunContext, pages: SavedSearchPages) -> None:
    per_search = ctx.config.icp.caps.pages_per_search
    for code, search in ctx.config.icp.searches.items():
        viewed = found = 0
        try:
            pages.open(search.name)
            while True:
                cards = pages.cards()
                viewed += 1
                for card in cards:
                    record_card(ctx.session, ctx.run, code, card)
                found += len(cards)
                ctx.session.add(ctx.run)  # page counters
                ctx.session.commit()  # keep every page read, even if a later page halts the run
                if viewed >= per_search or not cards or not pages.next_page():
                    break
        except CapReached as cap:
            ctx.log.info("%s %s: %d pages, %d cards; stopping all searches (%s)", code, search.name, viewed, found, cap)
            ctx.session.commit()
            return
        ctx.log.info("%s %s: %d pages, %d cards", code, search.name, viewed, found)
