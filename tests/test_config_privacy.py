from __future__ import annotations

from pathlib import Path

import yaml

# These live in the PROFILE_YAML secret and nowhere else (SPEC §5.3, §12).
PROFILE_ONLY_KEYS = ("scoring", "home", "recipient_email", "filters")

LEAK_EXPLANATION = (
    "These keys belong in the PROFILE_YAML secret only. This repository is public, so "
    "committing them publishes them — and the scoring weights, keyword boosts and home "
    "district together are a readable map of your taste and where you live (SPEC §12). "
    "Move them back into the secret. Do not relax this test to make it pass."
)


def _public_config_files(config_path: Path, sources_dir: Path) -> list[Path]:
    # AUDIT-3 MINOR-2: config.yaml was the only file this guard ever checked, but SPEC §12
    # treats sources/*.yaml as the same public surface — a leak there would have gone
    # undetected. Every file under both is public repo content once this repo is public.
    return [config_path, *sorted(sources_dir.glob("*.yaml"))]


def test_public_config_has_no_profile_keys(config_path: Path, sources_dir: Path) -> None:
    for path in _public_config_files(config_path, sources_dir):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        leaked = sorted(set(raw) & set(PROFILE_ONLY_KEYS))
        assert not leaked, f"{path.name} contains profile keys {leaked}. {LEAK_EXPLANATION}"


def test_public_config_has_no_email_address(config_path: Path, sources_dir: Path) -> None:
    for path in _public_config_files(config_path, sources_dir):
        text = path.read_text(encoding="utf-8")
        assert "@" not in text, (
            f"{path.name} contains '@', which is most likely an email address. {LEAK_EXPLANATION}"
        )
