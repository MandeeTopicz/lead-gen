"""Stage 5: score every lead collected this run against the ICP and store the breakdown."""

from db.models import Score
from pipeline.collect import facts_for_run
from pipeline.context import RunContext
from pipeline.scoring.match import score_match


def match_score(ctx: RunContext) -> None:
    passed = 0
    for lead_id, facts in facts_for_run(ctx.session, ctx.run.id).items():
        result = score_match(facts, ctx.config.icp)
        score = ctx.session.get(Score, (ctx.run.id, lead_id)) or Score(run_id=ctx.run.id, lead_id=lead_id, gates_passed=False)
        score.gates_passed = result.gates_passed
        score.match_score = result.score
        score.match_breakdown = result.breakdown()
        ctx.session.add(score)
        passed += result.gates_passed
    ctx.session.commit()
    ctx.log.info("%d of %d leads passed the gates and exclusions", passed, ctx.run.leads_found)
