"""
DetailPanel — right-side panel showing metadata, tags, and captions for the
currently selected asset, plus batch-action UI when multiple assets are selected.

Single-asset state:  full thumbnail + metadata + tag chips + caption editors
Multi-asset state:   asset count + batch [Add tag…] / [Delete…] buttons
Empty state:         placeholder text
"""
from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QCompleter, QDialog, QFrame, QHBoxLayout, QLabel,
    QLayout, QLineEdit, QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy,
    QStackedWidget, QStyle, QStyledItemDelegate, QStyleOptionViewItem,
    QVBoxLayout, QWidget,
)

import federation
import imgdb
import imgdb_thumbs
from imgdb_thumbs_qt import QtThumbnailBridge
from imgdb_worker_qt import QtDBBridge
from ui_dialogs import (
    AddCaptionDialog, AddTagCategoryDialog, AddToDatasetDialog, BatchMoveDialog,
    BatchRemoveTagDialog, BatchReplaceTagDialog, BatchTagDialog,
    ConfirmDeleteDialog, MergeDialog, MoveDialog, RenameDialog,
)

THUMB_DISPLAY_SIZE = 256
PANEL_WIDTH = 270


# ---------------------------------------------------------------------------
# Thumbnail label
# ---------------------------------------------------------------------------

class _FlexLabel(QLabel):
    """QLabel whose minimumSizeHint is always (0, 0).

    QLabel with wordWrap=True reports a minimum width equal to its longest
    unbreakable word, which propagates up through the layout and prevents the
    containing scroll area from narrowing below that point.  Returning (0, 0)
    lets the label word-wrap at whatever width Qt actually gives it.
    """
    def minimumSizeHint(self) -> QSize:
        return QSize(0, 0)


class _ScaledThumbnailLabel(QLabel):
    """QLabel that scales a stored source pixmap to fill available width
    (capped at THUMB_DISPLAY_SIZE) so the detail panel can narrow freely."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._source: Optional[QPixmap] = None
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("background: #f0f0f0; border-radius: 4px;")
        self.setText("…")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setMinimumSize(0, 0)

    def set_source_pixmap(self, pixmap: Optional[QPixmap]) -> None:
        self._source = pixmap
        if pixmap is None or pixmap.isNull():
            super().setPixmap(QPixmap())
            self.setText("…")
        else:
            self.setText("")
            self._apply_scaled()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._source and not self._source.isNull():
            self._apply_scaled()

    def _apply_scaled(self) -> None:
        w = min(self.width(), THUMB_DISPLAY_SIZE)
        h = min(self.height() if self.height() > 0 else THUMB_DISPLAY_SIZE, THUMB_DISPLAY_SIZE)
        if w <= 0:
            return
        scaled = self._source.scaled(
            w, h,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        super().setPixmap(scaled)

    def sizeHint(self) -> QSize:
        return QSize(THUMB_DISPLAY_SIZE, THUMB_DISPLAY_SIZE)

    def minimumSizeHint(self) -> QSize:
        return QSize(0, 0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return min(width, THUMB_DISPLAY_SIZE)


# ---------------------------------------------------------------------------
# Tag chip
# ---------------------------------------------------------------------------

class _TagChip(QWidget):
    removed = Signal(str)          # tag_id (UUID)
    filter_requested = Signal(str) # name

    def __init__(self, name: str, tag_id: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        label = QLabel(name)
        label.setCursor(Qt.CursorShape.PointingHandCursor)
        label.mousePressEvent = lambda _e: self.filter_requested.emit(name)
        remove_btn = QPushButton("×")
        remove_btn.setFixedSize(16, 16)
        remove_btn.setFlat(True)
        remove_btn.setStyleSheet("font-weight: bold; color: gray;")
        remove_btn.clicked.connect(lambda: self.removed.emit(tag_id))
        row = QHBoxLayout(self)
        row.setContentsMargins(6, 2, 4, 2)
        row.setSpacing(2)
        row.addWidget(label)
        row.addWidget(remove_btn)
        self.setStyleSheet(
            "background: #dce8f5; border-radius: 10px;"
        )


# ---------------------------------------------------------------------------
# Tag group (one per type)
# ---------------------------------------------------------------------------

class _TagGroup(QWidget):
    """Chips + input for a single tag type."""

    tag_added = Signal(str)         # name (type is this group's context)
    tag_removed = Signal(str)       # tag_id (UUID)
    filter_requested = Signal(str)  # name

    def __init__(self, type_name: str, is_general: bool, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._type_name = type_name
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(2)
        if not is_general:
            header = QLabel(f"<b>{type_name}</b>")
            header.setStyleSheet("font-size: 11px; color: #555;")
            layout.addWidget(header)
        self._chips = _FlowLayout()
        self._chip_by_id: dict[str, _TagChip] = {}
        layout.addLayout(self._chips)
        self._input = _TagInput()
        self._input.tag_submitted.connect(self.tag_added)
        layout.addWidget(self._input)

    def load(self, tag_rows: list) -> None:
        # Diff against existing chips: destroy/recreate only the deltas so a
        # single tag edit doesn't tear down and rebuild every chip in the group.
        desired = {row["tag_id"]: row["name"] for row in tag_rows}
        for tag_id in list(self._chip_by_id):
            if tag_id not in desired:
                chip = self._chip_by_id.pop(tag_id)
                idx = self._chips.indexOf(chip)
                if idx >= 0:
                    self._chips.takeAt(idx)
                chip.deleteLater()
        for row in tag_rows:
            tag_id = row["tag_id"]
            if tag_id in self._chip_by_id:
                continue
            chip = _TagChip(row["name"], tag_id)
            chip.removed.connect(self.tag_removed)
            chip.filter_requested.connect(self.filter_requested)
            self._chips.addWidget(chip)
            self._chip_by_id[tag_id] = chip

    def set_suggestions(self, tags: list[tuple[str, str, int]]) -> None:
        self._input.set_suggestions([t for t in tags if t[1] == self._type_name])

    def focus_input(self) -> None:
        self._input.focus()


# ---------------------------------------------------------------------------
# Tags section (stacks groups + add-category button)
# ---------------------------------------------------------------------------

class _TagsSection(QWidget):
    """Vertically stacked tag groups, one per type, with an add-category button."""

    tag_added = Signal(str, str)             # name, type_name
    tag_removed = Signal(str)                # tag_id
    filter_requested = Signal(str)           # name
    import_from_caption_requested = Signal() # triggered by "Import from caption" btn

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._all_types: list[str] = ["General"]
        self._all_tags: list[tuple[str, str, int]] = []
        self._groups: dict[str, _TagGroup] = {}

        self._groups_layout = QVBoxLayout()
        self._groups_layout.setContentsMargins(0, 0, 0, 0)
        self._groups_layout.setSpacing(0)

        self._add_cat_btn = QPushButton("+ Add Tag Category")
        self._add_cat_btn.setFlat(True)
        self._add_cat_btn.setStyleSheet("color: #2980b9; text-align: left;")
        self._add_cat_btn.clicked.connect(self._on_add_category)

        import_btn = QPushButton("Import from caption…")
        import_btn.setFlat(True)
        import_btn.setStyleSheet("color: #2980b9; text-align: left;")
        import_btn.setToolTip("Extract tags from this image's caption text")
        import_btn.clicked.connect(self.import_from_caption_requested)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 0, 0)
        btn_row.setSpacing(8)
        btn_row.addWidget(self._add_cat_btn)
        btn_row.addWidget(import_btn)
        btn_row.addStretch()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(self._groups_layout)
        layout.addLayout(btn_row)

    def load_tags(self, tag_rows: list, all_types: list[str]) -> None:
        self._all_types = all_types or ["General"]

        by_type: dict[str, list] = {}
        for row in tag_rows:
            by_type.setdefault(row["type_name"], []).append(row)

        needed = ["General"] + [
            t for t in self._all_types if t != "General" and t in by_type
        ]
        needed_set = set(needed)

        # Remove groups whose type is no longer present on this asset.
        for type_name in [k for k in self._groups if k not in needed_set]:
            grp = self._groups.pop(type_name)
            self._groups_layout.removeWidget(grp)
            grp.deleteLater()

        # Create missing groups (in order) and refresh all chip lists.
        for i, type_name in enumerate(needed):
            if type_name not in self._groups:
                self._add_group(type_name, type_name == "General", rows=[], position=i)
            self._groups[type_name].load(by_type.get(type_name, []))

    def set_suggestions(self, tags: list[tuple[str, str, int]]) -> None:
        self._all_tags = tags
        for grp in self._groups.values():
            grp.set_suggestions(tags)

    def focus_input(self, type_name: str) -> None:
        grp = self._groups.get(type_name)
        if grp:
            grp.focus_input()

    def _add_group(self, type_name: str, is_general: bool, rows: list, position: int = -1) -> None:
        grp = _TagGroup(type_name, is_general)
        grp.tag_added.connect(lambda name, tn=type_name: self.tag_added.emit(name, tn))
        grp.tag_removed.connect(self.tag_removed)
        grp.filter_requested.connect(self.filter_requested)
        grp.load(rows)
        if position >= 0:
            self._groups_layout.insertWidget(position, grp)
        else:
            self._groups_layout.addWidget(grp)
        self._groups[type_name] = grp
        grp.set_suggestions(self._all_tags)

    def _on_add_category(self) -> None:
        shown = set(self._groups.keys())
        dlg = AddTagCategoryDialog(self._all_types, shown, self._all_tags, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self.tag_added.emit(dlg.tag_name(), dlg.type_name())


# ---------------------------------------------------------------------------
# Caption editor block
# ---------------------------------------------------------------------------

class _CaptionBlock(QWidget):
    """One kind + its always-editable QPlainTextEdit that saves on focus-out."""

    save_requested = Signal(str, str)       # kind, content
    delete_requested = Signal(str)          # kind
    validated_changed = Signal(str, bool)   # kind, validated

    def __init__(
        self, kind: str, content: str, is_validated: bool = False,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._kind = kind
        self._original = content

        kind_row = QHBoxLayout()
        kind_label = QLabel(f"<b>{kind}</b>")
        validated_cb = QCheckBox("Validated")
        validated_cb.setStyleSheet("font-size: 11px; color: #555;")
        validated_cb.setChecked(is_validated)
        validated_cb.toggled.connect(lambda v: self.validated_changed.emit(kind, v))
        self._validated_cb = validated_cb
        del_btn = QPushButton("−")
        del_btn.setFixedWidth(22)
        del_btn.setStyleSheet("color: #c0392b; font-weight: bold;")
        del_btn.setToolTip(f"Delete '{kind}' caption")
        del_btn.clicked.connect(lambda: self.delete_requested.emit(kind))
        kind_row.addWidget(kind_label)
        kind_row.addWidget(validated_cb)
        kind_row.addStretch()
        kind_row.addWidget(del_btn)

        self._editor = QPlainTextEdit(content)
        self._editor.setFixedHeight(72)
        self._editor.setPlaceholderText("(empty)")
        self._editor.focusOutEvent = self._on_focus_out  # type: ignore[assignment]

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(2)
        layout.addLayout(kind_row)
        layout.addWidget(self._editor)

    def set_content(self, content: str) -> None:
        self._original = content
        self._editor.setPlainText(content)

    def set_state(self, content: str, is_validated: bool) -> None:
        """Reuse this block for another asset's caption of the same kind, without
        emitting validated_changed for the programmatic checkbox update."""
        self.set_content(content)
        self._validated_cb.blockSignals(True)
        self._validated_cb.setChecked(is_validated)
        self._validated_cb.blockSignals(False)

    def _on_focus_out(self, event) -> None:
        QPlainTextEdit.focusOutEvent(self._editor, event)
        current = self._editor.toPlainText()
        if current != self._original:
            self._original = current
            self.save_requested.emit(self._kind, current)


# ---------------------------------------------------------------------------
# Single-asset detail
# ---------------------------------------------------------------------------

class _SingleDetail(QWidget):

    favorite_changed = Signal(str, bool)          # asset_id, is_favorite
    rename_requested = Signal(str, str)           # asset_id, current_rel_path
    move_requested = Signal(str, str)             # asset_id, new_rel_path
    delete_requested = Signal(str, str)           # asset_id, rel_path
    merge_requested = Signal(str)                 # asset_id (this asset is "merged into" target)
    add_to_dataset_requested = Signal(str)        # asset_id
    remove_from_dataset_requested = Signal(str)   # asset_id
    tag_filter_requested = Signal(str)            # tag_name — click chip to filter
    tag_added = Signal(str, str, str)             # asset_id, tag_name, type_name
    tag_removed = Signal(str, str)                # asset_id, tag_id
    tags_validated_changed = Signal(str, bool)    # asset_id, validated
    caption_saved = Signal(str, str, str)         # asset_id, kind, content
    caption_deleted = Signal(str, str)            # asset_id, kind
    caption_validated_changed = Signal(str, str, bool)  # asset_id, kind, validated
    import_from_caption_requested = Signal(str)   # asset_id

    def __init__(
        self,
        bridge: QtDBBridge,
        thumb_bridge: QtThumbnailBridge,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._bridge = bridge
        self._thumb_bridge = thumb_bridge
        self._asset: Optional[federation.AssetRow] = None
        self._root_abs: Optional[str] = None
        self._caption_blocks: dict[str, _CaptionBlock] = {}
        self._active_dataset: Optional[str] = None
        self._is_favorite = False

        # Thumbnail, with a favorite star overlaid at the top-left. The star is
        # an absolutely-positioned child of the label (not in any layout), so
        # showing/hiding it never shifts the center-aligned thumbnail image.
        self._thumb_label = _ScaledThumbnailLabel()
        self._fav_star = QLabel("★", self._thumb_label)
        self._fav_star.setStyleSheet(
            "color: #f1c40f; font-size: 22px; background: transparent;"
        )
        self._fav_star.setToolTip("Favorited")
        self._fav_star.adjustSize()
        self._fav_star.move(4, 4)
        self._fav_star.hide()

        # Metadata
        self._meta_label = _FlexLabel()
        self._meta_label.setWordWrap(True)
        self._meta_label.setStyleSheet("font-size: 12px;")

        self._datasets_label = _FlexLabel()
        self._datasets_label.setWordWrap(True)
        self._datasets_label.setStyleSheet("font-size: 11px; color: #555;")
        self._datasets_label.hide()

        sep1 = _sep()

        # Tags
        tags_header_label = QLabel("<b>TAGS</b>")
        self._tags_validated_cb = QCheckBox("Validated")
        self._tags_validated_cb.setStyleSheet("font-size: 11px; color: #555;")
        self._tags_validated_cb.toggled.connect(self._on_tags_validated_toggled)
        tags_header_row = QHBoxLayout()
        tags_header_row.setContentsMargins(0, 0, 0, 0)
        tags_header_row.addWidget(tags_header_label)
        tags_header_row.addWidget(self._tags_validated_cb)
        tags_header_row.addStretch()
        self._tags_section = _TagsSection()
        self._tags_section.tag_added.connect(self._add_tag)
        self._tags_section.tag_removed.connect(self._remove_tag)
        self._tags_section.filter_requested.connect(self.tag_filter_requested)
        self._tags_section.import_from_caption_requested.connect(
            self._on_import_from_caption
        )

        sep2 = _sep()

        # Captions
        captions_header = QLabel("<b>CAPTIONS</b>")
        self._captions_container = QVBoxLayout()
        self._captions_container.setContentsMargins(0, 0, 0, 0)
        self._captions_container.setSpacing(0)
        add_caption_btn = QPushButton("+ Add caption type…")
        add_caption_btn.setFlat(True)
        add_caption_btn.setStyleSheet("color: #2980b9; text-align: left;")
        add_caption_btn.setToolTip("Add a new caption type (one caption per type)")
        add_caption_btn.clicked.connect(self._add_caption)

        sep3 = _sep()

        # Actions
        self._favorite_btn = QPushButton("☆ Favorite")
        self._favorite_btn.setCheckable(True)
        self._favorite_btn.clicked.connect(self._on_favorite_clicked)
        rename_btn = QPushButton("Rename…")
        rename_btn.clicked.connect(self._rename)
        move_btn = QPushButton("Move…")
        move_btn.clicked.connect(self._move)
        delete_btn = QPushButton("Delete")
        delete_btn.setStyleSheet("color: #c0392b;")
        delete_btn.clicked.connect(self._delete)
        merge_btn = QPushButton("Merge into…")
        merge_btn.clicked.connect(self._merge_into)
        dataset_btn = QPushButton("Add to dataset…")
        dataset_btn.clicked.connect(self._add_to_dataset)
        self._remove_from_dataset_btn = QPushButton("Remove from dataset")
        self._remove_from_dataset_btn.setStyleSheet("color: #c0392b;")
        self._remove_from_dataset_btn.clicked.connect(self._remove_from_dataset)
        self._remove_from_dataset_btn.hide()

        actions_row = QHBoxLayout()
        actions_row.addWidget(self._favorite_btn)
        actions_row.addWidget(rename_btn)
        actions_row.addWidget(move_btn)
        actions_row.addWidget(delete_btn)
        actions_row.addStretch()

        # Scroll area content
        content = QWidget()
        cl = QVBoxLayout(content)
        cl.setContentsMargins(8, 8, 8, 8)
        cl.setSpacing(4)
        cl.addWidget(self._thumb_label, alignment=Qt.AlignmentFlag.AlignHCenter)
        cl.addWidget(self._meta_label)
        cl.addWidget(self._datasets_label)
        cl.addWidget(sep1)
        cl.addLayout(tags_header_row)
        cl.addWidget(self._tags_section)
        cl.addWidget(sep2)
        cl.addWidget(captions_header)
        cl.addLayout(self._captions_container)
        cl.addWidget(add_caption_btn)
        cl.addWidget(sep3)
        cl.addLayout(actions_row)
        cl.addWidget(merge_btn)
        cl.addWidget(dataset_btn)
        cl.addWidget(self._remove_from_dataset_btn)
        cl.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll)

    def set_tag_suggestions(self, tags: list[tuple[str, str, int]]) -> None:
        self._tags_section.set_suggestions(tags)

    def load_asset(
        self,
        asset: federation.AssetRow,
        root_abs: str,
    ) -> None:
        self._asset = asset
        self._root_abs = root_abs
        self._thumb_label.set_source_pixmap(None)
        self._apply_favorite_state(False)  # reset until the fetch reports it
        self._meta_label.setText(
            f"<b>{asset.rel_path}</b><br>"
            f"Root: {asset.root}<br>"
            f"{_fmt_dims(asset.width, asset.height)}  ·  "
            f"{asset.format or '—'}  ·  {_fmt_size(asset.bytes)}"
        )
        self._load_thumb(asset, root_abs)
        self._load_tags_and_captions(asset)

    def _load_thumb(self, asset: federation.AssetRow, root_abs: str) -> None:
        src = os.path.join(root_abs, asset.rel_path)

        def fetch_hash(fed: federation.Federation):
            shard = fed.shards.get(asset.root)
            if shard is None:
                return None
            try:
                return imgdb.get_asset(shard.conn, asset.asset_id).file_hash
            except Exception:
                return None

        def on_hash(h: Optional[str]) -> None:
            if h is None:
                return
            lq_dest = imgdb_thumbs.thumb_path(root_abs, asset.asset_id, h)
            hq_dest = imgdb_thumbs.thumb_path_hq(root_abs, asset.asset_id, h)
            # Show the LQ thumb immediately if it's already in the pixmap cache
            # (loaded earlier by the table view) so there's no blank gap.
            lq_pix = self._thumb_bridge.get_pixmap(lq_dest)
            if lq_pix and not lq_pix.isNull():
                self._on_thumb_ready(asset.asset_id, lq_pix)
            # Always request the full-resolution HQ thumb for the detail panel.
            self._thumb_bridge.request(
                asset_id=asset.asset_id,
                src_abs_path=src,
                dest_abs_path=hq_dest,
                priority=imgdb_thumbs.PRIORITY_SELECTED,
                size=imgdb_thumbs.THUMB_SIZE_HQ,
                fast=False,
                on_ready=self._on_thumb_ready,
            )

        self._bridge.submit(fetch_hash, on_result=on_hash)

    def _on_thumb_ready(self, asset_id: str, pixmap: QPixmap) -> None:
        if self._asset and self._asset.asset_id == asset_id:
            self._thumb_label.set_source_pixmap(pixmap)

    def _load_tags_and_captions(self, asset: federation.AssetRow) -> None:
        """Full bundle reload — used on selection change (one worker round-trip)."""
        aid = asset.asset_id
        root = asset.root

        def fetch(fed: federation.Federation):
            shard = fed.shards.get(root)
            if shard is None:
                return [], {}, [], [], False, False
            tag_rows = imgdb.get_tags_for_asset(shard.conn, aid)
            caps = imgdb.get_captions_for_asset(shard.conn, aid)
            all_types = federation.list_all_tag_types_federation(fed)
            datasets = imgdb.get_dataset_membership(shard.conn, aid)
            tags_validated = imgdb.get_tags_validated(shard.conn, aid)
            is_favorite = imgdb.get_image_flag(shard.conn, aid, imgdb.ATTR_IS_FAVORITE)
            return tag_rows, caps, all_types, datasets, tags_validated, is_favorite

        def on_result(data) -> None:
            if self._asset is None or self._asset.asset_id != aid:
                return
            tag_rows, caps, all_types, datasets, tags_validated, is_favorite = data
            self._rebuild_tags(tag_rows, all_types)
            self._tags_validated_cb.setChecked(tags_validated)
            self._rebuild_captions(caps)
            self._load_datasets(datasets)
            self._apply_favorite_state(is_favorite)

        self._bridge.submit(fetch, on_result=on_result)

    def _apply_favorite_state(self, on: bool) -> None:
        self._is_favorite = on
        self._favorite_btn.setChecked(on)
        self._favorite_btn.setText("★ Favorited" if on else "☆ Favorite")
        self._fav_star.setVisible(on)
        if on:
            self._fav_star.raise_()

    def _on_favorite_clicked(self) -> None:
        if self._asset is None:
            self._favorite_btn.setChecked(self._is_favorite)  # nothing selected
            return
        on = self._favorite_btn.isChecked()
        self._apply_favorite_state(on)
        self.favorite_changed.emit(self._asset.asset_id, on)

    def _load_tags(self, asset: federation.AssetRow) -> None:
        """Reload only tags + validation state. Used after a single tag edit so a
        tag add/remove doesn't re-fetch captions and dataset membership too."""
        aid = asset.asset_id
        root = asset.root

        def fetch(fed: federation.Federation):
            shard = fed.shards.get(root)
            if shard is None:
                return [], [], False
            tag_rows = imgdb.get_tags_for_asset(shard.conn, aid)
            all_types = federation.list_all_tag_types_federation(fed)
            tags_validated = imgdb.get_tags_validated(shard.conn, aid)
            return tag_rows, all_types, tags_validated

        def on_result(data) -> None:
            if self._asset is None or self._asset.asset_id != aid:
                return
            tag_rows, all_types, tags_validated = data
            self._rebuild_tags(tag_rows, all_types)
            self._tags_validated_cb.setChecked(tags_validated)

        self._bridge.submit(fetch, on_result=on_result)

    def _load_datasets(self, datasets: list) -> None:
        if datasets:
            self._datasets_label.setText("Datasets: " + ", ".join(datasets))
            self._datasets_label.show()
        else:
            self._datasets_label.hide()

    def _rebuild_tags(self, tag_rows: list, all_types: list[str]) -> None:
        self._tags_section.load_tags(tag_rows, all_types)

    def _rebuild_captions(self, caps: dict[str, tuple[str, bool]]) -> None:
        # Diff by kind: reuse an existing block for the same kind (update content
        # + validated in place), add blocks for new kinds, drop blocks for gone
        # kinds. Avoids tearing down every editor when the kind set is unchanged.
        for kind in list(self._caption_blocks):
            if kind not in caps:
                block = self._caption_blocks.pop(kind)
                self._captions_container.removeWidget(block)
                block.deleteLater()
        for kind, (content, is_validated) in caps.items():
            block = self._caption_blocks.get(kind)
            if block is None:
                block = _CaptionBlock(kind, content, is_validated)
                block.save_requested.connect(
                    lambda k, c: self.caption_saved.emit(self._asset.asset_id if self._asset else "", k, c)
                )
                block.delete_requested.connect(self._delete_caption)
                block.validated_changed.connect(self._on_caption_validated_toggled)
                self._captions_container.addWidget(block)
                self._caption_blocks[kind] = block
            else:
                block.set_state(content, is_validated)

    def _add_tag(self, name: str, type_name: str) -> None:
        if self._asset:
            self.tag_added.emit(self._asset.asset_id, name, type_name)
            self._load_tags(self._asset)

    def _remove_tag(self, tag_id: str) -> None:
        if self._asset:
            self.tag_removed.emit(self._asset.asset_id, tag_id)
            self._load_tags(self._asset)

    def _on_tags_validated_toggled(self, checked: bool) -> None:
        if self._asset:
            self.tags_validated_changed.emit(self._asset.asset_id, checked)

    def _on_caption_validated_toggled(self, kind: str, checked: bool) -> None:
        if self._asset:
            self.caption_validated_changed.emit(self._asset.asset_id, kind, checked)

    def _delete_caption(self, kind: str) -> None:
        if self._asset:
            self.caption_deleted.emit(self._asset.asset_id, kind)
            self._load_tags_and_captions(self._asset)

    def _add_caption(self) -> None:
        if not self._asset:
            return
        dlg = AddCaptionDialog(list(self._caption_blocks.keys()), self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        kind = dlg.kind()
        self.caption_saved.emit(self._asset.asset_id, kind, "")
        self._load_tags_and_captions(self._asset)

    def _rename(self) -> None:
        if not self._asset:
            return
        dlg = RenameDialog(self._asset.rel_path, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self.rename_requested.emit(self._asset.asset_id, dlg.new_rel_path())

    def _move(self) -> None:
        if not self._asset or not self._root_abs:
            return
        dlg = MoveDialog(self._asset.rel_path, self._root_abs, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self.move_requested.emit(self._asset.asset_id, dlg.new_rel_path())

    def _delete(self) -> None:
        if not self._asset:
            return
        dlg = ConfirmDeleteDialog([self._asset.rel_path], self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self.delete_requested.emit(self._asset.asset_id, self._asset.rel_path)

    def _merge_into(self) -> None:
        if self._asset:
            self.merge_requested.emit(self._asset.asset_id)

    def _add_to_dataset(self) -> None:
        if self._asset:
            self.add_to_dataset_requested.emit(self._asset.asset_id)

    def _remove_from_dataset(self) -> None:
        if self._asset:
            self.remove_from_dataset_requested.emit(self._asset.asset_id)

    def _on_import_from_caption(self) -> None:
        if self._asset:
            self.import_from_caption_requested.emit(self._asset.asset_id)

    def set_active_dataset(self, name: Optional[str]) -> None:
        self._active_dataset = name
        if name:
            self._remove_from_dataset_btn.setText(f'Remove from “{name}”')
            self._remove_from_dataset_btn.show()
        else:
            self._remove_from_dataset_btn.hide()


# ---------------------------------------------------------------------------
# Multi-select / batch panel
# ---------------------------------------------------------------------------

class _MultiDetail(QWidget):

    batch_tag_requested = Signal(list)                    # list[str] asset_ids
    batch_remove_tag_requested = Signal(list)             # list[AssetRow]
    batch_replace_tag_requested = Signal(list)            # list[AssetRow]
    batch_move_requested = Signal(list)                   # list[AssetRow], all same shard
    batch_delete_requested = Signal(list)                 # list[AssetRow]
    batch_add_to_dataset_requested = Signal(list)         # list[AssetRow]
    batch_remove_from_dataset_requested = Signal(list)    # list[AssetRow]
    merge_two_requested = Signal(list)                    # list[AssetRow], exactly 2 same shard

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._assets: list[federation.AssetRow] = []

        self._count_label = QLabel()
        self._count_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._count_label.setStyleSheet("font-size: 14px; padding: 8px;")

        self._shard_label = QLabel()
        self._shard_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._shard_label.setStyleSheet("color: gray; font-size: 11px;")

        tag_btn = QPushButton("Add tag to all…")
        tag_btn.clicked.connect(lambda: self.batch_tag_requested.emit(
            [a.asset_id for a in self._assets]
        ))

        remove_tag_btn = QPushButton("Remove tag from all…")
        remove_tag_btn.clicked.connect(lambda: self.batch_remove_tag_requested.emit(self._assets))

        replace_tag_btn = QPushButton("Replace tag in all…")
        replace_tag_btn.clicked.connect(lambda: self.batch_replace_tag_requested.emit(self._assets))

        self._move_btn = QPushButton("Move all…")
        self._move_btn.clicked.connect(lambda: self.batch_move_requested.emit(self._assets))
        self._move_btn.hide()

        dataset_btn = QPushButton("Add to dataset…")
        dataset_btn.clicked.connect(lambda: self.batch_add_to_dataset_requested.emit(self._assets))

        self._remove_from_dataset_btn = QPushButton("Remove from dataset")
        self._remove_from_dataset_btn.setStyleSheet("color: #c0392b;")
        self._remove_from_dataset_btn.clicked.connect(
            lambda: self.batch_remove_from_dataset_requested.emit(self._assets)
        )
        self._remove_from_dataset_btn.hide()

        delete_btn = QPushButton("Delete all…")
        delete_btn.setStyleSheet("color: #c0392b;")
        delete_btn.clicked.connect(lambda: self.batch_delete_requested.emit(self._assets))

        self._merge_btn = QPushButton("Merge…")
        self._merge_btn.clicked.connect(lambda: self.merge_two_requested.emit(self._assets))
        self._merge_btn.hide()

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.setContentsMargins(16, 24, 16, 16)
        layout.setSpacing(8)
        layout.addWidget(self._count_label)
        layout.addWidget(self._shard_label)
        layout.addWidget(tag_btn)
        layout.addWidget(remove_tag_btn)
        layout.addWidget(replace_tag_btn)
        layout.addWidget(self._move_btn)
        layout.addWidget(dataset_btn)
        layout.addWidget(self._remove_from_dataset_btn)
        layout.addWidget(delete_btn)
        layout.addWidget(self._merge_btn)
        layout.addStretch()

    def load_assets(self, assets: list[federation.AssetRow]) -> None:
        self._assets = assets
        n = len(assets)
        self._count_label.setText(f"<b>{n} assets selected</b>")
        roots = sorted({a.root for a in assets})
        self._shard_label.setText("across: " + ", ".join(roots))
        single_root = len(roots) == 1
        self._move_btn.show() if single_root else self._move_btn.hide()
        if n == 2 and single_root:
            self._merge_btn.show()
        else:
            self._merge_btn.hide()

    def set_active_dataset(self, name: Optional[str]) -> None:
        if name:
            self._remove_from_dataset_btn.setText(f'Remove all from "{name}"')
            self._remove_from_dataset_btn.show()
        else:
            self._remove_from_dataset_btn.hide()


# ---------------------------------------------------------------------------
# Public panel (stacked widget)
# ---------------------------------------------------------------------------

class DetailPanel(QWidget):
    """
    Stacks empty / single / multi views. The main window calls
    `load_selection(assets)` after each selection change.
    """

    # Forward all action signals so the main window can connect them once.
    favorite_changed = Signal(str, bool)
    rename_requested = Signal(str, str)
    move_requested = Signal(str, str)
    delete_requested = Signal(str, str)
    merge_requested = Signal(str)
    add_to_dataset_requested = Signal(str)               # asset_id
    remove_from_dataset_requested = Signal(str)          # asset_id
    tag_filter_requested = Signal(str)                   # tag_name
    tag_added = Signal(str, str, str)   # asset_id, name, type_name
    tag_removed = Signal(str, str)      # asset_id, tag_id
    tags_validated_changed = Signal(str, bool)          # asset_id, validated
    caption_saved = Signal(str, str, str)
    caption_deleted = Signal(str, str)
    caption_validated_changed = Signal(str, str, bool)  # asset_id, kind, validated
    import_from_caption_requested = Signal(str)  # asset_id
    batch_tag_requested = Signal(list)
    batch_remove_tag_requested = Signal(list)            # list[AssetRow]
    batch_replace_tag_requested = Signal(list)           # list[AssetRow]
    batch_move_requested = Signal(list)
    batch_delete_requested = Signal(list)
    batch_add_to_dataset_requested = Signal(list)        # list[AssetRow]
    batch_remove_from_dataset_requested = Signal(list)   # list[AssetRow]
    merge_two_requested = Signal(list)

    def __init__(
        self,
        bridge: QtDBBridge,
        thumb_bridge: QtThumbnailBridge,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._bridge = bridge
        self.setMinimumWidth(180)

        self._empty = QLabel("Select an asset\nto see details.")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setStyleSheet("color: gray; font-size: 13px;")

        self._single = _SingleDetail(bridge, thumb_bridge)
        self._single.favorite_changed.connect(self.favorite_changed)
        self._single.rename_requested.connect(self.rename_requested)
        self._single.move_requested.connect(self.move_requested)
        self._single.delete_requested.connect(self.delete_requested)
        self._single.merge_requested.connect(self.merge_requested)
        self._single.add_to_dataset_requested.connect(self.add_to_dataset_requested)
        self._single.remove_from_dataset_requested.connect(self.remove_from_dataset_requested)
        self._single.tag_filter_requested.connect(self.tag_filter_requested)
        self._single.tag_added.connect(self.tag_added)
        self._single.tag_removed.connect(self.tag_removed)
        self._single.tags_validated_changed.connect(self.tags_validated_changed)
        self._single.caption_saved.connect(self.caption_saved)
        self._single.caption_deleted.connect(self.caption_deleted)
        self._single.caption_validated_changed.connect(self.caption_validated_changed)
        self._single.import_from_caption_requested.connect(
            self.import_from_caption_requested
        )

        self._multi = _MultiDetail()
        self._multi.batch_tag_requested.connect(self.batch_tag_requested)
        self._multi.batch_remove_tag_requested.connect(self.batch_remove_tag_requested)
        self._multi.batch_replace_tag_requested.connect(self.batch_replace_tag_requested)
        self._multi.batch_move_requested.connect(self.batch_move_requested)
        self._multi.batch_delete_requested.connect(self.batch_delete_requested)
        self._multi.batch_add_to_dataset_requested.connect(self.batch_add_to_dataset_requested)
        self._multi.batch_remove_from_dataset_requested.connect(self.batch_remove_from_dataset_requested)
        self._multi.merge_two_requested.connect(self.merge_two_requested)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._empty)   # 0
        self._stack.addWidget(self._single)  # 1
        self._stack.addWidget(self._multi)   # 2

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._stack)

    def set_tag_suggestions(self, tags: list[tuple[str, str, int]]) -> None:
        self._single.set_tag_suggestions(tags)

    def set_active_dataset(self, name: Optional[str]) -> None:
        self._single.set_active_dataset(name)
        self._multi.set_active_dataset(name)

    def load_selection(
        self,
        assets: list[federation.AssetRow],
        fed: Optional[federation.Federation],
    ) -> None:
        if not assets:
            self._stack.setCurrentIndex(0)
            return
        if len(assets) == 1:
            asset = assets[0]
            root_abs = fed.shards[asset.root].abs_path if (
                fed and asset.root in fed.shards
            ) else ""
            self._single.load_asset(asset, root_abs)
            self._stack.setCurrentIndex(1)
        else:
            self._multi.load_assets(assets)
            self._stack.setCurrentIndex(2)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sep() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.Shape.HLine)
    f.setStyleSheet("color: #e0e0e0; margin: 4px 0;")
    return f


def _fmt_size(b: Optional[int]) -> str:
    if b is None:
        return "—"
    if b >= 1_048_576:
        return f"{b / 1_048_576:.1f} MB"
    if b >= 1024:
        return f"{b / 1024:.0f} KB"
    return f"{b} B"


def _fmt_dims(w: Optional[int], h: Optional[int]) -> str:
    if w is None or h is None:
        return "—"
    return f"{w} × {h}"


class _FlowLayout(QLayout):
    """Left-to-right wrapping layout for tag chips."""

    def __init__(self, h_spacing: int = 4, v_spacing: int = 4) -> None:
        super().__init__()
        self._h_space = h_spacing
        self._v_space = v_spacing
        self._items: list = []

    def addItem(self, item) -> None:
        self._items.append(item)
        self.invalidate()
        if self.parentWidget():
            self.parentWidget().updateGeometry()

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):
        item = self._items.pop(index) if 0 <= index < len(self._items) else None
        self.invalidate()
        if self.parentWidget():
            self.parentWidget().updateGeometry()
        return item

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        m = self.contentsMargins()
        return QSize(m.left() + m.right(), m.top() + m.bottom())

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        x, y = rect.x(), rect.y()
        line_height = 0
        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + self._h_space
            if next_x - self._h_space > rect.right() and line_height > 0:
                x = rect.x()
                y += line_height + self._v_space
                next_x = x + hint.width() + self._h_space
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y()


class _CountDelegate(QStyledItemDelegate):
    """Popup delegate that appends a grayed-out usage count to each tag name."""

    def __init__(self, counts: dict[str, int], parent=None) -> None:
        super().__init__(parent)
        self._counts = counts

    def paint(self, painter, option, index) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        name = opt.text
        count = self._counts.get(name, 0)
        opt.text = f"{name}  ({count})"
        QStyle.drawControl(
            QApplication.style(),
            QStyle.ControlElement.CE_ItemViewItem,
            opt,
            painter,
        )


class _TagInput(QWidget):
    tag_submitted = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._edit = QLineEdit()
        self._edit.setPlaceholderText("+ add tag")
        self._edit.returnPressed.connect(self._submit)
        self._completer = QCompleter()
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self._completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self._edit.setCompleter(self._completer)
        add_btn = QPushButton("+")
        add_btn.setFixedWidth(28)
        add_btn.clicked.connect(self._submit)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 4, 0, 0)
        row.addWidget(self._edit)
        row.addWidget(add_btn)

    def set_suggestions(self, tags: list[tuple[str, str, int]]) -> None:
        from PySide6.QtCore import QStringListModel
        counts = {name: count for name, _, count in tags}
        model = QStringListModel([name for name, *_ in tags], self._completer)
        self._completer.setModel(model)
        self._completer.popup().setItemDelegate(_CountDelegate(counts, self._completer.popup()))

    def focus(self) -> None:
        self._edit.setFocus()

    def _submit(self) -> None:
        name = self._edit.text().strip()
        if name:
            self.tag_submitted.emit(name)
            # Defer clear past QCompleter's activated→setText() hook.
            QTimer.singleShot(0, self._edit.clear)
