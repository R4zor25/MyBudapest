from __future__ import annotations

import pytest
from structlog.testing import capture_logs

from digest.models import RawEvent
from digest.pipeline.normalize import normalize_district


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # 1. An int, as Port.hu's address.district publishes it.
        (11, "XI."),
        (1, "I."),
        (23, "XXIII."),
        # 2. A Roman string, already canonical or nearly so.
        ("XI.", "XI."),
        ("XI", "XI."),
        ("xi", "XI."),
        ("IX. kerület", "IX."),
        ("V. ker.", "V."),
        # 3. Hungarian text, the shape programturizmus published before §6.6 dropped it.
        ("9. kerület - Ferencváros", "IX."),
        ("13. kerület", "XIII."),
        ("5. kerület - Belváros-Lipótváros", "V."),
        ("1. kerület - Várkerület", "I."),
        # 4. A Budapest postal code.
        ("1113", "XI."),
        ("1053", "V."),
        ("1013", "I."),
    ],
)
def test_every_published_shape_maps_to_the_canonical_roman_form(value, expected: str) -> None:
    assert normalize_district(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "   ",
        "Ferencváros",  # a district NAME, with no number anywhere
        "kerület",
        24,  # past the last real district
        0,
        "XXIV.",  # a Roman numeral that is not a district
        "9026",  # Győr: a valid Hungarian postal code, not a Budapest one
        "2315",  # Szigethalom -- the trap: its leading "23" is a real district number
    ],
)
def test_unrecognised_input_is_none_and_never_a_guess(value) -> None:
    assert normalize_district(value) is None


def test_a_non_budapest_postal_code_is_not_read_as_a_district_number() -> None:
    """The ordering that matters: a four-digit run is a postal code, terminally. Falling
    through to the leading-number branch would turn Szigethalom's 2315 into district
    XXIII. -- a wrong district is worse than a missing one, because it scores (§7.7)."""
    assert normalize_district("2315") is None
    assert normalize_district("23") == "XXIII."


def test_an_unrecognised_value_is_logged_at_debug() -> None:
    with capture_logs() as logs:
        normalize_district("Ferencváros")

    (entry,) = [line for line in logs if line["event"] == "district_unrecognised"]
    assert entry["value"] == "Ferencváros"
    assert entry["log_level"] == "debug"


def test_a_recognised_value_logs_nothing() -> None:
    with capture_logs() as logs:
        normalize_district("9. kerület - Ferencváros")

    assert not [line for line in logs if line["event"] == "district_unrecognised"]


# --------------------------------------------------------------------------------------
# Every source path goes through the one normalizer
# --------------------------------------------------------------------------------------


def make_raw(**overrides) -> RawEvent:
    base = {
        "source_id": "test",
        "source_event_key": "k",
        "title": "Esemény",
        "url": "https://example.hu/x",
        "start_raw": "2026-08-20 19:00:00",
    }
    return RawEvent(**{**base, **overrides})


def normalized_district(**overrides) -> str | None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from digest.config import Config
    from digest.pipeline.normalize import normalize

    now = datetime(2026, 8, 18, 10, tzinfo=ZoneInfo("Europe/Budapest"))
    (event,) = normalize([make_raw(**overrides)], Config(), now=now)
    return event.district


def test_the_pipeline_normalizes_whatever_the_source_supplied() -> None:
    # Port.hu hands over the int it publishes; a text-publishing source hands over text; the
    # plugins that only know a postal code hand over nothing at all.
    assert normalized_district(district_raw=11) == "XI."
    assert normalized_district(district_raw="9. kerület - Ferencváros") == "IX."
    assert normalized_district(postal_code="1053") == "V."


def test_the_postal_code_is_the_fallback_not_the_override() -> None:
    """A source that states a district is believed over its own postal code."""
    assert normalized_district(district_raw=11, postal_code="1053") == "XI."


def test_an_unusable_district_falls_back_to_the_postal_code() -> None:
    assert normalized_district(district_raw="Ferencváros", postal_code="1093") == "IX."
