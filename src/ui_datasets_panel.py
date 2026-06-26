"""
DatasetsPanel — left sidebar section listing known datasets with member counts.

A dataset exists only in shards where it has members, so the panel shows
the union across currently-attached shards. Counts reflect only attached shards.

Signals:
    dataset_filter_changed(name: str | None)  — None means "no dataset filter"
    delete_dataset_requested(name: str)
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QVBoxLayout, QWidget,
)

import federation


class DatasetsPanel(QWidget):

    dataset_filter_changed = Signal(object)   # str or None
    rename_dataset_requested = Signal(str)    # current name
    delete_dataset_requested = Signal(str)    # dataset name

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(120)

        header = QLabel("<b>DATASETS</b>")
        header.setContentsMargins(6, 6, 6, 2)

        self._list = QListWidget()
        self._list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self._list.setFrameShape(QFrame.Shape.NoFrame)
        self._list.itemSelectionChanged.connect(self._on_selection_changed)
        self._list.itemClicked.connect(self._on_item_clicked)
        self._last_clicked: Optional[str] = None

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(4, 2, 4, 2)
        btn_row.setSpacing(4)

        self._rename_btn = QPushButton("Rename…")
        self._rename_btn.setEnabled(False)
        self._rename_btn.clicked.connect(self._on_rename_clicked)

        self._delete_btn = QPushButton("Delete")
        self._delete_btn.setEnabled(False)
        self._delete_btn.setStyleSheet("color: #c0392b;")
        self._delete_btn.clicked.connect(self._on_delete_clicked)

        btn_row.addWidget(self._rename_btn)
        btn_row.addWidget(self._delete_btn)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #ddd;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(sep)
        layout.addWidget(header)
        layout.addWidget(self._list, stretch=1)
        layout.addLayout(btn_row)

    # -- public API ----------------------------------------------------------

    def load_datasets(self, datasets: list[federation.DatasetInfo]) -> None:
        """Rebuild the list. Preserves current selection by name if possible."""
        current_name = self._selected_name()
        self._list.blockSignals(True)
        self._list.clear()
        for ds in datasets:
            text = f"{ds.name}  ({ds.total_count})"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, ds.name)
            tooltip_lines = [f"{lbl}: {cnt}" for lbl, cnt in ds.shard_counts.items()]
            if ds.description:
                tooltip_lines.insert(0, ds.description)
            item.setToolTip("\n".join(tooltip_lines))
            self._list.addItem(item)
            if ds.name == current_name:
                item.setSelected(True)
        self._list.blockSignals(False)
        self._delete_btn.setEnabled(bool(self._selected_name()))

    def clear_filter(self) -> None:
        self._list.clearSelection()

    def selected_dataset(self) -> Optional[str]:
        return self._selected_name()

    # -- private -------------------------------------------------------------

    def _selected_name(self) -> Optional[str]:
        items = self._list.selectedItems()
        return items[0].data(Qt.ItemDataRole.UserRole) if items else None

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        name = item.data(Qt.ItemDataRole.UserRole)
        if name == self._last_clicked:
            self._list.clearSelection()
            self._last_clicked = None
        else:
            self._last_clicked = name

    def _on_selection_changed(self) -> None:
        name = self._selected_name()
        if name is None:
            self._last_clicked = None
        self._rename_btn.setEnabled(name is not None)
        self._delete_btn.setEnabled(name is not None)
        self.dataset_filter_changed.emit(name)

    def _on_rename_clicked(self) -> None:
        name = self._selected_name()
        if name:
            self.rename_dataset_requested.emit(name)

    def _on_delete_clicked(self) -> None:
        name = self._selected_name()
        if name:
            self.delete_dataset_requested.emit(name)
