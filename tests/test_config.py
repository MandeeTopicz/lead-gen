import pytest
import yaml
from pydantic import ValidationError

from pipeline.config import load_config


def edit_icp(paths, change):
    icp_path = paths.config_dir / "icp.yaml"
    data = yaml.safe_load(icp_path.read_text())
    change(data)
    icp_path.write_text(yaml.safe_dump(data))


def test_shipped_config_is_valid(paths):
    config = load_config(paths.config_dir)
    assert config.icp.version_tag == "austin-logistics-ops-leaders@v1"
    assert list(config.icp.searches) == ["S1", "S2", "S3", "S4", "S5", "S6", "S7"]
    assert config.icp.caps.delay_seconds == (4, 12)


def test_match_weights_must_sum_to_100(paths):
    edit_icp(paths, lambda d: d["weights"]["match"].update(role=25))
    with pytest.raises(ValidationError, match="weights.match must sum to 100"):
        load_config(paths.config_dir)


def test_priority_weights_must_sum_to_1(paths):
    edit_icp(paths, lambda d: d["weights"]["priority"].update(match=0.7))
    with pytest.raises(ValidationError, match="weights.priority must sum to 1"):
        load_config(paths.config_dir)


def test_partial_credit_cannot_exceed_weight(paths):
    edit_icp(paths, lambda d: d["match_points"].update(role_related=21))
    with pytest.raises(ValidationError, match="role_related"):
        load_config(paths.config_dir)


def test_unknown_keys_are_rejected(paths):
    edit_icp(paths, lambda d: d["caps"].update(deep_read=40))
    with pytest.raises(ValidationError, match="deep_read"):
        load_config(paths.config_dir)


def test_delay_range_must_be_ordered(paths):
    edit_icp(paths, lambda d: d["caps"].update(delay_seconds=[12, 4]))
    with pytest.raises(ValidationError, match="delay_seconds"):
        load_config(paths.config_dir)


def test_search_codes_must_look_like_s_numbers(paths):
    edit_icp(paths, lambda d: d["searches"].update(strict={"name": "S1-strict"}))
    with pytest.raises(ValidationError, match="search code"):
        load_config(paths.config_dir)


def test_search_guarantees_relax_the_baseline(paths):
    icp = load_config(paths.config_dir).icp
    assert icp.guarantees("S1")["industry"] == "full"
    assert "activity" not in icp.guarantees("S1")  # S1 leaves activity, tenure, keywords unfiltered
    assert icp.guarantees("S3")["activity"] == "full"
    assert "activity" not in icp.guarantees("S2")
    assert icp.guarantees("S4")["size"] == "partial"
    assert icp.guarantees("S5")["geography"] == "partial"
    assert "keywords" not in icp.guarantees("S6")


def test_unknown_criterion_in_a_search_is_rejected(paths):
    edit_icp(paths, lambda d: d["searches"]["S2"].update(drops=["activty"]))
    with pytest.raises(ValidationError, match="activty"):
        load_config(paths.config_dir)


def test_empty_sender_lists_what_drafting_needs(paths):
    (paths.config_dir / "sender.yaml").write_text("sender: {}\n")
    config = load_config(paths.config_dir)
    assert config.sender.missing_for_drafting() == [
        "sender.name",
        "sender.company",
        "sender.physical_address",
        "offer",
    ]
