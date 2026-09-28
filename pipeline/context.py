"""Shared state handed to every pipeline stage."""

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlmodel import Session

from db.models import Run
from pipeline.config import Caps, Config


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass(frozen=True)
class Paths:
    root: Path

    @classmethod
    def default(cls) -> "Paths":
        return cls(Path(os.environ.get("LEADGEN_ROOT", Path(__file__).resolve().parents[1])))

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "leadgen.db"

    @property
    def browser_profile_dir(self) -> Path:
        return self.data_dir / "browser-profile"

    @property
    def captured_fixtures_dir(self) -> Path:
        return self.root / "tests" / "fixtures" / "captured"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / "run.lock"

    @property
    def output_dir(self) -> Path:
        return self.root / "output"

    def run_output_dir(self, run: Run) -> Path:
        return self.output_dir / run.started_at[:10]


class Halt(Exception):
    """Stop the run now and flag it for a human (login page, verification prompt, LinkedIn warning)."""

    def __init__(self, reason: str, screenshot_path: Path | None = None):
        super().__init__(reason)
        self.reason = reason
        self.screenshot_path = screenshot_path


class CapReached(Exception):
    """A per-run cap is used up. Stages catch this and stop collecting; it is not a halt."""

    def __init__(self, cap: str):
        super().__init__(f"per-run cap reached: {cap}")
        self.cap = cap


class Budget:
    """Per-run caps and cost tracking. Counters live on the run row so they survive a resume."""

    def __init__(self, run: Run, caps: Caps):
        self.run = run
        self.caps = caps

    def use_page(self) -> None:
        if self.run.pages_viewed >= self.caps.result_pages:
            raise CapReached("result_pages")
        self.run.pages_viewed += 1

    def use_deep_read(self) -> None:
        if self.run.profiles_read >= self.caps.deep_reads:
            raise CapReached("deep_reads")
        self.run.profiles_read += 1

    def add_llm_cost(self, usd: float) -> None:
        self.run.llm_cost_usd += usd

    def add_enrichment_cost(self, usd: float) -> None:
        self.run.enrichment_cost_usd += usd


@dataclass
class RunContext:
    config: Config
    paths: Paths
    session: Session
    run: Run
    budget: Budget
    log: logging.Logger
    # In-memory handoffs between stages within one process, e.g. the browser session.
    state: dict[str, Any] = field(default_factory=dict)
    closers: list[Callable[[], None]] = field(default_factory=list)

    def on_close(self, closer: Callable[[], None]) -> None:
        """Register cleanup (e.g. closing the browser) to run when the run ends, however it ends."""
        self.closers.append(closer)

    @property
    def dry_run(self) -> bool:
        return self.run.dry_run

    @property
    def output_dir(self) -> Path:
        return self.paths.run_output_dir(self.run)
