"""Text cleanup shared by everything that writes output."""

import re

# Emoji and pictographs, dingbats, flags, variation selectors, and zero-width joiners. Arrows and ordinary
# punctuation (–, —, ·, ≤) are kept.
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U0001F1E6-\U0001F1FF☀-➿⬀-⯿⌀-⏿︎️‍⃣]+"
)


def strip_emoji(text: str) -> str:
    """Remove emoji, then tidy the spaces they leave behind (without touching line breaks)."""
    if not text:
        return text
    cleaned = _EMOJI.sub("", text)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"[ \t]+([.,!?;:])", r"\1", cleaned)
    return "\n".join(line.rstrip() for line in cleaned.split("\n")).strip()
