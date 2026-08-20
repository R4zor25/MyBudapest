from __future__ import annotations

from pathlib import Path

import yaml


def test_every_enabled_source_has_at_least_one_listing_url(sources_dir: Path) -> None:
    # AUDIT-5 BLOCKER: sources/port-hu.yaml shipped `enabled: true` with `listing.urls: []`
    # -- the "backbone source" silently contributed 0 events on every real run, forever,
    # and the automated selector-drift check (SPEC 13) can never catch it, because its
    # trigger needs a last_count this source could never earn while stuck at 0. This is a
    # config-sanity invariant, not a per-source test: every declarative/plugin source in
    # this project reads its URLs from the same `listing.urls` key (declarative.py,
    # port_hu.py), so `enabled: true` with an empty list is never legitimate anywhere.
    broken = []
    for path in sorted(sources_dir.glob("*.yaml")):
        spec = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not spec.get("enabled", True):
            continue
        urls = (spec.get("listing") or {}).get("urls") or []
        if not urls:
            broken.append(path.name)
    assert not broken, (
        f"{broken} are enabled with no listing.urls -- they will silently contribute 0 "
        "events forever. Either fill in a real listing URL or set enabled: false until "
        "one is confirmed."
    )
