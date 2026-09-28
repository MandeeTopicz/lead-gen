"""leadgen command line. Exit codes: 0 completed, 1 failed or refused, 2 halted (needs you)."""

import argparse
import sys

from pydantic import ValidationError
from sqlmodel import Session, col, select

from db import make_engine
from db.models import Run, RunStage
from pipeline.config import load_config
from pipeline.context import Paths
from pipeline.run import RunRefused, resume_run, start_run

EXIT_CODES = {"completed": 0, "failed": 1, "halted": 2}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="leadgen")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="start a new run")
    run.add_argument("--trigger", choices=["manual", "cron"], default="manual")
    run.add_argument("--dry-run", action="store_true", help="no LinkedIn or paid APIs; not counted in daily caps")

    resume = commands.add_parser("resume", help="continue a halted or failed run")
    resume.add_argument("run_id", nargs="?", type=int, help="defaults to the latest halted or failed run")
    resume.add_argument("--from-stage", type=int, choices=range(1, 11), metavar="N")

    runs = commands.add_parser("runs", help="list recent runs")
    runs.add_argument("--limit", type=int, default=10)

    commands.add_parser("check-config", help="validate config/icp.yaml and config/sender.yaml")

    args = parser.parse_args(argv)
    paths = Paths.default()
    try:
        match args.command:
            case "run":
                result = start_run(paths, trigger=args.trigger, dry_run=args.dry_run)
                return _report(result)
            case "resume":
                result = resume_run(paths, run_id=args.run_id, from_stage=args.from_stage)
                return _report(result)
            case "runs":
                return _list_runs(paths, args.limit)
            case "check-config":
                return _check_config(paths)
    except RunRefused as refused:
        print(f"refused: {refused}", file=sys.stderr)
        return 1
    except ValidationError as invalid:
        print(f"invalid config:\n{invalid}", file=sys.stderr)
        return 1
    except Exception as exc:  # details are in the run log
        print(f"run failed: {exc!r}", file=sys.stderr)
        return 1
    return 0


def _report(run: Run) -> int:
    print(f"run {run.id}: {run.status}" + (f" ({run.halt_reason})" if run.halt_reason else ""))
    if run.halt_screenshot_path:
        print(f"screenshot: {run.halt_screenshot_path}")
    return EXIT_CODES[run.status]


def _list_runs(paths: Paths, limit: int) -> int:
    with Session(make_engine(paths.db_path)) as session:
        runs = session.exec(select(Run).order_by(col(Run.id).desc()).limit(limit)).all()
        if not runs:
            print("no runs yet")
        for run in runs:
            stages = session.exec(
                select(RunStage).where(RunStage.run_id == run.id).order_by(col(RunStage.stage))
            ).all()
            last = stages[-1] if stages else None
            print(
                f"{run.id:>4}  {run.started_at}  {run.trigger:<6}  {run.status:<9}"
                f"{' dry' if run.dry_run else '    '}  pages={run.pages_viewed:<3} reads={run.profiles_read:<3}"
                f"  last stage={last.stage if last else '-'} {last.name if last else ''}"
                + (f"  [{run.halt_reason}]" if run.halt_reason else "")
            )
    return 0


def _check_config(paths: Paths) -> int:
    config = load_config(paths.config_dir)
    print(f"icp.yaml ok: {config.icp.version_tag}, {len(config.icp.searches)} searches")
    missing = config.sender.missing_for_drafting()
    if missing:
        print(f"sender.yaml ok, but drafting needs: {', '.join(missing)}")
    else:
        print("sender.yaml ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
