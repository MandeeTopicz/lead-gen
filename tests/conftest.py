import shutil
from pathlib import Path

import pytest

from pipeline.context import Paths

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    """An isolated project root with the real config files and an empty data/ and output/."""
    shutil.copytree(REPO_ROOT / "config", tmp_path / "config")
    return Paths(tmp_path)
