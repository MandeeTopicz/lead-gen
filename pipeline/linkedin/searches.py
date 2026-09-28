"""Stage 3: open each saved search by name and page through its results at human pace.

The page mechanics (finding a saved search, loading every card, the next-page control) sit behind the
`SavedSearchPages` interface; the Sales Navigator implementation is built against captured pages.
Everything here is tested with a fake.
"""

from typing import Protocol

from pipeline.collect import record_card
from pipeline.context import CapReached, RunContext
from pipeline.linkedin.cards import Card


class SavedSearchPages(Protocol):
    def open(self, name: str) -> None:
        """Open a saved search's first results page. Halts if no saved search has this exact name."""

    def cards(self) -> list[Card]:
        """Parse every result card on the current page. Halts if the page has results it can't parse."""

    def next_page(self) -> bool:
        """Go to the next results page. False when this was the last page."""


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
