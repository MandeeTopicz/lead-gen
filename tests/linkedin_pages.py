"""Helpers for tests that use the saved LinkedIn page fixtures."""

from html.parser import HTMLParser
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "linkedin"


def fixture_html(name: str) -> str:
    return (FIXTURES / f"{name}.html").read_text()


class _TextExtractor(HTMLParser):
    """Rough stand-in for Playwright's inner_text("body"): visible text only."""

    HIDDEN = {"head", "title", "script", "style"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._hidden_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.HIDDEN:
            self._hidden_depth += 1

    def handle_endtag(self, tag):
        if tag in self.HIDDEN and self._hidden_depth:
            self._hidden_depth -= 1

    def handle_data(self, data):
        if not self._hidden_depth:
            self.parts.append(data)


def visible_text(html: str) -> str:
    extractor = _TextExtractor()
    extractor.feed(html)
    return " ".join(" ".join(extractor.parts).split())
