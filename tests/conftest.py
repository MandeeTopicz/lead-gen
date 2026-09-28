import shutil
from pathlib import Path

import pytest

from pipeline.context import Paths

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _no_real_api_calls(monkeypatch):
    """Tests never reach the paid Anthropic API, even if a key is exported in the shell."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    """An isolated project root with the real config files and an empty data/ and output/."""
    shutil.copytree(REPO_ROOT / "config", tmp_path / "config")
    return Paths(tmp_path)


@pytest.fixture
def full_sequence(paths: Paths) -> Paths:
    """Tests of the multi-step outreach sequence: 10 LinkedIn touches, 2 emails, 2 calls, whatever the shipped
    config drafts."""
    import yaml

    icp_path = paths.config_dir / "icp.yaml"
    data = yaml.safe_load(icp_path.read_text())
    data["sequence"]["linkedin"]["touches"] = 10
    data["sequence"]["email"]["touches"] = 2
    data["sequence"]["calls"]["touches"] = 2
    icp_path.write_text(yaml.safe_dump(data))
    return paths


@pytest.fixture
def all_searches_enabled(paths: Paths) -> Paths:
    """Tests that exercise all seven saved searches, whichever ones the shipped config has turned on."""
    import yaml

    icp_path = paths.config_dir / "icp.yaml"
    data = yaml.safe_load(icp_path.read_text())
    for search in data["searches"].values():
        search["enabled"] = True
    icp_path.write_text(yaml.safe_dump(data))
    return paths
