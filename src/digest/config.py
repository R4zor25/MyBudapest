from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Literal

import structlog
import yaml
from pydantic import BaseModel, ConfigDict, field_validator

from digest.errors import ConfigError

log = structlog.get_logger()

_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# Neutral weight for a category the profile says nothing about (§5.3).
_DEFAULT_CATEGORY_WEIGHT = 1.0


class _Section(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ScheduleConfig(_Section):
    timezone: str = "Europe/Budapest"
    horizon_days: int = 14


class FetchConfig(_Section):
    user_agent: str = "budapest-event-digest/1.0"
    timeout_seconds: float = 20
    max_retries: int = 3
    backoff_base_seconds: float = 2
    default_rate_limit_seconds: float = 1.5
    respect_robots_txt: bool = True


class CategoryRules(_Section):
    keywords: dict[str, float] = {}
    venue_prior: dict[str, float] = {}
    url_patterns: list[str] = []
    native_types: list[str] = []


class GroupingConfig(_Section):
    collapse_by: list[str] = ["venue_name", "effective_date", "primary_category"]
    min_group_size: int = 4
    max_per_venue: int = 3


class RecurrenceConfig(_Section):
    series_threshold_days: int = 7
    series_behavior: str = "send_once"
    run_behavior: str = "send_at_start"


class NightShiftConfig(_Section):
    before_hour: int = 5


class ExpiringSectionConfig(_Section):
    enabled: bool = True
    within_days: int = 3


class NewsletterConfig(_Section):
    per_category_limit: int = 5
    total_limit: int = 25
    send_when_empty: bool = True
    expiring_section: ExpiringSectionConfig = ExpiringSectionConfig()
    # WHAT COUNTS AS "the reader has seen this" (§8.2). Two behaviours, one switch:
    #
    #   "web"   — everything published to the site is recorded. The email is a top slice
    #             and the site is the full view, so an event that only made the site has
    #             still been shown. Surplus does NOT come back tomorrow.
    #   "email" — only what got a card in the email is recorded. Surplus queues and is
    #             re-offered on later runs.
    #
    # "email" was right while the email WAS the digest. It stopped being right when the
    # site started publishing everything: tomorrow's mail would spend its slots on
    # yesterday's leftovers instead of today's best, and the reader would have already
    # seen them on the page.
    ledger_records: Literal["web", "email"] = "web"


class LLMConfig(_Section):
    enabled: bool = False
    provider: str = "gemini"
    model: str = "gemini-2.5-flash-lite"
    batch_size: int = 35
    max_calls_per_run: int = 12
    # Not configurable on purpose: the LLM is never on the critical path (CLAUDE.md 4).
    on_quota_error: Literal["fallback_to_rules"] = "fallback_to_rules"
    only_for: list[str] = ["uncategorized", "ambiguous_dedup"]


class DeliveryTarget(_Section):
    type: Literal["smtp", "telegram"]
    enabled: bool = True


class SiteConfig(_Section):
    # The published site's ABSOLUTE base URL. `base_path` cannot serve here: a link in a
    # mail client has no page to be relative to, so a path-only href is dead on arrival.
    # Empty means "no site link" and the email simply omits the button rather than
    # rendering a broken one.
    base_url: str = ""
    base_path: str = ""
    archive_keep_days: int = 90


class HomeConfig(_Section):
    district: str
    lat: float
    lon: float


class CheapBonus(_Section):
    under_huf: int
    points: float


class SoonBonus(_Section):
    within_days: int
    points: float


class ProximityConfig(_Section):
    same_district_bonus: float = 0
    # BOUNDS A PENALTY, never excludes: the distance penalty is charged on
    # min(distance_km, penalty_cap_km). Named apart from filters.geo.max_distance_km on
    # purpose -- the two used to share a name and do different things (§7.7).
    penalty_cap_km: float | None = None
    distance_penalty_per_km: float = 0


class ScoringConfig(_Section):
    category_weights: dict[str, float] = {}
    keyword_boosts: dict[str, float] = {}
    free_bonus: float = 0
    cheap_bonus: CheapBonus | None = None
    proximity: ProximityConfig | None = None
    novelty_bonus: float = 0
    soon_bonus: SoonBonus | None = None
    weekday_weights: dict[str, float] = {}

    @field_validator("weekday_weights")
    @classmethod
    def _check_weekdays(cls, value: dict[str, float]) -> dict[str, float]:
        if unknown := sorted(set(value) - set(_WEEKDAYS)):
            raise ValueError(f"unknown weekday keys {unknown}, expected any of {list(_WEEKDAYS)}")
        return value


class GeoFilterConfig(_Section):
    """§7.6's geographic exclusion. Every default is neutral: an absent `filters.geo`
    block, or a block with no `city`, excludes nothing at all.

    `max_distance_km` here EXCLUDES THE EVENT. Its counterpart
    `scoring.proximity.penalty_cap_km` only bounds how large the distance penalty can
    grow. One decides whether the reader sees the event at all, the other only how high
    it ranks; they are never merged, and since §7.7 they no longer share a name."""

    city: str | None = None
    # True on purpose. Most sources publish no settlement at all, and dropping every
    # such event would silently lose good ones; the source list is Budapest-oriented
    # already, so failing open is the safe direction (requirement 2).
    allow_missing_city: bool = True
    # EXCLUDES THE EVENT, unlike scoring.proximity.penalty_cap_km which bounds a penalty.
    max_distance_km: float | None = None


class FiltersConfig(_Section):
    categories: list[str] | None = None
    max_price_huf: int | None = None
    blocked_keywords: list[str] = []
    min_score: float = 0
    geo: GeoFilterConfig = GeoFilterConfig()


class Config(_Section):
    version: int = 1
    schedule: ScheduleConfig = ScheduleConfig()
    fetch: FetchConfig = FetchConfig()
    categories: dict[str, CategoryRules] = {}
    min_category_score: float = 2
    fallback_category: str = "egyeb"
    grouping: GroupingConfig = GroupingConfig()
    recurrence: RecurrenceConfig = RecurrenceConfig()
    night_shift: NightShiftConfig = NightShiftConfig()
    newsletter: NewsletterConfig = NewsletterConfig()
    llm: LLMConfig = LLMConfig()
    delivery: list[DeliveryTarget] = []
    site: SiteConfig = SiteConfig()
    sources: dict[str, dict[str, Any]] = {}

    # From the PROFILE_YAML secret (§5.2). Every one of these has a neutral default, so a
    # clone without the secret still runs — it is just not personalised (§5.3).
    recipient_email: str | None = None
    home: HomeConfig | None = None
    scoring: ScoringConfig = ScoringConfig()
    filters: FiltersConfig = FiltersConfig()


def _parse_yaml_mapping(text: str, origin: str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        # AUDIT-3 BLOCKER: yaml.YAMLError's default str() includes a verbatim source-line
        # snippet (Mark.get_snippet()) — fine for the public config.yaml/sources/*.yaml,
        # but PROFILE_YAML is the secret itself, and that snippet is a raw profile line
        # (a scoring weight, a keyword, the recipient address). Report the grammar problem
        # and its line/column only, never the reconstructed source text, for that origin.
        if origin == "PROFILE_YAML":
            raise ConfigError(f"{origin} is not valid YAML: {_redact_yaml_error(exc)}") from exc
        raise ConfigError(f"{origin} is not valid YAML: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{origin} must be a mapping, got {type(data).__name__}")
    return data


def _redact_yaml_error(exc: yaml.YAMLError) -> str:
    problem = getattr(exc, "problem", None) or "invalid syntax"
    mark = getattr(exc, "problem_mark", None)
    location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
    return f"{problem}{location}"


def _read_yaml_file(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    return _parse_yaml_mapping(text, str(path))


def _load_source_specs(sources_dir: Path) -> dict[str, dict[str, Any]]:
    if not sources_dir.is_dir():
        log.warning("sources_dir_missing", path=str(sources_dir))
        return {}
    return {path.stem: _read_yaml_file(path) for path in sorted(sources_dir.glob("*.yaml"))}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def _with_neutral_category_weights(data: dict[str, Any]) -> dict[str, Any]:
    """Every configured category gets an explicit weight, so "no profile" means weight 1
    everywhere instead of a default hidden inside the scoring stage (§5.3)."""
    categories = data.get("categories") or {}
    scoring = dict(data.get("scoring") or {})
    weights = dict(scoring.get("category_weights") or {})
    for name in categories:
        weights.setdefault(name, _DEFAULT_CATEGORY_WEIGHT)
    scoring["category_weights"] = weights
    return {**data, "scoring": scoring}


def load_config(
    config_path: Path,
    sources_dir: Path,
    profile_yaml: str | None,
) -> Config:
    public = _read_yaml_file(config_path)
    if profile_yaml and profile_yaml.strip():
        profile = _parse_yaml_mapping(profile_yaml, "PROFILE_YAML")
    else:
        log.warning("profile_missing", reason="PROFILE_YAML empty, running unpersonalised")
        profile = {}

    merged = _deep_merge(public, profile)
    if "sources" in merged:
        raise ConfigError("`sources` comes from the sources directory; remove it from the config")
    merged["sources"] = _load_source_specs(sources_dir)

    # Raises pydantic.ValidationError as-is (unchanged contract, see test_config.py) — the
    # secret-leak risk in that error's default str() is real (AUDIT-3 BLOCKER), but it is
    # handled at the CLI boundary (cli._run_real), the only path whose output actually
    # reaches a public Actions log, rather than here where it would also mean library
    # callers/tests can no longer match on the specific pydantic exception type.
    config = Config.model_validate(_with_neutral_category_weights(merged))
    _check_llm_extra(config)
    return config


def _check_llm_extra(config: Config) -> None:
    """`llm.enabled: true` without `google-genai` installed used to surface as a
    ModuleNotFoundError from inside a pipeline stage, on whichever run first had an event
    worth asking about — a config mistake reported as a runtime crash, hours later and in
    the wrong place. It fails here instead, where the flag is read.

    Note where the extra is NOT installed: `.github/workflows/digest.yml` runs
    `pip install -e .`, so switching this on means adding `.[llm]` there too, not only
    locally."""
    if not config.llm.enabled:
        return
    require_llm_extra("llm.enabled is true but")


def require_llm_extra(because: str = "this needs the Gemini layer, but") -> None:
    """Raises unless `google-genai` can be imported. Separate from the flag so the
    comparison command (`digest categorize --compare-llm`) can demand it too — that command
    exists precisely to decide whether to switch the flag on, so it cannot be gated on it
    already being on."""
    try:
        installed = importlib.util.find_spec("google.genai") is not None
    except ModuleNotFoundError:
        # find_spec imports the PARENT package to search it, so a missing `google` raises
        # here rather than answering None — the very error this check exists to replace.
        installed = False
    if not installed:
        raise ConfigError(
            f"{because} the optional `llm` extra is not installed. "
            "Run `pip install -e .[llm]` (and add it to .github/workflows/digest.yml), "
            "or set `llm.enabled: false` in config.yaml."
        )
