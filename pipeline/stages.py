"""The ten pipeline stages, in order. Unbuilt stages log and pass so the orchestrator can run end to end."""

from collections.abc import Callable
from dataclasses import dataclass

from pipeline.context import RunContext
from pipeline.collect import collect_dedupe
from pipeline.deep_read import deep_read
from pipeline.digest import digest_stage
from pipeline.drafting import drafting
from pipeline.linkedin.searches import searches
from pipeline.linkedin.session import session_check
from pipeline.research import research
from pipeline.scoring.match_stage import match_score
from pipeline.scoring.response import response_score


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
    Stage(SESSION_CHECK, "session_check", session_check, uses_linkedin=True),
    Stage(3, "searches", searches, uses_linkedin=True),
    Stage(4, "collect_dedupe", collect_dedupe),
    Stage(5, "match_score", match_score),
    Stage(6, "deep_read", deep_read, uses_linkedin=True),
    Stage(7, "research", research),
    Stage(8, "response_score", response_score),
    Stage(9, "drafting", drafting),
    Stage(10, "digest", digest_stage),
]
