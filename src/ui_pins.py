"""
Pure pin-resolution logic for pinned datasets. No Qt, no persistence, no
federation imports — operates on any objects with `.name` and `.ids` (dict of
shard label → dataset_id), i.e. federation.DatasetInfo.

The pin store (held by the UI in imgdb_ui.ini) is a flat set of dataset_id
UUIDs — never names, so nothing in the centralized config identifies shard
contents. A logical (federation-level) dataset is pinned if ANY of its
per-shard UUIDs is in the set.

Self-healing: the same logical dataset has a different UUID in each shard it
spans. resolve_pins() returns the pin set widened with sibling UUIDs from
currently-attached shards, so a pin keeps matching no matter which subset of
its shards is attached later. UUIDs for detached shards are retained — the
shard may come back.
"""
from __future__ import annotations

from typing import Iterable


def resolve_pins(
    pinned: set[str], datasets: Iterable
) -> tuple[set[str], set[str]]:
    """Return (pinned_dataset_names, healed_pin_set) for the given datasets."""
    pinned_names: set[str] = set()
    healed = set(pinned)
    for ds in datasets:
        uuids = {u for u in ds.ids.values() if u}
        if uuids & pinned:
            pinned_names.add(ds.name)
            healed |= uuids
    return pinned_names, healed


def uuids_for_name(name: str, datasets: Iterable) -> set[str]:
    """All per-shard UUIDs of the named dataset (empty if not present)."""
    for ds in datasets:
        if ds.name == name:
            return {u for u in ds.ids.values() if u}
    return set()
