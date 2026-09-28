"""Claude API access. The key comes from ANTHROPIC_API_KEY, loaded from .env at the project root."""

import anthropic

from pipeline.config import Llm


def check_access(settings: Llm) -> list[str]:
    """Confirm the key works and both configured models are available. Model lookups aren't billed."""
    client = anthropic.Anthropic()
    lines = []
    for role, model_id in (("fast", settings.fast_model), ("writer", settings.writer_model)):
        model = client.models.retrieve(model_id)
        lines.append(f"{role} model ok: {model.display_name} ({model.id})")
    return lines
