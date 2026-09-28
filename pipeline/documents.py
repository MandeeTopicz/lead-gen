"""A tiny document model rendered three ways (Markdown, DOCX, PDF), so all three formats say the same thing.

Inline text may use **bold** and [text](url); DOCX shows links as "text (url)".
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

import docx
from docx.shared import Pt
from markdown_pdf import MarkdownPdf, Section

from pipeline.text import strip_emoji


@dataclass
class Heading:
    level: int
    text: str


@dataclass
class Para:
    text: str


@dataclass
class Bullets:
    items: list[str]


@dataclass
class Table:
    headers: list[str]
    rows: list[list[str]]


@dataclass
class Quote:
    """A drafted message: shown as a block quote so it's easy to copy."""

    lines: list[str] = field(default_factory=list)


Block = Heading | Para | Bullets | Table | Quote

PDF_CSS = """
body { font-family: Helvetica, Arial, sans-serif; font-size: 10pt; line-height: 1.35; }
h1 { font-size: 18pt; margin-bottom: 2pt; } h2 { font-size: 13pt; margin-top: 14pt; } h3 { font-size: 11pt; }
table { border-collapse: collapse; width: 100%; margin: 4pt 0; }
th, td { border: 0.5pt solid #bbb; padding: 3pt 5pt; text-align: left; vertical-align: top; font-size: 9pt; }
th { font-weight: bold; }
blockquote { border-left: 2pt solid #999; margin: 4pt 0; padding-left: 8pt; color: #222; }
"""


def clean(blocks: list[Block]) -> list[Block]:
    """No emoji anywhere in generated documents."""
    cleaned: list[Block] = []
    for block in blocks:
        match block:
            case Heading(level, text):
                cleaned.append(Heading(level, strip_emoji(text)))
            case Para(text):
                cleaned.append(Para(strip_emoji(text)))
            case Bullets(items):
                cleaned.append(Bullets([strip_emoji(i) for i in items]))
            case Table(headers, rows):
                cleaned.append(Table(headers, [[strip_emoji(c or "") for c in row] for row in rows]))
            case Quote(lines):
                cleaned.append(Quote([strip_emoji(line) for line in lines]))
    return cleaned


def to_markdown(blocks: list[Block]) -> str:
    out: list[str] = []
    for block in clean(blocks):
        match block:
            case Heading(level, text):
                out += [f"{'#' * level} {text}", ""]
            case Para(text):
                out += [text, ""]
            case Bullets(items):
                out += [f"- {item}" for item in items] + [""]
            case Table(headers, rows):
                out.append("| " + " | ".join(headers) + " |")
                out.append("| " + " | ".join("---" for _ in headers) + " |")
                out += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]
                out.append("")
            case Quote(lines):
                out += [f"> {line}" if line else ">" for line in lines] + [""]
    return "\n".join(out).rstrip() + "\n"


def write_docx(blocks: list[Block], path: Path) -> None:
    document = docx.Document()
    document.styles["Normal"].font.size = Pt(10)
    for block in clean(blocks):
        match block:
            case Heading(level, text):
                document.add_heading(_plain(text), level=min(level, 4))
            case Para(text):
                _inline(document.add_paragraph(), text)
            case Bullets(items):
                for item in items:
                    _inline(document.add_paragraph(style="List Bullet"), item)
            case Table(headers, rows):
                table = document.add_table(rows=1, cols=len(headers))
                table.style = "Light Grid Accent 1"
                for cell, header in zip(table.rows[0].cells, headers):
                    cell.text = _plain(header)
                for row in rows:
                    for cell, value in zip(table.add_row().cells, row):
                        cell.text = _plain(value)
            case Quote(lines):
                paragraph = document.add_paragraph(style="Intense Quote")
                paragraph.add_run("\n".join(lines))
    document.save(path)


def write_pdf(markdown: str, path: Path, title: str) -> None:
    pdf = MarkdownPdf(toc_level=0)
    pdf.meta["title"] = strip_emoji(title)
    pdf.add_section(Section(strip_emoji(markdown), toc=False), user_css=PDF_CSS)
    pdf.save(str(path))


_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")


def _plain(text: str) -> str:
    return _BOLD.sub(r"\1", _LINK.sub(r"\1 (\2)", text))


def _inline(paragraph, text: str) -> None:
    """Bold runs and 'text (url)' links in a DOCX paragraph."""
    text = _LINK.sub(r"\1 (\2)", text)
    for i, part in enumerate(_BOLD.split(text)):
        if part:
            paragraph.add_run(part).bold = i % 2 == 1


def _cell(value: str) -> str:
    return (value or "-").replace("|", "\\|").replace("\n", " ")
