"""Claude API access. The key comes from ANTHROPIC_API_KEY, loaded from .env at the project root."""

from typing import Any

import anthropic

from pipeline.config import Llm

# USD per million tokens (input, output). Cache writes bill at 1.25x input, cache reads at 0.1x.
PRICES = {
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-opus-5": (5.0, 25.0),
}
WEB_SEARCH_USD = 10 / 1000


def usage_cost(model: str, usage: Any) -> float:
    """Dollar cost of one response from its usage block, including web searches."""
    price_in, price_out = PRICES.get(model, PRICES["claude-sonnet-5"])
    tokens_in = (
        (usage.input_tokens or 0)
        + 1.25 * (getattr(usage, "cache_creation_input_tokens", 0) or 0)
        + 0.1 * (getattr(usage, "cache_read_input_tokens", 0) or 0)
    )
    server = getattr(usage, "server_tool_use", None)
    searches = (getattr(server, "web_search_requests", 0) or 0) if server else 0
    return (tokens_in * price_in + (usage.output_tokens or 0) * price_out) / 1_000_000 + searches * WEB_SEARCH_USD


def check_access(settings: Llm) -> list[str]:
    """Confirm the key works and both configured models are available. Model lookups aren't billed."""
    client = anthropic.Anthropic()
    lines = []
    for role, model_id in (("fast", settings.fast_model), ("writer", settings.writer_model)):
        model = client.models.retrieve(model_id)
        lines.append(f"{role} model ok: {model.display_name} ({model.id})")
    return lines
