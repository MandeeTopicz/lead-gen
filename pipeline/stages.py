"""The ten pipeline stages, in order. Unbuilt stages log and pass so the orchestrator can run end to end."""

from collections.abc import Callable
from dataclasses import dataclass

from pipeline.context import RunContext


@dataclass(frozen=True)
class Stage:
    number: int
    name: str
    run: Callable[[RunContext], None]
    uses_linkedin: bool = False


def trigger(ctx: RunContext) -> None:
    # The lock, daily cap, and run row are handled by the orchestrator before any stage runs.
    ctx.log.info("trigger=%s dry_run=%s icp=%s", ctx.run.trigger, ctx.dry_run, ctx.run.icp_version)


def not_built(ctx: RunContext) -> None:
    ctx.log.info("not built yet, skipping")


SESSION_CHECK = 2

STAGES: list[Stage] = [
    Stage(1, "trigger", trigger),
    Stage(SESSION_CHECK, "session_check", not_built, uses_linkedin=True),
    Stage(3, "searches", not_built, uses_linkedin=True),
    Stage(4, "collect_dedupe", not_built),
    Stage(5, "match_score", not_built),
    Stage(6, "deep_read", not_built, uses_linkedin=True),
    Stage(7, "research", not_built),
    Stage(8, "response_score", not_built),
    Stage(9, "drafting", not_built),
    Stage(10, "dossiers", not_built),
]
