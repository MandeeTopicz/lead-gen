"""Claude API access. The key comes from ANTHROPIC_API_KEY, loaded from .env at the project root."""

from dataclasses import dataclass, field
from typing import Any

import anthropic

from db.models import LlmCall
from pipeline.config import Llm
from pipeline.context import now_iso

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


def check_access(settings: Llm, research_model: str, research_tools: list[dict]) -> list[str]:
    """Confirm the key works, both configured models are available, and the API accepts the research tools'
    schemas. Model lookups and token counts aren't billed."""
    client = anthropic.Anthropic()
    lines = []
    for role, model_id in (("fast", settings.fast_model), ("writer", settings.writer_model)):
        model = client.models.retrieve(model_id)
        lines.append(f"{role} model ok: {model.display_name} ({model.id})")
    client.messages.count_tokens(model=research_model, messages=[{"role": "user", "content": "schema check"}],
                                 tools=research_tools)
    lines.append("research tool schemas ok")
    return lines


@dataclass
class CallRecord:
    """What one API call used and cost, plus what it searched and read (for web research)."""

    model: str
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    web_searches: int = 0
    web_fetches: int = 0
    cost_usd: float = 0.0
    seconds: float = 0.0
    queries: list[str] = field(default_factory=list)
    fetched: list[str] = field(default_factory=list)

    @classmethod
    def from_response(cls, model: str, response: Any, seconds: float) -> "CallRecord":
        usage = response.usage
        server = getattr(usage, "server_tool_use", None)
        record = cls(
            model=model,
            input_tokens=usage.input_tokens or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            output_tokens=usage.output_tokens or 0,
            web_searches=(getattr(server, "web_search_requests", 0) or 0) if server else 0,
            web_fetches=(getattr(server, "web_fetch_requests", 0) or 0) if server else 0,
            cost_usd=usage_cost(model, usage),
            seconds=seconds,
        )
        for block in getattr(response, "content", []) or []:
            if getattr(block, "type", None) == "server_tool_use":
                arguments = block.input if isinstance(block.input, dict) else {}
                if block.name == "web_search" and arguments.get("query"):
                    record.queries.append(arguments["query"])
                elif block.name == "web_fetch" and arguments.get("url"):
                    record.fetched.append(arguments["url"])
        return record


def total_cost(records: list[CallRecord]) -> float:
    return sum(r.cost_usd for r in records)


def store_calls(session, run_id: int, lead_id: int | None, stage: str, records: list[CallRecord]) -> None:
    for r in records:
        session.add(LlmCall(
            run_id=run_id, lead_id=lead_id, stage=stage, model=r.model, input_tokens=r.input_tokens,
            cache_read_tokens=r.cache_read_tokens, cache_write_tokens=r.cache_write_tokens,
            output_tokens=r.output_tokens, web_searches=r.web_searches, web_fetches=r.web_fetches,
            cost_usd=r.cost_usd, seconds=round(r.seconds, 1),
            detail={"queries": r.queries, "fetched": r.fetched} if (r.queries or r.fetched) else None,
            created_at=now_iso(),
        ))


def account_problem(exc: Exception) -> str | None:
    """Errors that will fail every call (bad key, no credits): stop the stage instead of trying each lead."""
    if isinstance(exc, anthropic.AuthenticationError):
        return "the Anthropic API key was rejected"
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "the Anthropic API key isn't allowed to use this model"
    if isinstance(exc, anthropic.BadRequestError) and "credit balance" in str(exc).lower():
        return "the Anthropic account is out of credits (console.anthropic.com > Settings > Billing)"
    return None
