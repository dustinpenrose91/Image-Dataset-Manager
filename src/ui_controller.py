"""
ui_controller — mutation/intent layer between the UI widgets and the
federation/worker layer.

`AppController` is a plain QObject (no widget imports). It owns DB-write
submissions through a bridge (`QtDBBridge` in production, a synchronous stub in
tests) and reports outcomes via signals. Widgets gather user input (dialogs,
selections) and call intent methods; the main window connects the controller's
change signals to its refresh slots.

Refresh policy is encoded in *which* signal an intent emits, mirroring the
pre-extraction behaviour exactly:

  - Single tag add/remove (detail panel): `tag_suggestions_stale` — only the
    autocomplete suggestion list needs a (debounced) rescan.
  - Tag-management + tag→selection/filtered ops: `tags_changed` — the full tag
    refresh (suggestions + tag-management list + detail reload).
  - Captions, validation flags, mask/perceptual-hash writes, and batch tag
    edits: no change signal (the detail panel already reflects them
    optimistically, matching prior behaviour).
  - Errors from any job: `error`.

The bridge contract is exactly `QtDBBridge.submit(fn, *args, on_result=,
on_error=, **kwargs)`, where `fn(fed, *args)` runs on the worker thread and the
callbacks run on the GUI thread.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, Signal

import federation
import imgdb


class AppController(QObject):
    error = Signal(object)             # (exception)
    tag_suggestions_stale = Signal()   # only the suggestion autocomplete is stale
    tags_changed = Signal()            # full tag refresh warranted
    assets_changed = Signal(bool)      # asset set changed; arg = preserve scroll
    datasets_changed = Signal()        # dataset membership/names changed

    def __init__(self, bridge: Any, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._bridge = bridge

    def _submit(
        self,
        op: Callable[[federation.Federation], Any],
        on_result: Optional[Callable[[Any], None]] = None,
    ) -> None:
        self._bridge.submit(op, on_result=on_result, on_error=self.error.emit)

    # -- single tag edits (detail panel) ------------------------------------

    def add_tag(self, asset_id: str, name: str, type_name: str) -> None:
        self._submit(
            lambda fed: federation.add_tags(fed, asset_id, [name], type_name=type_name),
            on_result=lambda _: self.tag_suggestions_stale.emit(),
        )

    def remove_tag(self, asset_id: str, tag_id: str) -> None:
        self._submit(
            lambda fed: federation.remove_tags(fed, asset_id, [tag_id]),
            on_result=lambda _: self.tag_suggestions_stale.emit(),
        )

    # -- captions / validation / mask / perceptual hash (write-only) --------

    def save_caption(self, asset_id: str, kind: str, content: str) -> None:
        self._submit(lambda fed: federation.set_caption(fed, asset_id, kind, content))

    def delete_caption(self, asset_id: str, kind: str) -> None:
        self._submit(lambda fed: federation.delete_caption(fed, asset_id, kind))

    def set_tags_validated(self, asset_id: str, validated: bool) -> None:
        self._submit(
            lambda fed: imgdb.set_tags_validated(
                federation.shard_for_asset(fed, asset_id).conn, asset_id, validated
            )
        )

    def set_caption_validated(self, asset_id: str, kind: str, validated: bool) -> None:
        self._submit(
            lambda fed: imgdb.set_caption_validated(
                federation.shard_for_asset(fed, asset_id).conn, asset_id, kind, validated
            )
        )

    def set_favorite(self, asset_id: str, on: bool) -> None:
        # Detail panel reflects the change optimistically, so no refresh signal;
        # any active favorite filter reconciles on the next filter change.
        self._submit(
            lambda fed: federation.set_image_flag(fed, asset_id, imgdb.ATTR_IS_FAVORITE, on)
        )

    def set_has_mask(self, abs_path: str, has_mask: bool) -> None:
        self._submit(lambda fed: federation.set_has_mask_by_abs_path(fed, abs_path, has_mask))

    def set_perceptual_hash(self, abs_path: str, phash: str) -> None:
        self._submit(lambda fed: federation.set_perceptual_hash_by_abs_path(fed, abs_path, phash))

    # -- batch tag edits (dialog-gathered) ----------------------------------
    # These change tag membership across many assets, so they emit tags_changed
    # to refresh the suggestion list and the Tag Management panel's counts.

    def batch_add_tag(self, asset_ids: list[str], tags: list[str], type_name: str) -> None:
        def op(fed: federation.Federation) -> None:
            for tag in tags:
                federation.add_tag_to_asset_ids(fed, asset_ids, tag, type_name)

        self._submit(op, on_result=lambda _: self.tags_changed.emit())

    def batch_remove_tag(self, asset_ids: list[str], tag: str) -> None:
        def op(fed: federation.Federation) -> None:
            for asset_id in asset_ids:
                federation.remove_tags_by_name(fed, asset_id, tag)

        self._submit(op, on_result=lambda _: self.tags_changed.emit())

    def batch_replace_tag(self, asset_ids: list[str], old_tag: str, new_tag: str) -> None:
        def op(fed: federation.Federation) -> None:
            for asset_id in asset_ids:
                federation.remove_tags_by_name(fed, asset_id, old_tag)
                federation.add_tags(fed, asset_id, [new_tag])

        self._submit(op, on_result=lambda _: self.tags_changed.emit())

    # -- tag-management operations (full tags_changed) ----------------------

    def add_tag_to_selection(self, asset_ids: list[str], name: str, type_name: str) -> None:
        self._submit(
            lambda fed: federation.add_tag_to_asset_ids(fed, asset_ids, name, type_name),
            on_result=lambda _: self.tags_changed.emit(),
        )

    def remove_tag_from_selection(self, asset_ids: list[str], name: str, type_name: str) -> None:
        self._submit(
            lambda fed: federation.remove_tag_from_asset_ids(fed, asset_ids, name, type_name),
            on_result=lambda _: self.tags_changed.emit(),
        )

    def add_tag_to_filtered(
        self, name: str, type_name: str, labels: Optional[list[str]], rules: list
    ) -> None:
        self._submit(
            lambda fed: federation.add_tag_to_filtered_assets(fed, name, type_name, labels, rules),
            on_result=lambda _: self.tags_changed.emit(),
        )

    def remove_tag_from_filtered(
        self, name: str, type_name: str, labels: Optional[list[str]], rules: list
    ) -> None:
        self._submit(
            lambda fed: federation.remove_tag_from_filtered_assets(fed, name, type_name, labels, rules),
            on_result=lambda _: self.tags_changed.emit(),
        )

    def replace_tag_globally(
        self, old_name: str, old_type: str, new_name: str, new_type: str
    ) -> None:
        self._submit(
            lambda fed: federation.replace_tag_globally(fed, old_name, old_type, new_name, new_type),
            on_result=lambda _: self.tags_changed.emit(),
        )

    def delete_tag_globally(self, name: str, type_name: str) -> None:
        self._submit(
            lambda fed: federation.delete_tag_globally(fed, name, type_name),
            on_result=lambda _: self.tags_changed.emit(),
        )

    # -- asset mutations ----------------------------------------------------

    def rename_asset(self, asset_id: str, new_rel_path: str, force: bool = False) -> None:
        self._submit(
            lambda fed: federation.rename_asset(fed, asset_id, new_rel_path, force=force),
            on_result=lambda _: self.assets_changed.emit(False),
        )

    def rename_assets(self, planned: list[tuple[str, str, bool]]) -> None:
        """Batch move: each item is (asset_id, new_rel_path, force)."""
        def op(fed: federation.Federation) -> None:
            for asset_id, new_rel, force in planned:
                federation.rename_asset(fed, asset_id, new_rel, force=force)

        self._submit(op, on_result=lambda _: self.assets_changed.emit(False))

    def delete_assets(self, asset_ids: list[str]) -> None:
        def op(fed: federation.Federation) -> None:
            for asset_id in asset_ids:
                federation.delete_asset(fed, asset_id)

        self._submit(op, on_result=lambda _: self.assets_changed.emit(True))

    # -- dataset membership -------------------------------------------------

    def remove_from_dataset(self, name: str, asset_ids: list[str]) -> None:
        def on_done(_: Any) -> None:
            self.datasets_changed.emit()
            self.assets_changed.emit(True)

        self._submit(
            lambda fed: federation.remove_from_dataset(fed, name, asset_ids),
            on_result=on_done,
        )

    def rename_dataset(self, old_name: str, new_name: str) -> None:
        self._submit(
            lambda fed: federation.rename_dataset(fed, old_name, new_name),
            on_result=lambda _: self.datasets_changed.emit(),
        )
