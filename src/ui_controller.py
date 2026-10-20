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

`AutoCaptionController` is a second widget-free QObject that drives the
auto-caption queue across the DB bridge and a dedicated caption bridge.
"""
from __future__ import annotations

import collections
import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, Signal

import captioner
import federation
import imgdb

logger = logging.getLogger(__name__)


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


# ---------------------------------------------------------------------------
# Auto-captioning queue
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AutoCaptionRequest:
    model: str                     # ModelSpec.name
    kind: str                      # caption kind to write
    prompt: str
    params: captioner.CaptionParams
    overwrite: bool                # False = skip assets that already have `kind`


class AutoCaptionController(QObject):
    """Captions queued assets one at a time, across three hops per asset:

      DB worker:      resolve asset_id -> absolute path (+ does `kind` exist?)
      caption worker: load the model if needed, generate (blocks on llama-server)
      DB worker:      write the caption

    The next asset starts only when the previous one is written, so the DB
    worker is never blocked by generation. Requests carry their own settings,
    so assets enqueued mid-run with different settings are honoured. The
    caption worker holds no federation; its jobs ignore their `fed` argument.
    """

    error = Signal(object)
    model_state_changed = Signal(str, str)   # model name ('' if none), state
    running_changed = Signal(bool)
    progress = Signal(int, int, str)         # done, total, current file name
    caption_written = Signal(str, str)       # asset_id, kind
    finished = Signal(int, int, int, bool)   # written, skipped, failed, cancelled

    # model_state_changed states
    UNLOADED, LOADING, READY, FAILED = "unloaded", "loading", "ready", "error"

    def __init__(
        self,
        db_bridge: Any,
        caption_bridge: Any,
        server: captioner.LlamaServer,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._db = db_bridge
        self._cap = caption_bridge
        self._server = server
        self._specs: dict[str, captioner.ModelSpec] = {}
        self._loaded: Optional[str] = None      # GUI-thread mirror of the server
        self._queue: collections.deque[tuple[str, AutoCaptionRequest]] = collections.deque()
        self._keys: set[tuple[str, str]] = set()  # (asset_id, kind) queued or in flight
        self._current: Optional[tuple[str, AutoCaptionRequest]] = None
        self._running = False
        self._reset_counts()

    def _reset_counts(self) -> None:
        self._done = self._written = self._skipped = self._failed = 0
        self._cancelled = False

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def loaded_model(self) -> Optional[str]:
        return self._loaded

    def set_models(self, specs: list[captioner.ModelSpec]) -> None:
        self._specs = {s.name: s for s in specs}

    def enqueue(self, asset_ids: list[str], request: AutoCaptionRequest) -> None:
        if request.model not in self._specs:
            self.error.emit(captioner.CaptionerError(f"Unknown caption model {request.model!r}"))
            return
        added = 0
        for aid in asset_ids:
            key = (aid, request.kind)
            if key not in self._keys:
                self._keys.add(key)
                self._queue.append((aid, request))
                added += 1
        if not added:
            return
        if not self._running:
            self._running = True
            self._reset_counts()
            self.running_changed.emit(True)
        self.progress.emit(self._done, self._total(), "")
        if self._current is None:
            self._next()

    def cancel(self) -> None:
        """Drop everything not yet started; the in-flight asset still finishes."""
        if not self._running:
            return
        self._cancelled = True
        for aid, req in self._queue:
            self._keys.discard((aid, req.kind))
        self._queue.clear()
        if self._current is None:
            self._finish()

    def unload(self) -> None:
        """Stop the server and free VRAM. Ignored while a run is active."""
        if self._running:
            return

        def on_done(_: Any) -> None:
            self._loaded = None
            self.model_state_changed.emit("", self.UNLOADED)

        self._cap.submit(lambda _fed: self._server.unload(),
                         on_result=on_done, on_error=self.error.emit)

    # -- pipeline -----------------------------------------------------------

    def _total(self) -> int:
        return self._done + len(self._queue) + (1 if self._current else 0)

    def _next(self) -> None:
        if not self._queue:
            self._current = None
            self._finish()
            return
        self._current = self._queue.popleft()
        aid, req = self._current

        def resolve(fed: federation.Federation) -> tuple[str, bool]:
            shard = federation.shard_for_asset(fed, aid)
            real_id = imgdb.resolve_merged_id(shard.conn, aid)
            asset = imgdb.get_asset(shard.conn, real_id)
            exists = req.kind in imgdb.get_captions_for_asset(shard.conn, real_id)
            return os.path.join(shard.abs_path, asset.rel_path), exists

        self._db.submit(resolve, on_result=self._on_resolved, on_error=self._on_item_failed)

    def _on_resolved(self, result: tuple[str, bool]) -> None:
        abs_path, exists = result
        aid, req = self._current
        if exists and not req.overwrite:
            self._skipped += 1
            self._advance()
            return
        self.progress.emit(self._done, self._total(), os.path.basename(abs_path))
        spec = self._specs[req.model]
        if self._loaded != spec.name:
            self.model_state_changed.emit(spec.name, self.LOADING)

        def generate(_fed: Any) -> str:
            self._server.load(spec)
            return self._server.caption(abs_path, req.prompt, req.params)

        self._cap.submit(generate, on_result=self._on_generated, on_error=self._on_generate_failed)

    def _on_generated(self, text: str) -> None:
        aid, req = self._current
        if self._loaded != req.model:
            self._loaded = req.model
            self.model_state_changed.emit(req.model, self.READY)

        def on_written(_: Any) -> None:
            self._written += 1
            self.caption_written.emit(aid, req.kind)
            self._advance()

        self._db.submit(
            lambda fed: federation.set_caption(fed, aid, req.kind, text),
            on_result=on_written, on_error=self._on_item_failed,
        )

    def _on_generate_failed(self, exc: BaseException) -> None:
        if isinstance(exc, captioner.ImageError):
            self._on_item_failed(exc)
            return
        # Server-level failure: every remaining asset would fail the same way.
        self._loaded = None
        self.model_state_changed.emit(self._current[1].model, self.FAILED)
        self.error.emit(exc)
        self._failed += 1
        for aid, req in self._queue:
            self._keys.discard((aid, req.kind))
        self._queue.clear()
        self._advance()

    def _on_item_failed(self, exc: BaseException) -> None:
        logger.warning("Auto-caption failed for %s: %s", self._current[0], exc)
        self._failed += 1
        self._advance()

    def _advance(self) -> None:
        aid, req = self._current
        self._keys.discard((aid, req.kind))
        self._current = None
        self._done += 1
        self.progress.emit(self._done, self._total(), "")
        self._next()

    def _finish(self) -> None:
        if not self._running:
            return
        self._running = False
        self.finished.emit(self._written, self._skipped, self._failed, self._cancelled)
        self.running_changed.emit(False)
        self._reset_counts()
