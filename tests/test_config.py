from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from pydantic import ValidationError

from digest.config import load_config
from digest.errors import ConfigError

PROFILE = """
recipient_email: "someone@example.com"
home:
  district: "XI."
  lat: 47.47
  lon: 19.05
scoring:
  category_weights:
    tarsasjatek: 5
  keyword_boosts: { koreai: 3 }
  free_bonus: 2
  proximity:
    same_district_bonus: 2
    penalty_cap_km: 8
    distance_penalty_per_km: 0.3
  weekday_weights: { fri: 2, sat: 2 }
filters:
  categories: [koncert, kviz]
  max_price_huf: 12000
  min_score: 3
"""


def test_profile_overrides_a_scalar(config_path: Path, sources_dir: Path) -> None:
    assert load_config(config_path, sources_dir, None).min_category_score == 2
    config = load_config(config_path, sources_dir, "min_category_score: 5")
    assert config.min_category_score == 5


def test_profile_merges_a_nested_dict(config_path: Path, sources_dir: Path) -> None:
    config = load_config(config_path, sources_dir, "newsletter:\n  total_limit: 10")
    assert config.newsletter.total_limit == 10
    # Untouched siblings survive the merge instead of being replaced with defaults.
    assert config.newsletter.per_category_limit == 10
    assert config.newsletter.expiring_section.within_days == 3


def test_profile_replaces_a_list_wholesale(config_path: Path, sources_dir: Path) -> None:
    config = load_config(config_path, sources_dir, "grouping:\n  collapse_by: [venue_name]")
    assert config.grouping.collapse_by == ["venue_name"]
    assert config.grouping.min_group_size == 4


def test_profile_sections_are_applied(config_path: Path, sources_dir: Path) -> None:
    config = load_config(config_path, sources_dir, PROFILE)
    assert config.recipient_email == "someone@example.com"
    assert config.home is not None
    assert config.home.district == "XI."
    assert config.scoring.category_weights["tarsasjatek"] == 5
    assert config.scoring.keyword_boosts == {"koreai": 3}
    assert config.scoring.proximity is not None
    assert config.scoring.proximity.penalty_cap_km == 8
    assert config.filters.categories == ["koncert", "kviz"]
    assert config.filters.max_price_huf == 12000


def test_unknown_top_level_key_raises(config_path: Path, sources_dir: Path) -> None:
    with pytest.raises(ValidationError):
        load_config(config_path, sources_dir, "nonsense: true")


def test_unknown_nested_key_raises(config_path: Path, sources_dir: Path) -> None:
    with pytest.raises(ValidationError):
        load_config(config_path, sources_dir, "scoring:\n  fre_bonus: 2")


def test_unknown_weekday_key_raises(config_path: Path, sources_dir: Path) -> None:
    with pytest.raises(ValidationError):
        load_config(config_path, sources_dir, "scoring:\n  weekday_weights: { mnd: 3 }")


@pytest.mark.parametrize("profile", [None, "", "   \n"])
def test_missing_profile_loads_with_neutral_defaults(
    config_path: Path, sources_dir: Path, profile: str | None
) -> None:
    config = load_config(config_path, sources_dir, profile)

    assert config.recipient_email is None
    assert config.home is None
    assert config.scoring.proximity is None
    assert config.scoring.keyword_boosts == {}
    assert config.scoring.free_bonus == 0
    assert config.scoring.novelty_bonus == 0
    assert config.scoring.cheap_bonus is None
    assert config.scoring.soon_bonus is None
    assert config.scoring.weekday_weights == {}

    # No filter narrows the run beyond the horizon in schedule.horizon_days.
    assert config.filters.categories is None
    assert config.filters.max_price_huf is None
    assert config.filters.blocked_keywords == []
    assert config.filters.min_score == 0
    assert config.schedule.horizon_days == 20


def test_every_category_gets_a_weight(config_path: Path, sources_dir: Path) -> None:
    config = load_config(config_path, sources_dir, None)
    assert set(config.scoring.category_weights) == set(config.categories)
    assert set(config.scoring.category_weights.values()) == {1}

    with_profile = load_config(config_path, sources_dir, PROFILE)
    assert with_profile.scoring.category_weights["tarsasjatek"] == 5
    assert with_profile.scoring.category_weights["koncert"] == 1


def test_config_is_frozen(config_path: Path, sources_dir: Path) -> None:
    config = load_config(config_path, sources_dir, None)
    with pytest.raises(ValidationError):
        config.min_category_score = 99
    with pytest.raises(ValidationError):
        config.schedule.horizon_days = 99


def test_public_config_parses(config_path: Path, sources_dir: Path) -> None:
    config = load_config(config_path, sources_dir, None)
    assert config.version == 1
    assert config.categories["koncert"].native_types == ["concert"]
    assert config.categories["koncert"].keywords["élő zene"] == 3
    assert [target.type for target in config.delivery] == ["smtp", "telegram"]
    # Deliberately not asserted against a literal: `llm.enabled` is an operator switch, and
    # the two tests above already cover what each position does. What matters here is that
    # the shipped file parses, whichever way it happens to be set.
    assert isinstance(config.llm.enabled, bool)


def _llm_extra_installed() -> bool:
    try:
        return importlib.util.find_spec("google.genai") is not None
    except ModuleNotFoundError:
        return False


def _config_with_llm(config_path: Path, tmp_path: Path, *, enabled: bool) -> Path:
    """A copy of the shipped config with the llm switch forced either way.

    Forced, not read: these two tests are about what each SETTING does, and pinning them to
    whatever value config.yaml currently ships makes them fail every time somebody retunes
    it — which is exactly what happened twice."""
    lines = config_path.read_text(encoding="utf-8").splitlines(keepends=True)
    inside_llm = False
    for index, line in enumerate(lines):
        if line.startswith("llm:"):
            inside_llm = True
        elif inside_llm and line.strip().startswith("enabled:"):
            lines[index] = f"  enabled: {str(enabled).lower()}\n"
            break
    else:  # pragma: no cover - the llm block is in config.yaml
        raise AssertionError("config.yaml has no llm.enabled switch any more")
    copy = tmp_path / "config.yaml"
    copy.write_text("".join(lines), encoding="utf-8")
    return copy


@pytest.mark.skipif(_llm_extra_installed(), reason="the `llm` extra is installed here")
def test_llm_enabled_without_the_extra_fails_at_load_naming_the_install(
    config_path: Path, sources_dir: Path, tmp_path: Path
) -> None:
    """A flag that is on but cannot work has to say so where it is read. Before this, the
    missing package surfaced as a ModuleNotFoundError from inside a pipeline stage, on
    whichever run first had an event worth asking the model about."""
    enabled = _config_with_llm(config_path, tmp_path, enabled=True)

    with pytest.raises(ConfigError) as excinfo:
        load_config(enabled, sources_dir, None)

    message = str(excinfo.value)
    assert "pip install -e .[llm]" in message
    assert "llm.enabled" in message


def test_llm_disabled_loads_whether_or_not_the_extra_is_present(
    config_path: Path, sources_dir: Path, tmp_path: Path
) -> None:
    # Switched off, the optional package is nobody's business — the check must not run at
    # all, on a machine that has it or one that does not.
    disabled = _config_with_llm(config_path, tmp_path, enabled=False)

    config = load_config(disabled, sources_dir, None)

    assert config.llm.enabled is False


def test_sources_are_keyed_by_filename(config_path: Path, tmp_path: Path) -> None:
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "port-hu.yaml").write_text("id: port-hu\npriority: 10\n", encoding="utf-8")

    config = load_config(config_path, sources, None)
    assert config.sources["port-hu"]["priority"] == 10


def test_missing_sources_dir_is_tolerated(config_path: Path, tmp_path: Path) -> None:
    assert load_config(config_path, tmp_path / "absent", None).sources == {}


def test_broken_profile_yaml_raises_config_error(config_path: Path, sources_dir: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(config_path, sources_dir, "scoring: [unclosed")


def test_missing_config_file_raises_config_error(tmp_path: Path, sources_dir: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "absent.yaml", sources_dir, None)
