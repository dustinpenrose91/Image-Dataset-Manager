"""
ui_flows — multi-step UI flows that interleave worker jobs with dialogs.

`CaptionImportFlow` is the "Import tags from caption" flow, previously a single
115-line method of nested closures in imgdb_ui.py. Each step is a named method
and all shared state lives on the instance, so the fetch → dialog →
single/bulk → prescan → resolve → import sequence reads top-to-bottom.

The flow needs a parent widget (dialogs are modal children of it), the DB
bridge, and two callbacks supplied by the main window: `on_tags_changed`
(post-import refresh) and `on_error`.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtWidgets import QDialog, QMessageBox, QWidget

import federation
import imgdb
from ui_dialogs import AmbiguousTagResolutionDialog, ImportFromCaptionDialog


class CaptionImportFlow:
    def __init__(
        self,
        bridge: Any,
        parent: QWidget,
        checked_labels: Optional[list[str]],
        filter_rules: list,
        on_tags_changed: Callable[[], None],
        on_error: Callable[[BaseException], None],
    ) -> None:
        self._bridge = bridge
        self._parent = parent
        self._checked = checked_labels
        self._rules = filter_rules
        self._on_tags_changed = on_tags_changed
        self._on_error = on_error

        self._asset_id: str = ""
        self._tag_lookup: dict = {}
        self._caption_kind: Optional[str] = None
        self._bulk_kwargs: dict = {}

    def start(self, asset_id: str) -> None:
        self._asset_id = asset_id
        self._bridge.submit(self._fetch, on_result=self._show_dialog, on_error=self._on_error)

    # -- step 1: gather the asset's captions + scope counts on the worker ----

    def _fetch(self, fed: federation.Federation) -> tuple:
        tag_lookup = federation.build_tag_lookup(fed)
        shard = federation.shard_for_asset(fed, self._asset_id)
        caption_texts = {
            kind: (content or "")
            for kind, (content, _validated)
            in imgdb.get_captions_for_asset(shard.conn, self._asset_id).items()
        }
        all_kinds = federation.list_all_caption_kinds(fed, self._checked)
        filtered_count = federation.count_filtered_assets(fed, self._checked, self._rules)
        total_count = federation.count_filtered_assets(fed, self._checked, [])
        return tag_lookup, caption_texts, all_kinds, filtered_count, total_count

    # -- step 2: present the import dialog, then branch on scope -------------

    def _show_dialog(self, data: tuple) -> None:
        tag_lookup, caption_texts, all_kinds, filtered_count, total_count = data
        if not caption_texts:
            QMessageBox.information(
                self._parent, "No captions", "This asset has no captions to import from."
            )
            return
        self._tag_lookup = tag_lookup

        dlg = ImportFromCaptionDialog(
            caption_texts=caption_texts,
            all_caption_kinds=all_kinds,
            match_func=lambda text: federation.match_tags_in_text(text, tag_lookup),
            filtered_count=filtered_count,
            total_count=total_count,
            parent=self._parent,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        scope = dlg.scope()
        self._caption_kind = dlg.caption_kind()

        if scope == "single":
            self._import_single(dlg.selected_tags())
            return

        # Bulk scope — kwargs shared by prescan and import.
        self._bulk_kwargs = dict(
            caption_kind=self._caption_kind,
            tag_lookup=self._tag_lookup,
            checked_labels=self._checked,
        )
        if scope == "filtered":
            self._bulk_kwargs["filter_rules"] = self._rules

        if dlg.ambiguous_policy() == "ask":
            self._bridge.submit(self._prescan, on_result=self._resolve, on_error=self._on_error)
        else:
            self._run_bulk_import({})

    # -- step 3a: single-asset import ---------------------------------------

    def _import_single(self, selected: list[tuple[str, str]]) -> None:
        if not selected:
            return

        def do_single(fed: federation.Federation) -> int:
            for name, type_name in selected:
                federation.add_tags(fed, self._asset_id, [name], type_name=type_name)
            return len(selected)

        self._bridge.submit(
            do_single,
            on_result=lambda _: self._on_tags_changed(),
            on_error=self._on_error,
        )

    # -- step 3b: bulk import (optionally resolving ambiguous names first) ---

    def _prescan(self, fed: federation.Federation) -> list:
        return federation.prescan_ambiguous_matches(fed, **self._bulk_kwargs)

    def _resolve(self, ambiguous: list) -> None:
        resolution: dict = {}
        if ambiguous:
            res_dlg = AmbiguousTagResolutionDialog(ambiguous, self._parent)
            if res_dlg.exec() != QDialog.DialogCode.Accepted:
                return
            resolution = res_dlg.resolution()
        self._run_bulk_import(resolution)

    def _run_bulk_import(self, resolution: dict) -> None:
        def do_bulk(fed: federation.Federation) -> int:
            return federation.bulk_import_caption_tags(
                fed, resolution=resolution, **self._bulk_kwargs
            )

        def on_bulk_done(count: int) -> None:
            self._on_tags_changed()
            noun = "assignment" if count == 1 else "assignments"
            QMessageBox.information(
                self._parent, "Import complete",
                f"Added {count} tag {noun} across matching images.",
            )

        self._bridge.submit(do_bulk, on_result=on_bulk_done, on_error=self._on_error)
