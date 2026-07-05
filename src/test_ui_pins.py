"""Tests for ui_pins — pure pin resolution and self-healing."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from ui_pins import resolve_pins, uuids_for_name


def _ds(name: str, ids: dict) -> SimpleNamespace:
    return SimpleNamespace(name=name, ids=ids)


DATASETS = [
    _ds("solo", {"alpha": "u-solo-a"}),
    _ds("spanning", {"alpha": "u-span-a", "beta": "u-span-b"}),
]


class ResolvePinsTests(unittest.TestCase):

    def test_no_pins(self):
        names, healed = resolve_pins(set(), DATASETS)
        self.assertEqual(names, set())
        self.assertEqual(healed, set())

    def test_single_shard_pin(self):
        names, healed = resolve_pins({"u-solo-a"}, DATASETS)
        self.assertEqual(names, {"solo"})
        self.assertEqual(healed, {"u-solo-a"})

    def test_self_heal_adds_sibling_uuids(self):
        # Pinned via alpha's UUID only; healing widens to beta's UUID so the
        # pin survives if alpha is later detached.
        names, healed = resolve_pins({"u-span-a"}, DATASETS)
        self.assertEqual(names, {"spanning"})
        self.assertEqual(healed, {"u-span-a", "u-span-b"})

    def test_unresolvable_pin_retained_in_healed_set(self):
        # UUID from a detached shard: no name resolves, but the pin is kept.
        names, healed = resolve_pins({"u-gone"}, DATASETS)
        self.assertEqual(names, set())
        self.assertIn("u-gone", healed)

    def test_none_ids_ignored(self):
        broken = [_ds("nulled", {"alpha": None})]
        names, healed = resolve_pins({"u-x"}, broken)
        self.assertEqual(names, set())
        self.assertNotIn(None, healed)


class UuidsForNameTests(unittest.TestCase):

    def test_found(self):
        self.assertEqual(
            uuids_for_name("spanning", DATASETS), {"u-span-a", "u-span-b"}
        )

    def test_missing(self):
        self.assertEqual(uuids_for_name("nope", DATASETS), set())


if __name__ == "__main__":
    unittest.main(verbosity=2)
