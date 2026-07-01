"""
imgdb_ui — main window for the Image Dataset Manager.

Layout (Browse mode):
    ┌──────────┬──────────────────────────────────┬───────────────┐
    │  Roots   │  Filter bar                       │               │
    │  (side-  │──────────────────────────────────│  Detail       │
    │   bar)   │  Asset table (virtualized)        │  Panel        │
    │          │                                   │               │
    └──────────┴──────────────────────────────────┴───────────────┘

Layout (Query mode):
    ┌──────────────────────────────────────────────────────────────┐
    │  QueryTab (full-width)                                       │
    └──────────────────────────────────────────────────────────────┘

The toolbar switches between Browse and Query. The status bar shows asset
counts and thumbnail-queue depth.
"""
from __future__ import annotations

import os
import sys
from typing import Optional

from PySide6.QtCore import QItemSelectionModel, QSettings, QTimer, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QHBoxLayout, QInputDialog, QLabel,
    QMainWindow, QMessageBox, QPushButton, QSpinBox, QSplitter,
    QStackedWidget, QStatusBar, QTabWidget, QToolBar, QVBoxLayout, QWidget,
)

import federation
import imgdb
import imgdb_thumbs
from filter_model import FilterRule, SortRule
from imgdb_thumbs_qt import QtThumbnailBridge
from imgdb_worker import DBWorker
from imgdb_worker_qt import QtDBBridge
from ui_asset_table import AssetTableModel, AssetTableView
from ui_detail_panel import DetailPanel
from ui_filter_panel import FilterPanel
from ui_dialogs import (
    AddToDatasetDialog, AmbiguousTagResolutionDialog,
    BatchMoveDialog, BatchRemoveTagDialog, BatchReplaceTagDialog, BatchTagDialog,
    BulkImportDialog, ChangeTagTypeDialog,
    ConfirmDeleteDialog, ImportFromCaptionDialog, MergeDialog, RenameDialog, ReplaceTagDialog,
)
from ui_preview_window import PreviewWindow
from ui_query_tab import QueryTab
from ui_roots_panel import RootsPanel
from ui_tag_panel import TagManagementPanel

DEFAULT_CONFIG = "./imgdb.conf"


class MainWindow(QMainWindow):
    def __init__(self, config_path: str = DEFAULT_CONFIG) -> None:
        super().__init__()
        self._config_path = os.path.abspath(config_path)
        self._fed: Optional[federation.Federation] = None
        self._worker: Optional[DBWorker] = None
        self._bridge: Optional[QtDBBridge] = None
        self._tag_suggestions: list = []
        self._tag_types: list[str] = ["General"]
        self._selected_assets: list[federation.AssetRow] = []
        self._thumb_worker = imgdb_thumbs.ThumbnailWorker()
        self._thumb_bridge: Optional[QtThumbnailBridge] = None

        self.setWindowTitle("Image Dataset Manager")
        self.showMaximized()

        self._setup_workers()
        self._build_ui()
        self._connect_signals()
        self._restore_settings()
        self._open_federation()

        # Poll thumbnail queue depth for the status bar.
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(1000)
        self._poll_timer.timeout.connect(self._update_status)
        self._poll_timer.start()

    # -----------------------------------------------------------------------
    # Workers
    # -----------------------------------------------------------------------

    def _setup_workers(self) -> None:
        self._thumb_worker.start()
        self._thumb_bridge = QtThumbnailBridge(self._thumb_worker)

        self._worker = DBWorker()
        cfg = self._config_path
        self._worker.start(
            lambda: federation.open_federation(
                cfg, on_warning=self._on_shard_warning
            ),
            on_warning=self._on_shard_warning,
        )
        self._bridge = QtDBBridge(self._worker)

    def _on_shard_warning(self, msg: str) -> None:
        # May fire before _build_ui() has created the status bar (e.g. during
        # federation open on startup), so guard before accessing it.
        if hasattr(self, "_status_bar"):
            self._status_bar.showMessage(f"Warning: {msg}", 5000)

    # -----------------------------------------------------------------------
    # UI construction
    # -----------------------------------------------------------------------

    def _build_ui(self) -> None:
        # Toolbar
        toolbar = QToolBar("Main", self)
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        self._browse_btn = QPushButton("Browse")
        self._browse_btn.setCheckable(True)
        self._browse_btn.setChecked(True)
        self._query_btn = QPushButton("Query")
        self._query_btn.setCheckable(True)
        toolbar.addWidget(self._browse_btn)
        toolbar.addWidget(self._query_btn)
        toolbar.addSeparator()

        self._preview_btn = QPushButton("Preview")
        self._preview_btn.setCheckable(True)
        self._preview_btn.setToolTip("Open floating image preview (P)")
        toolbar.addWidget(self._preview_btn)
        toolbar.addSeparator()

        self._import_btn = QPushButton("Import…")
        self._import_btn.setToolTip("Bulk import paired image/.txt files")
        toolbar.addWidget(self._import_btn)
        toolbar.addSeparator()


        # Status bar
        self._status_bar = QStatusBar(self)
        self.setStatusBar(self._status_bar)
        self._asset_count_label = QLabel("0 assets")
        self._thumb_queue_label = QLabel()
        self._status_bar.addPermanentWidget(self._asset_count_label)
        self._status_bar.addPermanentWidget(self._thumb_queue_label)

        # Main stacked area
        self._stack = QStackedWidget()
        self.setCentralWidget(self._stack)

        # Browse mode widget
        browse_widget = QWidget()
        browse_layout = QVBoxLayout(browse_widget)
        browse_layout.setContentsMargins(0, 0, 0, 0)
        browse_layout.setSpacing(0)

        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.setChildrenCollapsible(False)

        # Left panel — tabbed: Browse (FilterPanel) | Config (Roots + Datasets)
        self._left_tabs = QTabWidget()
        self._left_tabs.setTabPosition(QTabWidget.TabPosition.North)
        self._left_tabs.setDocumentMode(True)

        self._filter_panel = FilterPanel()
        self._left_tabs.addTab(self._filter_panel, "Browse")

        config_widget = QWidget()
        config_layout = QVBoxLayout(config_widget)
        config_layout.setContentsMargins(0, 0, 0, 0)
        config_layout.setSpacing(0)
        self._roots_panel = RootsPanel(self._bridge)
        config_layout.addWidget(self._roots_panel, stretch=2)

        worker_header = QLabel("Worker Settings")
        worker_header.setStyleSheet("font-weight: bold; padding: 6px 4px 2px 4px;")
        config_layout.addWidget(worker_header)

        self._backfill_cb = QCheckBox("Auto-compute perceptual hashes")
        self._backfill_cb.setToolTip(
            "On startup, queue background computation of perceptual hashes\n"
            "for any assets that don't have one yet."
        )
        self._batch_size_spin = QSpinBox()
        self._batch_size_spin.setRange(1, 200)
        self._batch_size_spin.setFixedWidth(60)
        self._batch_size_spin.setToolTip(
            "How many assets are processed per background worker job.\n"
            "Smaller batches keep the app more responsive."
        )
        batch_row = QHBoxLayout()
        batch_row.addWidget(QLabel("Assets per batch:"))
        batch_row.addWidget(self._batch_size_spin)
        batch_row.addStretch()

        worker_inner = QWidget()
        worker_inner_layout = QVBoxLayout(worker_inner)
        worker_inner_layout.setContentsMargins(4, 0, 4, 4)
        worker_inner_layout.addWidget(self._backfill_cb)
        worker_inner_layout.addLayout(batch_row)
        config_layout.addWidget(worker_inner)

        self._left_tabs.addTab(config_widget, "Config")

        self._splitter.addWidget(self._left_tabs)

        self._table_model = AssetTableModel(self._bridge, self._thumb_bridge)
        self._table_view = AssetTableView(self._table_model)
        self._splitter.addWidget(self._table_view)

        self._detail_panel = DetailPanel(self._bridge, self._thumb_bridge)
        self._tag_panel = TagManagementPanel()

        self._right_tabs = QTabWidget()
        self._right_tabs.addTab(self._detail_panel, "Image Properties")
        self._right_tabs.addTab(self._tag_panel, "Tag Management")
        self._splitter.addWidget(self._right_tabs)

        # All three panes resize freely; initial proportions are 1:3:1.5.
        self._splitter.setStretchFactor(0, 1)
        self._splitter.setStretchFactor(1, 3)
        self._splitter.setStretchFactor(2, 1)

        browse_layout.addWidget(self._splitter)
        self._stack.addWidget(browse_widget)  # index 0

        # Preview window — created once, shown/hidden as needed.
        self._preview_win = PreviewWindow(self)

        # Query mode widget
        self._query_tab = QueryTab(self._bridge)
        self._stack.addWidget(self._query_tab)   # index 1

    def _connect_signals(self) -> None:
        # Toolbar mode switch
        self._browse_btn.clicked.connect(lambda: self._set_mode(0))
        self._query_btn.clicked.connect(lambda: self._set_mode(1))
        self._preview_btn.clicked.connect(self._toggle_preview)
        self._import_btn.clicked.connect(self._bulk_import)

        # Filter panel
        self._filter_panel.filter_changed.connect(self._apply_filter)
        self._filter_panel.rename_dataset_requested.connect(self._rename_dataset)
        self._filter_panel.delete_dataset_requested.connect(self._delete_dataset)

        # Roots panel
        self._roots_panel.roots_changed.connect(self._on_roots_changed)
        self._roots_panel.filter_changed.connect(self._apply_filter)

        # Table selection → detail panel + preview
        self._table_view.selection_changed.connect(self._on_selection_changed)
        self._table_view.context_menu_requested.connect(self._on_asset_context_menu)
        # Double-click opens the preview window.
        self._table_view.doubleClicked.connect(self._on_table_double_click)
        # Keep toolbar button in sync when the window is closed via Escape/X.
        self._preview_win.visibility_changed.connect(self._preview_btn.setChecked)
        self._preview_win.image_modified.connect(self._on_preview_image_modified)

        # Table count updates status bar
        self._table_model.rowsInserted.connect(self._update_status)
        self._table_model.modelReset.connect(self._update_status)

        # Detail panel actions
        dp = self._detail_panel
        dp.rename_requested.connect(self._rename_asset)
        dp.move_requested.connect(self._move_asset)
        dp.delete_requested.connect(self._delete_asset)
        dp.merge_requested.connect(self._merge_into)
        dp.tag_added.connect(self._add_tag)
        dp.tag_removed.connect(self._remove_tag)
        dp.caption_saved.connect(self._save_caption)
        dp.caption_deleted.connect(self._delete_caption)
        dp.tags_validated_changed.connect(self._set_tags_validated)
        dp.caption_validated_changed.connect(self._set_caption_validated)
        dp.batch_tag_requested.connect(self._batch_add_tag)
        dp.batch_remove_tag_requested.connect(self._batch_remove_tag)
        dp.batch_replace_tag_requested.connect(self._batch_replace_tag)
        dp.batch_move_requested.connect(self._batch_move)
        dp.batch_delete_requested.connect(self._batch_delete)
        dp.add_to_dataset_requested.connect(self._add_to_dataset)
        dp.batch_add_to_dataset_requested.connect(self._batch_add_to_dataset)
        dp.remove_from_dataset_requested.connect(self._remove_from_dataset)
        dp.tag_filter_requested.connect(self._add_tag_filter)
        dp.batch_remove_from_dataset_requested.connect(self._batch_remove_from_dataset)
        dp.merge_two_requested.connect(self._merge_two)
        dp.import_from_caption_requested.connect(self._import_from_caption)

        # Tag management panel
        tp = self._tag_panel
        tp.replace_tag_requested.connect(self._tm_replace_tag)
        tp.delete_tag_requested.connect(self._tm_delete_tag)
        tp.change_type_requested.connect(self._tm_change_type)
        tp.add_to_selection_requested.connect(self._tm_add_to_selection)
        tp.add_to_filtered_requested.connect(self._tm_add_to_filtered)
        tp.remove_from_selection_requested.connect(self._tm_remove_from_selection)
        tp.remove_from_filtered_requested.connect(self._tm_remove_from_filtered)
        tp.add_as_filter_requested.connect(self._on_add_as_filter)
        self._right_tabs.currentChanged.connect(self._on_right_tab_changed)

        # Preview window signals → DB updates
        self._preview_win.mask_saved.connect(self._on_mask_saved)
        self._preview_win.perceptual_hash_ready.connect(self._on_perceptual_hash_ready)

        # Worker settings changes → persist immediately
        self._backfill_cb.toggled.connect(lambda _: self._save_settings())
        self._batch_size_spin.valueChanged.connect(lambda _: self._save_settings())

        # Query tab — save result as dataset
        self._query_tab.save_as_dataset_requested.connect(self._save_as_dataset)

    # -----------------------------------------------------------------------
    # Federation open / refresh
    # -----------------------------------------------------------------------

    def _open_federation(self) -> None:
        """
        Read the current federation from the worker (it was opened at
        start-up) and hydrate the panels. All further writes go through
        QtDBBridge so the worker thread's copy stays authoritative.
        """
        def get_fed(fed: federation.Federation) -> federation.Federation:
            return fed

        def on_ready(fed: federation.Federation) -> None:
            self._fed = fed
            self._roots_panel.load_roots(fed)
            self._apply_filter()
            self._refresh_datasets()
            self._refresh_tag_suggestions()
            self._repair_has_mask()
            self._start_phash_backfill()

        def on_error(exc: BaseException) -> None:
            self._status_bar.showMessage(f"Failed to open federation: {exc}", 0)

        self._bridge.submit(get_fed, on_result=on_ready, on_error=on_error)

    def _on_roots_changed(self) -> None:
        """Called after attach/detach; re-opens the federation inside the worker."""
        cfg = self._config_path

        def reopen(fed: federation.Federation) -> federation.Federation:
            fed.close()
            new_fed = federation.open_federation(cfg)
            self._worker.set_federation(new_fed)
            return new_fed

        def on_ready(fed: federation.Federation) -> None:
            self._fed = fed
            self._roots_panel.load_roots(fed)
            self._apply_filter()
            self._refresh_datasets()
            self._refresh_tag_suggestions()
            self._repair_has_mask()
            self._start_phash_backfill()

        self._bridge.submit(reopen, on_result=on_ready)

    # -----------------------------------------------------------------------
    # Filter
    # -----------------------------------------------------------------------

    def _apply_filter(self, _=None) -> None:
        self._table_model.refresh(
            checked_labels=self._roots_panel.checked_labels(),
            filter_rules=self._filter_panel.current_filter_rules(),
            sort_rules=self._filter_panel.current_sort_rules(),
        )
        # Keep the detail panel's active-dataset context in sync with the
        # first checked dataset (used for "Remove from [X]" button).
        checked_ds = self._filter_panel.checked_dataset_names()
        self._detail_panel.set_active_dataset(checked_ds[0] if checked_ds else None)
        if self._right_tabs.currentIndex() == 1:
            self._refresh_tag_list()

    def _apply_filter_preserving_scroll(self) -> None:
        """Like _apply_filter, but restores the viewport position after the
        model resets.  Use this for deletions and dataset selection changes."""
        sb = self._table_view.verticalScrollBar()
        saved = sb.value()
        self._apply_filter()
        if saved <= 0:
            return
        fired = [False]
        def _restore(_) -> None:
            if fired[0]:
                return
            fired[0] = True
            self._table_model.selection_hint.disconnect(_restore)
            # Defer past Qt's post-endResetModel layout pass, which would
            # otherwise reset the scrollbar to 0 after we set it.
            QTimer.singleShot(0, lambda: sb.setValue(min(saved, sb.maximum())))
        self._table_model.selection_hint.connect(_restore)

    def _auto_select_after_refresh(self, target_row: int) -> None:
        """Select min(target_row, count-1) after the next model reset completes."""
        if target_row < 0:
            return
        fired = [False]
        def _do_select(count: int) -> None:
            if fired[0]:
                return
            fired[0] = True
            self._table_model.selection_hint.disconnect(_do_select)
            row = min(target_row, count - 1)
            if row < 0:
                return
            idx = self._table_model.index(row, 0)
            self._table_view.selectionModel().setCurrentIndex(
                idx,
                QItemSelectionModel.SelectionFlag.ClearAndSelect |
                QItemSelectionModel.SelectionFlag.Rows,
            )
        self._table_model.selection_hint.connect(_do_select)

    # -----------------------------------------------------------------------
    # Mode switching
    # -----------------------------------------------------------------------

    def _set_mode(self, index: int) -> None:
        self._stack.setCurrentIndex(index)
        self._browse_btn.setChecked(index == 0)
        self._query_btn.setChecked(index == 1)

    # -----------------------------------------------------------------------
    # Selection
    # -----------------------------------------------------------------------

    def _on_selection_changed(self, assets: list[federation.AssetRow]) -> None:
        self._selected_assets = assets
        self._detail_panel.load_selection(assets, self._fed)
        if self._preview_win.isVisible() and assets:
            self._load_preview(assets[0])

    def _on_asset_context_menu(self, menu, assets: list[federation.AssetRow]) -> None:
        """
        Extend the right-click menu with row-level actions.
        The view has already populated copy actions; add a separator then
        anything that requires federation access (merge, delete, datasets, …).

        Pattern:
            if menu.actions():
                menu.addSeparator()
            act = menu.addAction("Some Action")
            act.triggered.connect(lambda: self._some_method(assets))
        """
        if not assets or not menu:
            return

    def _on_table_double_click(self, index) -> None:
        asset = self._table_model.row_data(index.row())
        if asset is None:
            return
        self._load_preview(asset)
        if not self._preview_win.isVisible():
            self._preview_win.show()
            self._preview_btn.setChecked(True)

    def _on_preview_image_modified(self, abs_path: str) -> None:
        """Delete stale thumbnail files + evict all caches, then reload both panes."""
        def op(fed: federation.Federation) -> Optional[tuple[str, str, str]]:
            for shard in fed.shards.values():
                try:
                    rel = os.path.relpath(abs_path, shard.abs_path).replace(os.sep, "/")
                except ValueError:
                    continue
                if rel.startswith("../"):
                    continue
                row = shard.conn.execute(
                    "SELECT asset_id, file_hash FROM assets WHERE rel_path = ?",
                    (rel,),
                ).fetchone()
                if row:
                    lq = imgdb_thumbs.thumb_path(
                        shard.abs_path, row["asset_id"], row["file_hash"]
                    )
                    hq = imgdb_thumbs.thumb_path_hq(
                        shard.abs_path, row["asset_id"], row["file_hash"]
                    )
                    for path in (lq, hq):
                        try:
                            os.remove(path)
                        except FileNotFoundError:
                            pass
                    return row["asset_id"], lq, hq
            return None

        def on_done(result: Optional[tuple[str, str, str]]) -> None:
            if result:
                asset_id, lq, hq = result
                # Evict from pixmap LRU so the next request regenerates.
                self._thumb_bridge.invalidate_thumb(lq)
                self._thumb_bridge.invalidate_thumb(hq)
                # Clear model's dest/requested tracking so _get_or_request_thumb
                # re-submits rather than returning None indefinitely.
                self._table_model.invalidate_asset_thumb(asset_id)
            # Reload detail panel (triggers HQ re-request at SELECTED priority).
            self._detail_panel.load_selection(self._selected_assets, self._fed)
            # Repaint visible table rows — now that tracking is cleared,
            # _get_or_request_thumb will submit fresh LQ requests.
            self._table_view.viewport().update()

        self._bridge.submit(op, on_result=on_done, on_error=self._show_error)

    def _load_preview(self, asset: federation.AssetRow) -> None:
        if self._fed is None:
            return
        shard = self._fed.shards.get(asset.root)
        if shard is None:
            return
        abs_path = os.path.join(shard.abs_path, asset.rel_path)
        self._preview_win.set_image(
            abs_path, asset.rel_path,
            needs_phash=asset.perceptual_hash is None,
        )

    def _toggle_preview(self, checked: bool) -> None:
        if checked:
            # Load whatever is currently selected.
            selected = [
                self._table_model.row_data(idx.row())
                for idx in self._table_view.selectionModel().selectedRows()
            ]
            selected = [a for a in selected if a is not None]
            if selected:
                self._load_preview(selected[0])
            self._preview_win.show()
            self._preview_win.raise_()
        else:
            self._preview_win.hide()

    # -----------------------------------------------------------------------
    # Status bar
    # -----------------------------------------------------------------------

    def _update_status(self) -> None:
        n = self._table_model.rowCount()
        self._asset_count_label.setText(f"{n:,} assets")
        q = self._thumb_worker.queue_depth()
        if q:
            self._thumb_queue_label.setText(f"thumbs: {q}")
        else:
            self._thumb_queue_label.setText("")

    # -----------------------------------------------------------------------
    # Asset actions (wired from detail panel)
    # -----------------------------------------------------------------------

    def _add_tag(self, asset_id: str, tag_name: str, type_name: str) -> None:
        def op(fed: federation.Federation) -> None:
            federation.add_tags(fed, asset_id, [tag_name], type_name=type_name)

        self._bridge.submit(op, on_result=lambda _: self._refresh_tag_suggestions(), on_error=self._show_error)

    def _remove_tag(self, asset_id: str, tag_id: str) -> None:
        def op(fed: federation.Federation) -> None:
            federation.remove_tags(fed, asset_id, [tag_id])

        self._bridge.submit(op, on_result=lambda _: self._refresh_tag_suggestions(), on_error=self._show_error)

    def _save_caption(self, asset_id: str, kind: str, content: str) -> None:
        def op(fed: federation.Federation) -> None:
            federation.set_caption(fed, asset_id, kind, content)

        self._bridge.submit(op, on_error=self._show_error)

    def _delete_caption(self, asset_id: str, kind: str) -> None:
        def op(fed: federation.Federation) -> None:
            federation.delete_caption(fed, asset_id, kind)

        self._bridge.submit(op, on_error=self._show_error)

    def _set_tags_validated(self, asset_id: str, validated: bool) -> None:
        def op(fed: federation.Federation) -> None:
            shard = federation.shard_for_asset(fed, asset_id)
            imgdb.set_tags_validated(shard.conn, asset_id, validated)

        self._bridge.submit(op, on_error=self._show_error)

    def _on_mask_saved(self, abs_path: str, has_mask: bool) -> None:
        def op(fed: federation.Federation) -> None:
            for shard in fed.shards.values():
                try:
                    rel = os.path.relpath(abs_path, shard.abs_path).replace(os.sep, "/")
                except ValueError:
                    continue
                if rel.startswith("../"):
                    continue
                row = shard.conn.execute(
                    "SELECT asset_id FROM assets WHERE rel_path = ?", (rel,)
                ).fetchone()
                if row:
                    imgdb.set_has_mask(shard.conn, row["asset_id"], has_mask)
                    return

        self._bridge.submit(op, on_error=self._show_error)

    def _on_perceptual_hash_ready(self, abs_path: str, phash: str) -> None:
        """Write a perceptual hash computed by the preview window into the DB."""
        def op(fed: federation.Federation) -> None:
            for shard in fed.shards.values():
                try:
                    rel = os.path.relpath(abs_path, shard.abs_path).replace(os.sep, "/")
                except ValueError:
                    continue
                if rel.startswith("../"):
                    continue
                row = shard.conn.execute(
                    "SELECT asset_id FROM assets WHERE rel_path = ?", (rel,)
                ).fetchone()
                if row:
                    imgdb.set_perceptual_hash(shard.conn, row["asset_id"], phash)
                    return

        self._bridge.submit(op, on_error=self._show_error)

    def _repair_has_mask(self) -> None:
        """Fix assets where has_mask=0 but a mask file exists on disk.

        Runs once at startup to repair records predating the has_mask column.
        Recurses until no more repairs are found so large libraries are handled
        without blocking the worker.
        """
        def op(fed: federation.Federation) -> int:
            return federation.repair_missing_has_mask(fed)

        def on_result(repaired: int) -> None:
            if repaired > 0:
                self._apply_filter()
                self._repair_has_mask()

        self._bridge.submit(op, on_result=on_result, on_error=self._show_error)

    def _start_phash_backfill(self) -> None:
        """Entry point: query total missing count, then start the batch loop."""
        if not self._backfill_cb.isChecked():
            return

        def count_op(fed: federation.Federation) -> int:
            return federation.count_assets_missing_perceptual_hash(fed)

        def on_count(total: int) -> None:
            if total == 0:
                return
            self._backfill_done = 0
            self._backfill_total = total
            self._run_phash_batch()

        self._bridge.submit(count_op, on_result=on_count, on_error=self._show_error)

    def _run_phash_batch(self) -> None:
        """Process one batch of perceptual hashes; reschedules itself until done."""
        batch_size = self._batch_size_spin.value()
        # Capture filter state on the main thread before handing off to worker.
        priority_labels = self._roots_panel.checked_labels()
        priority_rules = self._filter_panel.current_filter_rules()

        def op(fed: federation.Federation) -> tuple[int, bool]:
            missing = federation.list_assets_missing_perceptual_hash(
                fed, batch_size,
                priority_labels=priority_labels,
                priority_rules=priority_rules,
            )
            for asset_id, rel_path, label in missing:
                shard = fed.shards.get(label)
                if shard is None:
                    continue
                abs_path = os.path.join(shard.abs_path, rel_path)
                phash = imgdb.compute_perceptual_hash(abs_path)
                # Write PHASH_FAILED sentinel (not NULL) when hashing fails so
                # the file is excluded from future backfill batches. NULL means
                # "not yet attempted" and would re-queue the same file forever.
                imgdb.set_perceptual_hash(
                    shard.conn, asset_id, phash if phash is not None else imgdb.PHASH_FAILED
                )
            return len(missing), len(missing) < batch_size

        def on_result(result: tuple[int, bool]) -> None:
            n, complete = result
            self._backfill_done = getattr(self, "_backfill_done", 0) + n
            done = self._backfill_done
            grand = getattr(self, "_backfill_total", done)
            if not complete and self._bridge.is_running:
                self._status_bar.showMessage(
                    f"Computing perceptual hashes… ({done:,} of {grand:,})"
                )
                self._run_phash_batch()
            else:
                self._backfill_done = 0
                self._backfill_total = 0
                if done > 0:
                    self._status_bar.showMessage(
                        f"Perceptual hash backfill complete ({done:,} computed)", 5000
                    )

        self._bridge.submit(op, on_result=on_result, on_error=self._show_error)

    def _set_caption_validated(self, asset_id: str, kind: str, validated: bool) -> None:
        def op(fed: federation.Federation) -> None:
            shard = federation.shard_for_asset(fed, asset_id)
            imgdb.set_caption_validated(shard.conn, asset_id, kind, validated)

        self._bridge.submit(op, on_error=self._show_error)

    def _import_from_caption(self, asset_id: str) -> None:
        checked = self._roots_panel.checked_labels()
        rules = self._filter_panel.current_filter_rules()

        def fetch(fed: federation.Federation) -> tuple:
            tag_lookup = federation.build_tag_lookup(fed)
            shard = federation.shard_for_asset(fed, asset_id)
            cap_rows = shard.conn.execute(
                "SELECT kind, content FROM captions WHERE asset_id = ? ORDER BY kind",
                (asset_id,),
            ).fetchall()
            caption_texts = {r["kind"]: (r["content"] or "") for r in cap_rows}
            all_kinds = federation.list_all_caption_kinds(fed, checked)
            filtered_count = federation.count_filtered_assets(fed, checked, rules)
            total_count = federation.count_filtered_assets(fed, checked, [])
            return tag_lookup, caption_texts, all_kinds, filtered_count, total_count

        def on_fetch(data: tuple) -> None:
            tag_lookup, caption_texts, all_kinds, filtered_count, total_count = data
            if not caption_texts:
                QMessageBox.information(
                    self, "No captions", "This asset has no captions to import from."
                )
                return

            match_func = lambda text: federation.match_tags_in_text(text, tag_lookup)
            dlg = ImportFromCaptionDialog(
                caption_texts=caption_texts,
                all_caption_kinds=all_kinds,
                match_func=match_func,
                filtered_count=filtered_count,
                total_count=total_count,
                parent=self,
            )
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return

            scope = dlg.scope()
            caption_kind = dlg.caption_kind()
            ambiguous_policy = dlg.ambiguous_policy()

            if scope == "single":
                selected = dlg.selected_tags()
                if not selected:
                    return

                def do_single(fed: federation.Federation) -> int:
                    count = 0
                    for name, type_name in selected:
                        federation.add_tags(fed, asset_id, [name], type_name=type_name)
                        count += 1
                    return count

                def on_single_done(_: int) -> None:
                    self._refresh_tag_suggestions()
                    self._detail_panel.load_selection(self._selected_assets, self._fed)
                    if self._right_tabs.currentIndex() == 1:
                        self._refresh_tag_list()

                self._bridge.submit(do_single, on_result=on_single_done, on_error=self._show_error)
                return

            # Bulk scope — build kwargs shared by prescan and import.
            if scope == "filtered":
                bulk_kwargs: dict = dict(
                    caption_kind=caption_kind,
                    tag_lookup=tag_lookup,
                    checked_labels=checked,
                    filter_rules=rules,
                )
            else:  # "all" — all assets in checked shards, no other filter
                bulk_kwargs = dict(
                    caption_kind=caption_kind,
                    tag_lookup=tag_lookup,
                    checked_labels=checked,
                )

            def _do_bulk_import(resolution: dict) -> None:
                def do_bulk(fed: federation.Federation) -> int:
                    return federation.bulk_import_caption_tags(
                        fed, resolution=resolution, **bulk_kwargs
                    )

                def on_bulk_done(count: int) -> None:
                    self._refresh_tag_suggestions()
                    self._detail_panel.load_selection(self._selected_assets, self._fed)
                    if self._right_tabs.currentIndex() == 1:
                        self._refresh_tag_list()
                    noun = "assignment" if count == 1 else "assignments"
                    QMessageBox.information(
                        self, "Import complete",
                        f"Added {count} tag {noun} across matching images."
                    )

                self._bridge.submit(do_bulk, on_result=on_bulk_done, on_error=self._show_error)

            if ambiguous_policy == "ask":
                def do_prescan(fed: federation.Federation) -> list:
                    return federation.prescan_ambiguous_matches(fed, **bulk_kwargs)

                def on_prescan(ambiguous: list) -> None:
                    resolution: dict = {}
                    if ambiguous:
                        res_dlg = AmbiguousTagResolutionDialog(ambiguous, self)
                        if res_dlg.exec() != QDialog.DialogCode.Accepted:
                            return
                        resolution = res_dlg.resolution()
                    _do_bulk_import(resolution)

                self._bridge.submit(do_prescan, on_result=on_prescan, on_error=self._show_error)
            else:
                _do_bulk_import({})

        self._bridge.submit(fetch, on_result=on_fetch, on_error=self._show_error)

    def _rename_asset(self, asset_id: str, current_rel_path: str) -> None:
        dlg = RenameDialog(current_rel_path, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        new_rel_path = dlg.new_rel_path()

        def op(fed: federation.Federation) -> None:
            federation.rename_asset(fed, asset_id, new_rel_path)

        def on_done(_) -> None:
            self._apply_filter()

        self._bridge.submit(op, on_result=on_done, on_error=self._show_error)

    def _move_asset(self, asset_id: str, new_rel_path: str) -> None:
        force = False
        if self._fed is not None:
            label = self._fed.asset_index.get(asset_id)
            shard = self._fed.shards.get(label) if label else None
            if shard is not None:
                new_abs = os.path.join(shard.abs_path, new_rel_path.replace("/", os.sep))
                if os.path.exists(new_abs):
                    btn = QMessageBox.question(
                        self, "⚠ File already exists",
                        f"A file already exists at:\n{new_rel_path}\n\nOverwrite it?",
                    )
                    if btn != QMessageBox.StandardButton.Yes:
                        return
                    force = True

        def op(fed: federation.Federation) -> None:
            federation.rename_asset(fed, asset_id, new_rel_path, force=force)

        def on_done(_) -> None:
            self._apply_filter()

        self._bridge.submit(op, on_result=on_done, on_error=self._show_error)

    def _delete_asset(self, asset_id: str, rel_path: str) -> None:
        dlg = ConfirmDeleteDialog([rel_path], self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        row_hint = self._table_view.currentIndex().row()

        def op(fed: federation.Federation) -> None:
            federation.delete_asset(fed, asset_id)

        def on_done(_) -> None:
            self._apply_filter_preserving_scroll()
            self._auto_select_after_refresh(row_hint)

        self._bridge.submit(op, on_result=on_done, on_error=self._show_error)

    def _merge_into(self, this_asset_id: str) -> None:
        """
        Called when the detail panel's 'Merge into…' is clicked for one asset.
        Requires a second asset; prompt the user to paste its asset_id via an
        input dialog rather than opening the full MergeDialog (which expects
        both paths, not known here without a DB lookup).
        """
        other_id, ok = QInputDialog.getText(
            self, "Merge into…",
            "Enter the asset_id of the target (survivor) asset:",
        )
        if not ok or not other_id.strip():
            return
        other_id = other_id.strip()
        # We need rel_paths for the dialog labels; fetch them.
        asset_id_a = this_asset_id
        asset_id_b = other_id

        def fetch(fed: federation.Federation):
            def _path(aid):
                shard = federation.shard_for_asset(fed, aid)
                row = shard.conn.execute(
                    "SELECT rel_path FROM assets WHERE asset_id = ?", (aid,)
                ).fetchone()
                return row[0] if row else aid
            return _path(asset_id_a), _path(asset_id_b)

        def on_ready(paths) -> None:
            path_a, path_b = paths
            dlg = MergeDialog(path_a, asset_id_a, path_b, asset_id_b, self)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            self._do_merge(dlg.survivor_id(), dlg.merged_id(), dlg.delete_duplicate())

        self._bridge.submit(fetch, on_result=on_ready, on_error=self._show_error)

    def _merge_two(self, assets: list[federation.AssetRow]) -> None:
        """Called when exactly 2 same-shard assets are selected and Merge clicked."""
        if len(assets) != 2:
            return
        a, b = assets
        dlg = MergeDialog(a.rel_path, a.asset_id, b.rel_path, b.asset_id, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._do_merge(dlg.survivor_id(), dlg.merged_id(), dlg.delete_duplicate())

    def _do_merge(self, survivor_id: str, merged_id: str, delete_file: bool = False) -> None:
        def op(fed: federation.Federation) -> Optional[str]:
            merged_abs = None
            if delete_file:
                try:
                    shard = federation.shard_for_asset(fed, merged_id)
                    row = shard.conn.execute(
                        "SELECT rel_path FROM assets WHERE asset_id = ?", (merged_id,)
                    ).fetchone()
                    merged_abs = os.path.join(shard.abs_path, row[0]) if row else None
                except Exception:
                    pass
            federation.merge_assets(fed, survivor_id, merged_id)
            return merged_abs

        def on_done(merged_abs: Optional[str]) -> None:
            self._apply_filter()
            if merged_abs and os.path.exists(merged_abs):
                try:
                    os.remove(merged_abs)
                except OSError as e:
                    self._show_error(e)

        self._bridge.submit(op, on_result=on_done, on_error=self._show_error)

    def _batch_add_tag(self, asset_ids: list[str]) -> None:
        dlg = BatchTagDialog(len(asset_ids), self)
        dlg.set_suggestions(self._tag_suggestions)
        dlg.set_type_suggestions(self._tag_types)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        tags = dlg.tags()
        if not tags:
            return
        type_name = dlg.type_name()

        def op(fed: federation.Federation) -> None:
            for tag in tags:
                federation.add_tag_to_asset_ids(fed, asset_ids, tag, type_name)

        self._bridge.submit(op, on_error=self._show_error)

    def _batch_remove_tag(self, assets: list[federation.AssetRow]) -> None:
        dlg = BatchRemoveTagDialog(len(assets), self._tag_suggestions, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        tag = dlg.tag()

        def op(fed: federation.Federation) -> None:
            for asset in assets:
                federation.remove_tags_by_name(fed, asset.asset_id, tag)

        self._bridge.submit(op, on_error=self._show_error)

    def _batch_replace_tag(self, assets: list[federation.AssetRow]) -> None:
        dlg = BatchReplaceTagDialog(len(assets), self._tag_suggestions, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        old_tag, new_tag = dlg.old_tag(), dlg.new_tag()

        def op(fed: federation.Federation) -> None:
            for asset in assets:
                federation.remove_tags_by_name(fed, asset.asset_id, old_tag)
                federation.add_tags(fed, asset.asset_id, [new_tag])

        self._bridge.submit(op, on_error=self._show_error)

    def _batch_move(self, assets: list[federation.AssetRow]) -> None:
        if not assets or self._fed is None:
            return
        root_label = assets[0].root
        shard = self._fed.shards.get(root_label)
        if shard is None:
            self._show_error(Exception(f"Shard '{root_label}' is unavailable."))
            return

        dlg = BatchMoveDialog(len(assets), shard.abs_path, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        dest_dir = dlg.dest_dir()

        # Pre-compute moves and detect filesystem conflicts.
        planned: list[tuple[str, str, bool]] = []  # (asset_id, new_rel, force)
        conflicts: list[tuple[str, str]] = []       # (asset_id, new_rel)
        for asset in assets:
            filename = os.path.basename(asset.rel_path)
            new_rel = (dest_dir + "/" + filename) if dest_dir else filename
            new_abs = os.path.join(shard.abs_path, new_rel.replace("/", os.sep))
            if new_rel != asset.rel_path and os.path.exists(new_abs):
                conflicts.append((asset.asset_id, new_rel))
            else:
                planned.append((asset.asset_id, new_rel, False))

        if conflicts:
            conflict_list = "\n".join(r for _, r in conflicts[:10])
            if len(conflicts) > 10:
                conflict_list += f"\n… and {len(conflicts) - 10} more"
            msg = QMessageBox(self)
            msg.setWindowTitle("Files already exist")
            msg.setText(
                f"{len(conflicts)} file(s) already exist at the destination:\n\n"
                f"{conflict_list}\n\nWhat would you like to do?"
            )
            overwrite_btn = msg.addButton("Overwrite", QMessageBox.ButtonRole.AcceptRole)
            skip_btn = msg.addButton("Skip these files", QMessageBox.ButtonRole.RejectRole)
            msg.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
            msg.exec()
            clicked = msg.clickedButton()
            if clicked is overwrite_btn:
                for asset_id, new_rel in conflicts:
                    planned.append((asset_id, new_rel, True))
            elif clicked is skip_btn:
                pass  # conflicts are simply omitted
            else:
                return  # cancel or window closed

        if not planned:
            return

        def op(fed: federation.Federation) -> None:
            for asset_id, new_rel, force in planned:
                federation.rename_asset(fed, asset_id, new_rel, force=force)

        def on_done(_) -> None:
            self._apply_filter()

        self._bridge.submit(op, on_result=on_done, on_error=self._show_error)

    def _batch_delete(self, assets: list[federation.AssetRow]) -> None:
        paths = [a.rel_path for a in assets]
        dlg = ConfirmDeleteDialog(paths, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        def op(fed: federation.Federation) -> None:
            for asset in assets:
                federation.delete_asset(fed, asset.asset_id)

        def on_done(_) -> None:
            self._apply_filter_preserving_scroll()

        self._bridge.submit(op, on_result=on_done, on_error=self._show_error)

    def _bulk_import(self) -> None:
        if self._fed is None:
            QMessageBox.warning(self, "No federation", "Open a federation first.")
            return
        root_labels = list(self._fed.shards.keys())
        if not root_labels:
            QMessageBox.warning(self, "No roots", "Attach at least one root before importing.")
            return

        dlg = BulkImportDialog(root_labels, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        source_dir = dlg.source_dir()
        shard_label = dlg.shard_label()
        file_mode = dlg.file_mode()
        dest_subdir = dlg.dest_subdir()
        caption_kind = dlg.caption_kind()
        overwrite = dlg.overwrite()

        self._status_bar.showMessage(f"Importing from {source_dir}…", 0)
        self._import_btn.setEnabled(False)

        def on_event(kind: str, rel_path: str) -> None:
            short = rel_path if len(rel_path) <= 60 else "…" + rel_path[-59:]
            self._status_bar.showMessage(f"[{kind}] {short}", 0)

        def on_result(summary) -> None:
            self._import_btn.setEnabled(True)
            self._apply_filter()
            parts = [f"{summary.processed} imported"]
            if summary.copied:
                parts.append(f"{summary.copied} files {'copied' if file_mode == 'copy' else 'moved'}")
            if summary.skipped:
                parts.append(f"{summary.skipped} skipped")
            if summary.no_txt:
                parts.append(f"{summary.no_txt} had no .txt")
            if summary.not_registered:
                parts.append(f"{summary.not_registered} not in DB")
            msg = "Import complete: " + ", ".join(parts) + "."
            if summary.errors:
                msg += f"\n\n{len(summary.errors)} error(s):\n" + "\n".join(summary.errors[:10])
                if len(summary.errors) > 10:
                    msg += f"\n… and {len(summary.errors) - 10} more"
                QMessageBox.warning(self, "Import finished with errors", msg)
            else:
                self._status_bar.showMessage(msg, 8000)

        def on_error(exc: BaseException) -> None:
            self._import_btn.setEnabled(True)
            self._show_error(exc)

        self._bridge.submit(
            federation.bulk_import_paired_files,
            source_dir,
            shard_label,
            caption_kind,
            overwrite,
            file_mode,
            dest_subdir,
            on_event=on_event,
            on_result=on_result,
            on_error=on_error,
        )

    # -----------------------------------------------------------------------
    # Dataset actions
    # -----------------------------------------------------------------------

    def _refresh_tag_suggestions(self) -> None:
        def op(fed: federation.Federation) -> tuple:
            return (
                federation.list_all_tags_with_counts(fed),
                federation.list_all_tag_types_federation(fed),
            )

        def on_ready(result: tuple) -> None:
            tags, types = result
            self._tag_suggestions = tags
            self._tag_types = types
            self._detail_panel.set_tag_suggestions(tags)
            self._filter_panel.set_tag_names([n for n, *_ in tags])

        self._bridge.submit(op, on_result=on_ready)

    def _refresh_tag_list(self) -> None:
        """Populate the Tag Management panel with tags matching the current filter."""
        labels = self._roots_panel.checked_labels()
        rules = self._filter_panel.current_filter_rules()

        def op(fed: federation.Federation) -> list[tuple[str, str, int]]:
            return federation.list_tags_for_filtered_assets(fed, labels, rules)

        self._bridge.submit(op, on_result=self._tag_panel.load_tags)

    def _on_right_tab_changed(self, index: int) -> None:
        if index == 1:
            self._refresh_tag_list()

    # -----------------------------------------------------------------------
    # Tag management panel handlers
    # -----------------------------------------------------------------------

    def _tm_replace_tag(self, tag_name: str, type_name: str) -> None:
        dlg = ReplaceTagDialog(tag_name, type_name, self._tag_types, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        new_name, new_type = dlg.new_name(), dlg.new_type_name()

        def op(fed: federation.Federation) -> None:
            federation.replace_tag_globally(fed, tag_name, type_name, new_name, new_type)

        def on_done(_) -> None:
            self._refresh_tag_suggestions()
            self._refresh_tag_list()
            self._detail_panel.load_selection(self._selected_assets, self._fed)

        self._bridge.submit(op, on_result=on_done, on_error=self._show_error)

    def _tm_delete_tag(self, tag_name: str, type_name: str) -> None:
        ans = QMessageBox.question(
            self,
            "⚠ Delete Tag",
            f"Delete tag \"{tag_name}\" (category: {type_name}) from all assets?\n\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return

        def op(fed: federation.Federation) -> None:
            federation.delete_tag_globally(fed, tag_name, type_name)

        def on_done(_) -> None:
            self._refresh_tag_suggestions()
            self._refresh_tag_list()

        self._bridge.submit(op, on_result=on_done, on_error=self._show_error)

    def _tm_change_type(self, tag_name: str, type_name: str) -> None:
        dlg = ChangeTagTypeDialog(tag_name, type_name, self._tag_types, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        new_type = dlg.new_type_name()
        if new_type == type_name:
            return

        def op(fed: federation.Federation) -> None:
            federation.replace_tag_globally(fed, tag_name, type_name, tag_name, new_type)

        def on_done(_) -> None:
            self._refresh_tag_suggestions()
            self._refresh_tag_list()

        self._bridge.submit(op, on_result=on_done, on_error=self._show_error)

    def _tm_add_to_selection(self, tag_name: str, type_name: str) -> None:
        if not self._selected_assets:
            return
        asset_ids = [a.asset_id for a in self._selected_assets]

        def op(fed: federation.Federation) -> None:
            federation.add_tag_to_asset_ids(fed, asset_ids, tag_name, type_name)

        def on_done(_) -> None:
            self._refresh_tag_suggestions()
            self._refresh_tag_list()

        self._bridge.submit(op, on_result=on_done, on_error=self._show_error)

    def _tm_add_to_filtered(self, tag_name: str, type_name: str) -> None:
        btn = QMessageBox.question(
            self,
            "⚠ Add tag to filtered assets",
            f"Add tag \"{tag_name}\" ({type_name}) to all currently filtered assets?",
        )
        if btn != QMessageBox.StandardButton.Yes:
            return
        labels = self._roots_panel.checked_labels()
        rules = self._filter_panel.current_filter_rules()

        def op(fed: federation.Federation) -> None:
            federation.add_tag_to_filtered_assets(fed, tag_name, type_name, labels, rules)

        def on_done(_) -> None:
            self._refresh_tag_suggestions()
            self._refresh_tag_list()

        self._bridge.submit(op, on_result=on_done, on_error=self._show_error)

    def _tm_remove_from_selection(self, tag_name: str, type_name: str) -> None:
        if not self._selected_assets:
            return
        asset_ids = [a.asset_id for a in self._selected_assets]

        def op(fed: federation.Federation) -> None:
            federation.remove_tag_from_asset_ids(fed, asset_ids, tag_name, type_name)

        def on_done(_) -> None:
            self._refresh_tag_suggestions()
            self._refresh_tag_list()

        self._bridge.submit(op, on_result=on_done, on_error=self._show_error)

    def _tm_remove_from_filtered(self, tag_name: str, type_name: str) -> None:
        labels = self._roots_panel.checked_labels()
        rules = self._filter_panel.current_filter_rules()

        def op(fed: federation.Federation) -> None:
            federation.remove_tag_from_filtered_assets(fed, tag_name, type_name, labels, rules)

        def on_done(_) -> None:
            self._refresh_tag_suggestions()
            self._refresh_tag_list()

        self._bridge.submit(op, on_result=on_done, on_error=self._show_error)

    def _refresh_datasets(self) -> None:
        def op(fed: federation.Federation) -> list[federation.DatasetInfo]:
            return federation.list_datasets_federation(fed)

        def on_ready(datasets: list[federation.DatasetInfo]) -> None:
            self._filter_panel.set_datasets([ds.name for ds in datasets])

        self._bridge.submit(op, on_result=on_ready)

    def _add_tag_filter(self, tag: str) -> None:
        """Add a 'tag has <name>' filter rule to the browse panel."""
        self._filter_panel.add_filter_rule(FilterRule("tag", "has", tag))
        self._left_tabs.setCurrentIndex(0)  # switch to Browse tab

    def _on_add_as_filter(self, tag_name: str, type_name: str) -> None:
        self._filter_panel.add_filter_rule(FilterRule("tag", "has", tag_name))
        self._left_tabs.setCurrentIndex(0)

    def _add_to_dataset(self, asset_id: str) -> None:
        def fetch(fed: federation.Federation) -> list[str]:
            return [ds.name for ds in federation.list_datasets_federation(fed)]

        def on_names(names: list[str]) -> None:
            dlg = AddToDatasetDialog(names, self)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            dataset_names = dlg.dataset_names()

            def op(fed: federation.Federation) -> None:
                for name in dataset_names:
                    federation.add_to_dataset(fed, name, [asset_id])

            def on_done(_) -> None:
                self._refresh_datasets()
                self._detail_panel.load_selection(self._selected_assets, self._fed)

            self._bridge.submit(op, on_result=on_done, on_error=self._show_error)

        self._bridge.submit(fetch, on_result=on_names, on_error=self._show_error)

    def _batch_add_to_dataset(self, assets: list[federation.AssetRow]) -> None:
        asset_ids = [a.asset_id for a in assets]

        def fetch(fed: federation.Federation) -> list[str]:
            return [ds.name for ds in federation.list_datasets_federation(fed)]

        def on_names(names: list[str]) -> None:
            dlg = AddToDatasetDialog(names, self)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            dataset_names = dlg.dataset_names()

            def op(fed: federation.Federation) -> None:
                for name in dataset_names:
                    federation.add_to_dataset(fed, name, asset_ids)

            self._bridge.submit(op, on_result=lambda _: self._refresh_datasets(), on_error=self._show_error)

        self._bridge.submit(fetch, on_result=on_names, on_error=self._show_error)

    def _rename_dataset(self, old_name: str) -> None:
        new_name, ok = QInputDialog.getText(
            self, "Rename dataset", "New name:", text=old_name
        )
        new_name = new_name.strip()
        if not ok or not new_name or new_name == old_name:
            return

        def op(fed: federation.Federation) -> None:
            federation.rename_dataset(fed, old_name, new_name)

        def on_done(_) -> None:
            self._refresh_datasets()

        self._bridge.submit(op, on_result=on_done, on_error=self._show_error)

    def _remove_from_dataset(self, asset_id: str) -> None:
        checked = self._filter_panel.checked_dataset_names()
        if not checked:
            return
        name = checked[0]

        def op(fed: federation.Federation) -> None:
            federation.remove_from_dataset(fed, name, [asset_id])

        self._bridge.submit(
            op,
            on_result=lambda _: (self._refresh_datasets(), self._apply_filter_preserving_scroll()),
            on_error=self._show_error,
        )

    def _batch_remove_from_dataset(self, assets: list[federation.AssetRow]) -> None:
        checked = self._filter_panel.checked_dataset_names()
        if not checked:
            return
        name = checked[0]
        asset_ids = [a.asset_id for a in assets]

        def op(fed: federation.Federation) -> None:
            federation.remove_from_dataset(fed, name, asset_ids)

        self._bridge.submit(
            op,
            on_result=lambda _: (self._refresh_datasets(), self._apply_filter_preserving_scroll()),
            on_error=self._show_error,
        )

    def _delete_dataset(self, name: str) -> None:
        btn = QMessageBox.question(
            self, "⚠ Delete dataset",
            f"Remove dataset '{name}' from all attached shards?\n\n"
            "This only removes membership records — images are not deleted.\n"
            "Offline shards will still contain the dataset.",
        )
        if btn != QMessageBox.StandardButton.Yes:
            return

        def op(fed: federation.Federation) -> list[str]:
            return federation.delete_dataset(fed, name)

        def on_done(affected: list[str]) -> None:
            self._refresh_datasets()
            self._apply_filter()
            self._status_bar.showMessage(
                f"Deleted dataset '{name}' from {len(affected)} shard(s).", 5000
            )

        self._bridge.submit(op, on_result=on_done, on_error=self._show_error)

    def _save_as_dataset(self, sql: str) -> None:
        def fetch(fed: federation.Federation) -> list[str]:
            return [ds.name for ds in federation.list_datasets_federation(fed)]

        def on_names(names: list[str]) -> None:
            dlg = AddToDatasetDialog(names, self)
            dlg.setWindowTitle("Save query as dataset…")
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            dataset_names = dlg.dataset_names()

            def op(fed: federation.Federation) -> dict[str, int]:
                return {
                    name: federation.add_to_dataset_from_query(fed, name, sql)
                    for name in dataset_names
                }

            def on_done(counts: dict[str, int]) -> None:
                self._refresh_datasets()
                total = sum(counts.values())
                labels = ", ".join(f"'{n}'" for n in counts)
                self._status_bar.showMessage(
                    f"Saved {total} asset(s) into {labels}.", 5000
                )

            self._bridge.submit(op, on_result=on_done, on_error=self._show_error)

        self._bridge.submit(fetch, on_result=on_names, on_error=self._show_error)

    # -----------------------------------------------------------------------
    # Error display
    # -----------------------------------------------------------------------

    def _show_error(self, exc: BaseException) -> None:
        QMessageBox.critical(self, "Error", str(exc))

    # -----------------------------------------------------------------------
    # Shutdown
    # -----------------------------------------------------------------------

    # -----------------------------------------------------------------------
    # Settings persistence
    # -----------------------------------------------------------------------

    def _settings(self) -> QSettings:
        path = os.path.join(os.path.dirname(self._config_path), "imgdb_ui.ini")
        return QSettings(path, QSettings.Format.IniFormat)

    def _save_settings(self) -> None:
        s = self._settings()
        s.setValue("window/geometry", self.saveGeometry())
        s.setValue("splitter/main", self._splitter.saveState())
        s.setValue("table/header", self._table_view.save_header_state())
        s.setValue("tag_panel/header", self._tag_panel.save_header_state())
        s.setValue("worker/backfill_phash", self._backfill_cb.isChecked())
        s.setValue("worker/batch_size", self._batch_size_spin.value())

    def _restore_settings(self) -> None:
        s = self._settings()
        if geom := s.value("window/geometry"):
            self.restoreGeometry(geom)
        if state := s.value("splitter/main"):
            self._splitter.restoreState(state)
        if state := s.value("table/header"):
            self._table_view.restore_header_state(state)
        if state := s.value("tag_panel/header"):
            self._tag_panel.restore_header_state(state)
        backfill = s.value("worker/backfill_phash", defaultValue=True, type=bool)
        self._backfill_cb.setChecked(backfill)
        batch = s.value("worker/batch_size", defaultValue=10, type=int)
        self._batch_size_spin.setValue(batch)

    def closeEvent(self, event) -> None:
        self._save_settings()
        self._poll_timer.stop()
        if self._bridge and self._worker:
            self._worker.shutdown(timeout=10)
        self._thumb_worker.shutdown(timeout=5)
        event.accept()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import signal

    config = DEFAULT_CONFIG
    if len(sys.argv) > 1:
        config = sys.argv[1]

    QApplication.setApplicationName("imgdb")
    app = QApplication.instance() or QApplication(sys.argv)

    _icon_path = os.path.join(os.path.dirname(__file__), "icons", "imgdb_256.png")
    if os.path.exists(_icon_path):
        app.setWindowIcon(QIcon(_icon_path))

    # Make Ctrl+C close the application cleanly instead of looping.
    # Qt's C++ event loop blocks Python signal delivery between events, so we
    # run a no-op timer at 200 ms to give the interpreter a chance to check
    # for pending signals (including SIGINT) on each tick.
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    _signal_pulse = QTimer()
    _signal_pulse.start(200)
    _signal_pulse.timeout.connect(lambda: None)

    win = MainWindow(config_path=config)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
