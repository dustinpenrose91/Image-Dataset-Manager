"""
AssetTableModel + AssetTableView — virtualized asset list backed by
federation.list_filtered_assets / count_filtered_assets.

Threading model:
- Data fetches run on the DBWorker thread via QtDBBridge.
- Thumbnails are requested via QtThumbnailBridge, which delivers decoded
  QPixmaps to the GUI thread via queued signals.
- The model uses a page cache: it materialises rows in slabs of PAGE_SIZE
  on demand. Only pages that are currently visible are actively loaded.
  Unpaged rows return placeholder data until their page arrives.

Thumbnail priority:
- Rows currently visible in the viewport are submitted at PRIORITY_VISIBLE.
- Off-screen rows are submitted at PRIORITY_BACKGROUND.
- The view sends debounced scroll events to the model so it can bump
  in-flight BACKGROUND jobs to VISIBLE without recreating callbacks.
- The detail panel uses PRIORITY_SELECTED (highest) for HQ thumbnails.

Multi-select: the view uses ExtendedSelection. The main window reads
selectedRows() to populate the detail panel.
"""
from __future__ import annotations

import os
from typing import Iterator, Optional

from PySide6.QtCore import (
    QAbstractTableModel, QModelIndex, QSize, Qt, Signal, QTimer,
)
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QHeaderView, QLabel, QMenu,
    QSizePolicy, QTableView, QWidget,
)

import federation
import imgdb
import imgdb_thumbs
from filter_model import FilterRule, SortRule
from imgdb_thumbs_qt import QtThumbnailBridge
from imgdb_worker_qt import QtDBBridge

PAGE_SIZE = 200          # rows loaded per fetch
THUMB_COL_SIZE = 64      # pixels, square

COL_THUMB  = 0
COL_PATH   = 1
COL_ROOT   = 2
COL_DIMS   = 3
COL_FORMAT = 4
COL_SIZE   = 5
COL_PHASH  = 6
COL_ID     = 7
NUM_COLS   = 8

_HEADERS = ["", "Path", "Root", "Dimensions", "Format", "Size", "Perceptual Hash", "Asset ID"]

# Columns that can be toggled via the header context menu (excludes thumbnail).
_TOGGLEABLE_COLS = (COL_PATH, COL_ROOT, COL_DIMS, COL_FORMAT, COL_SIZE, COL_PHASH, COL_ID)
# Columns hidden by default (shown only when user opts in).
_DEFAULT_HIDDEN = (COL_PHASH,)

_SCROLL_DEBOUNCE_MS = 150   # ms after scroll stops before bumping priorities


def _fmt_size(b: Optional[int]) -> str:
    if b is None:
        return ""
    if b >= 1_048_576:
        return f"{b / 1_048_576:.1f} MB"
    if b >= 1024:
        return f"{b / 1024:.0f} KB"
    return f"{b} B"


def _fmt_dims(w: Optional[int], h: Optional[int]) -> str:
    if w is None or h is None:
        return ""
    return f"{w}×{h}"


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

class AssetTableModel(QAbstractTableModel):
    """
    Lazy-loading table model. Row count comes from count_filtered_assets;
    row data arrives in pages fetched from list_filtered_assets.

    The model tracks which pages are in flight so it never double-fetches.
    When a page arrives it emits dataChanged for the corresponding row range
    so the view repaints.
    """

    selection_hint = Signal(int)    # row count changed; view may reselect
    page_loaded    = Signal(int, int)  # first_row, last_row (inclusive)

    def __init__(
        self,
        bridge: QtDBBridge,
        thumb_bridge: QtThumbnailBridge,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._bridge = bridge
        self._thumb_bridge = thumb_bridge
        self._rows: list[Optional[federation.AssetRow]] = []
        self._total = 0
        self._pages_inflight: set[int] = set()

        # asset_id → LQ dest path (set once the DB lookup or disk scan resolves it)
        self._thumb_dests: dict[str, str] = {}
        # asset_id → True once we've submitted the DB lookup for thumb paths
        self._thumb_requested: set[str] = set()
        # currently-visible asset IDs (updated by update_visible_range)
        self._visible_ids: set[str] = set()

        # Current filter state.
        self._checked_labels: Optional[list[str]] = None
        self._filter_rules: list[FilterRule] = []
        self._sort_rules: list[SortRule] = []

    # -- public API ---------------------------------------------------------

    def refresh(
        self,
        checked_labels: Optional[list[str]] = None,
        filter_rules: Optional[list[FilterRule]] = None,
        sort_rules: Optional[list[SortRule]] = None,
    ) -> None:
        """Re-query with the given filter. Clears all cached rows."""
        self._checked_labels = checked_labels
        self._filter_rules = filter_rules or []
        self._sort_rules = sort_rules or []
        self._rows.clear()
        self._pages_inflight.clear()
        self._thumb_requested.clear()
        self._thumb_dests.clear()
        self._visible_ids.clear()
        self._total = 0
        self.beginResetModel()
        self.endResetModel()
        self._fetch_count()

    def row_data(self, row: int) -> Optional[federation.AssetRow]:
        if row < 0 or row >= self._total:
            return None
        if row < len(self._rows):
            return self._rows[row]
        return None

    def all_row_data(self) -> list[federation.AssetRow]:
        return [r for r in self._rows if r is not None]

    def update_visible_range(self, first: int, last: int) -> None:
        """
        Called by the view (debounced) when the visible row range changes.
        Updates _visible_ids and bumps already-queued BACKGROUND thumb jobs
        for visible rows to PRIORITY_VISIBLE.
        """
        self._visible_ids = set()
        for i in range(first, min(last + 1, len(self._rows))):
            row = self._rows[i]
            if row is not None:
                self._visible_ids.add(row.asset_id)

        # Bump in-flight BACKGROUND jobs for visible rows.
        for aid in self._visible_ids:
            dest = self._thumb_dests.get(aid)
            if dest and self._thumb_bridge.get_pixmap(dest) is None:
                self._thumb_bridge.bump_priority(dest, imgdb_thumbs.PRIORITY_VISIBLE)

    # -- QAbstractTableModel ------------------------------------------------

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return self._total

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return NUM_COLS

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return _HEADERS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row, col = index.row(), index.column()
        asset = self.row_data(row)

        # Trigger page fetch when data is requested but not yet available.
        if asset is None:
            self._ensure_page_loaded(row)

        if role == Qt.ItemDataRole.DisplayRole:
            if asset is None:
                return "…" if col == COL_PATH else ""
            if col == COL_THUMB:  return ""
            if col == COL_PATH:   return asset.rel_path
            if col == COL_ROOT:   return asset.root
            if col == COL_DIMS:   return _fmt_dims(asset.width, asset.height)
            if col == COL_FORMAT: return asset.format or ""
            if col == COL_SIZE:   return _fmt_size(asset.bytes)
            if col == COL_PHASH:  return asset.perceptual_hash or ""
            if col == COL_ID:     return asset.asset_id

        if role == Qt.ItemDataRole.DecorationRole and col == COL_THUMB:
            if asset is None:
                return None
            return self._get_or_request_thumb(asset)

        if role == Qt.ItemDataRole.ForegroundRole:
            if asset is not None and asset.exists_flag == 0:
                return QColor("#e67e22")

        if role == Qt.ItemDataRole.SizeHintRole and col == COL_THUMB:
            return QSize(THUMB_COL_SIZE, THUMB_COL_SIZE)

        if role == Qt.ItemDataRole.UserRole:
            return asset


        return None

    # -- page loading -------------------------------------------------------

    def _fetch_count(self) -> None:
        cl = self._checked_labels
        fr = list(self._filter_rules)

        def on_result(count: int) -> None:
            self.beginResetModel()
            self._total = count
            self._rows = [None] * count
            self.endResetModel()
            self.selection_hint.emit(count)
            if count > 0:
                self._fetch_page(0)

        def on_error(exc: BaseException) -> None:
            # SQL error in custom-SQL FilterRule — reset to empty.
            self.beginResetModel()
            self._total = 0
            self._rows = []
            self.endResetModel()

        self._bridge.submit(
            federation.count_filtered_assets,
            cl,
            fr,
            on_result=on_result,
            on_error=on_error,
        )

    def _ensure_page_loaded(self, row: int) -> None:
        page = row // PAGE_SIZE
        if page not in self._pages_inflight and (
            row >= len(self._rows) or self._rows[row] is None
        ):
            self._fetch_page(page)

    def _fetch_page(self, page: int) -> None:
        if page in self._pages_inflight:
            return
        self._pages_inflight.add(page)

        offset = page * PAGE_SIZE
        cl = self._checked_labels
        fr = list(self._filter_rules)
        sr = list(self._sort_rules)

        def on_result(rows: list[federation.AssetRow]) -> None:
            self._pages_inflight.discard(page)
            for i, r in enumerate(rows):
                idx = offset + i
                if idx < len(self._rows):
                    self._rows[idx] = r
                    if r is not None:
                        self._visible_ids.discard(r.asset_id)
            if rows:
                top = self.index(offset, 0)
                bot = self.index(offset + len(rows) - 1, NUM_COLS - 1)
                self.dataChanged.emit(top, bot)
                self.page_loaded.emit(offset, offset + len(rows) - 1)

        def on_error(_exc: BaseException) -> None:
            self._pages_inflight.discard(page)

        def fetch_fn(fed: federation.Federation) -> list[federation.AssetRow]:
            return list(federation.list_filtered_assets(
                fed, cl, fr, sr,
                limit=PAGE_SIZE,
                offset=offset,
            ))

        self._bridge.submit(
            fetch_fn,
            on_result=on_result,
            on_error=on_error,
        )

    # -- thumbnail ----------------------------------------------------------

    def _get_or_request_thumb(self, asset: federation.AssetRow) -> Optional[QPixmap]:
        aid = asset.asset_id

        # Fast path: LQ dest already resolved — check LRU.
        dest = self._thumb_dests.get(aid)
        if dest is not None:
            pix = self._thumb_bridge.get_pixmap(dest)
            return pix  # None while still generating — caller gets nothing until ready

        # DB lookup already submitted; wait for on_ready_data.
        if aid in self._thumb_requested:
            return None

        # Check for an existing LQ thumbnail from a previous session.
        # Strictly LQ only — never show HQ in the table (different aspect crop).
        existing_lq = self._find_existing_lq_thumb(asset)
        if existing_lq:
            self._thumb_dests[aid] = existing_lq
            self._thumb_requested.add(aid)
            priority = (imgdb_thumbs.PRIORITY_VISIBLE if aid in self._visible_ids
                        else imgdb_thumbs.PRIORITY_BACKGROUND)
            self._thumb_bridge.request(
                asset_id=aid,
                src_abs_path="",   # unused when file already exists on disk
                dest_abs_path=existing_lq,
                priority=priority,
                size=imgdb_thumbs.THUMB_SIZE_LQ,
                fast=True,
                on_ready=lambda _aid, pix: self._on_thumb_ready(_aid, pix),
            )
            return None

        # Submit DB lookup to resolve hash → compute LQ dest path.
        self._thumb_requested.add(aid)
        root = asset.root

        def fetch(fed: federation.Federation):
            shard = fed.shards.get(root)
            if shard is None:
                return None
            try:
                a = imgdb.get_asset(shard.conn, aid)
                src = os.path.join(shard.abs_path, a.rel_path)
                dest = imgdb_thumbs.thumb_path(shard.abs_path, aid, a.file_hash)
                return src, dest
            except Exception:
                return None

        def on_ready_data(data) -> None:
            if data is None:
                return
            src, dest = data
            self._thumb_dests[aid] = dest
            priority = (imgdb_thumbs.PRIORITY_VISIBLE if aid in self._visible_ids
                        else imgdb_thumbs.PRIORITY_BACKGROUND)
            self._thumb_bridge.request(
                asset_id=aid,
                src_abs_path=src,
                dest_abs_path=dest,
                priority=priority,
                size=imgdb_thumbs.THUMB_SIZE_LQ,
                fast=True,
                on_ready=lambda _aid, pix: self._on_thumb_ready(_aid, pix),
            )

        self._bridge.submit(fetch, on_result=on_ready_data)
        return None

    def _find_existing_lq_thumb(self, asset: federation.AssetRow) -> Optional[str]:
        """
        Look for an already-generated LQ thumbnail on disk without a DB round-trip.
        Returns the LQ path, or None.  Strictly excludes HQ files (-hq.webp).
        """
        root_path = self._bridge.root_abs_path(asset.root)
        if root_path is None:
            return None
        fan = asset.asset_id[:2]
        thumb_dir = os.path.join(root_path, imgdb.THUMBS_DIRNAME, fan)
        if not os.path.isdir(thumb_dir):
            return None
        prefix = asset.asset_id + "-"
        hq_suffix = f"-hq{imgdb_thumbs.THUMB_EXT}"
        for name in os.listdir(thumb_dir):
            if (name.startswith(prefix)
                    and name.endswith(imgdb_thumbs.THUMB_EXT)
                    and not name.endswith(hq_suffix)):
                return os.path.join(thumb_dir, name)
        return None

    def invalidate_asset_thumb(self, asset_id: str) -> None:
        """
        Clear thumbnail tracking state for one asset so the next paint
        re-requests from scratch.  Call after evicting the pixmap from the
        LRU so _get_or_request_thumb doesn't return None indefinitely.
        """
        self._thumb_dests.pop(asset_id, None)
        self._thumb_requested.discard(asset_id)

    def _on_thumb_ready(self, asset_id: str, pixmap: QPixmap) -> None:
        for i, row in enumerate(self._rows):
            if row is not None and row.asset_id == asset_id:
                idx = self.index(i, COL_THUMB)
                self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DecorationRole])
                break


# ---------------------------------------------------------------------------
# View
# ---------------------------------------------------------------------------

class AssetTableView(QTableView):
    """
    Wraps AssetTableModel with column sizing and selection behaviour.
    Emits selection_changed(rows: list[AssetRow]) when the selection changes.
    Sends debounced viewport-change notifications to the model so it can
    prioritise thumbnail generation for visible rows.

    Context menu:
      Right-clicking any data cell shows copy actions for the clicked cell,
      then emits context_menu_requested so the parent can append further
      actions (with its own separator).  Pass `object` type to avoid Qt
      metatype registration for QMenu.
    """

    selection_changed      = Signal(list)           # list[federation.AssetRow]
    context_menu_requested = Signal(object, list)   # (QMenu, list[AssetRow])

    def __init__(self, model: AssetTableModel, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._model = model
        self.setModel(model)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setAlternatingRowColors(True)
        self.setShowGrid(False)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(THUMB_COL_SIZE + 4)
        hh = self.horizontalHeader()
        hh.setSectionResizeMode(COL_THUMB, QHeaderView.ResizeMode.Fixed)
        hh.resizeSection(COL_THUMB, THUMB_COL_SIZE + 8)
        _interactive_defaults = {
            COL_PATH: 300, COL_ROOT: 90, COL_DIMS: 90, COL_FORMAT: 60,
            COL_SIZE: 70, COL_PHASH: 140,
        }
        for col, width in _interactive_defaults.items():
            hh.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
            hh.resizeSection(col, width)
        hh.setSectionResizeMode(COL_ID, QHeaderView.ResizeMode.Stretch)
        for col in _DEFAULT_HIDDEN:
            hh.hideSection(col)
        hh.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        hh.customContextMenuRequested.connect(self._show_column_menu)
        self.selectionModel().selectionChanged.connect(self._on_selection_changed)
        model.page_loaded.connect(self._on_page_loaded)

        # Debounced scroll handler: wait until scrolling settles before bumping
        # thumbnail priorities, so fast scrolling doesn't cause constant reshuffles.
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setSingleShot(True)
        self._scroll_timer.setInterval(_SCROLL_DEBOUNCE_MS)
        self._scroll_timer.timeout.connect(self._on_scroll_settled)
        self.verticalScrollBar().valueChanged.connect(self._scroll_timer.start)

    def save_header_state(self) -> bytes:
        return bytes(self.horizontalHeader().saveState())

    def restore_header_state(self, state: bytes) -> None:
        hh = self.horizontalHeader()
        # Apply defaults first; restoreState will override them if the saved
        # state is compatible (same column count).
        for col in _DEFAULT_HIDDEN:
            hh.hideSection(col)
        hh.restoreState(state)
        # restoreState overwrites resize modes — re-apply ours.
        hh.setSectionResizeMode(COL_THUMB, QHeaderView.ResizeMode.Fixed)
        for col in (COL_PATH, COL_ROOT, COL_DIMS, COL_FORMAT, COL_SIZE, COL_PHASH):
            hh.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(COL_ID, QHeaderView.ResizeMode.Stretch)

    def contextMenuEvent(self, event) -> None:
        idx = self.indexAt(event.pos())
        if not idx.isValid():
            return

        row = idx.row()
        col = idx.column()
        asset = self._model.row_data(row)

        # Determine scope for parent-level actions.
        # If the right-clicked row is already selected, use the full selection
        # so bulk actions feel natural.  Otherwise, scope to just this row so
        # right-clicking a different row doesn't accidentally act on the old set.
        selected_rows = sorted({i.row() for i in self.selectedIndexes()})
        if row in selected_rows:
            scope_assets = [self._model.row_data(r) for r in selected_rows]
        else:
            scope_assets = [asset]
        scope_assets = [a for a in scope_assets if a is not None]

        menu = QMenu(self)

        # -- Copy actions for the clicked cell --------------------------------
        if col != COL_THUMB and asset is not None:
            cell_text = str(self._model.data(idx, Qt.ItemDataRole.DisplayRole) or "")
            if cell_text:
                col_name = _HEADERS[col]
                act = menu.addAction(f"Copy {col_name}")
                act.triggered.connect(
                    lambda _=False, t=cell_text: QApplication.clipboard().setText(t)
                )

        # Asset ID is universally useful; always offer it unless it's the clicked column.
        if asset is not None and col != COL_ID:
            aid = asset.asset_id
            act = menu.addAction("Copy Asset ID")
            act.triggered.connect(
                lambda _=False, t=aid: QApplication.clipboard().setText(t)
            )

        # -- Parent-supplied actions (merge, delete, etc.) --------------------
        # The parent adds a separator then its own actions when it handles this.
        self.context_menu_requested.emit(menu, scope_assets)

        if not menu.isEmpty():
            menu.exec(event.globalPos())

    def _show_column_menu(self, pos) -> None:
        hh = self.horizontalHeader()
        menu = QMenu(self)
        for col in _TOGGLEABLE_COLS:
            action = menu.addAction(_HEADERS[col])
            action.setCheckable(True)
            action.setChecked(not hh.isSectionHidden(col))
            action.toggled.connect(
                lambda checked, c=col: hh.showSection(c) if checked else hh.hideSection(c)
            )
        menu.exec(hh.mapToGlobal(pos))

    def _on_page_loaded(self, first: int, last: int) -> None:
        selected = sorted({idx.row() for idx in self.selectedIndexes()})
        if any(first <= r <= last for r in selected):
            self._on_selection_changed()

    def _on_selection_changed(self, *_) -> None:
        rows = sorted({idx.row() for idx in self.selectedIndexes()})
        assets = [self._model.row_data(r) for r in rows]
        assets = [a for a in assets if a is not None]
        self.selection_changed.emit(assets)

    def _on_scroll_settled(self) -> None:
        first, last = self._visible_row_range()
        self._model.update_visible_range(first, last)

    def _visible_row_range(self) -> tuple[int, int]:
        vp = self.viewport()
        first = self.rowAt(0)
        last  = self.rowAt(vp.height() - 1)
        if first < 0:
            first = 0
        if last < 0:
            last = max(0, self._model.rowCount() - 1)
        return first, last
