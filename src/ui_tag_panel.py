"""
TagManagementPanel — right-pane tab for browsing and bulk-editing tags.

Displays all tags used by assets matching the current filter, with columns
for tag name, category, and usage count.  Buttons operate on the selected row:

  Row 1 (global): Replace Tag, Delete Tag, Change Tag Type
  Row 2 (add):    Add to Selection, Add to Filtered
  Row 3 (remove): Remove from Selection, Remove from Filtered
  Row 4:          Add as Filter (adds a tag filter rule to the Browse panel)
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)


class TagManagementPanel(QWidget):

    replace_tag_requested           = Signal(str, str)  # name, type_name
    delete_tag_requested            = Signal(str, str)  # name, type_name
    change_type_requested           = Signal(str, str)  # name, type_name
    add_to_selection_requested      = Signal(str, str)  # name, type_name
    add_to_filtered_requested       = Signal(str, str)  # name, type_name
    remove_from_selection_requested = Signal(str, str)  # name, type_name
    remove_from_filtered_requested  = Signal(str, str)  # name, type_name
    add_as_filter_requested         = Signal(str, str)  # name, type_name

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._all_tags: list[tuple[str, str, int]] = []

        # Search field
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("Search tags…")
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.textChanged.connect(self._apply_search_filter)

        # Tag table
        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(["Tag", "Category", "Count"])
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        hdr.resizeSection(0, 200)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        hdr.resizeSection(1, 100)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSortingEnabled(True)
        self._table.sortByColumn(2, Qt.SortOrder.DescendingOrder)
        self._table.verticalHeader().setVisible(False)
        self._table.itemSelectionChanged.connect(self._update_buttons)

        self._status_label = QLabel()
        self._status_label.setStyleSheet("color: gray; font-size: 11px;")

        # Row 1 — global tag operations
        self._replace_btn     = QPushButton("Replace Tag")
        self._delete_btn      = QPushButton("Delete Tag")
        self._delete_btn.setStyleSheet("color: #c0392b;")
        self._change_type_btn = QPushButton("Change Tag Type")

        # Row 2 — add tag to asset sets
        self._add_sel_btn      = QPushButton("Add to Selection")
        self._add_filtered_btn = QPushButton("Add to Filtered")

        # Row 3 — remove tag from asset sets
        self._rm_sel_btn      = QPushButton("Remove from Selection")
        self._rm_filtered_btn = QPushButton("Remove from Filtered")

        # Row 4 — add as filter in browse panel
        self._add_filter_btn = QPushButton("Add as Filter")

        self._action_buttons = [
            self._replace_btn, self._delete_btn, self._change_type_btn,
            self._add_sel_btn, self._add_filtered_btn,
            self._rm_sel_btn, self._rm_filtered_btn,
            self._add_filter_btn,
        ]
        for btn in self._action_buttons:
            btn.setEnabled(False)

        self._replace_btn.clicked.connect(
            lambda: self._emit(self.replace_tag_requested))
        self._delete_btn.clicked.connect(
            lambda: self._emit(self.delete_tag_requested))
        self._change_type_btn.clicked.connect(
            lambda: self._emit(self.change_type_requested))
        self._add_sel_btn.clicked.connect(
            lambda: self._emit(self.add_to_selection_requested))
        self._add_filtered_btn.clicked.connect(
            lambda: self._emit(self.add_to_filtered_requested))
        self._rm_sel_btn.clicked.connect(
            lambda: self._emit(self.remove_from_selection_requested))
        self._rm_filtered_btn.clicked.connect(
            lambda: self._emit(self.remove_from_filtered_requested))
        self._add_filter_btn.clicked.connect(
            lambda: self._emit(self.add_as_filter_requested))

        row1 = QHBoxLayout()
        row1.setSpacing(4)
        row1.addWidget(self._replace_btn)
        row1.addWidget(self._delete_btn)
        row1.addWidget(self._change_type_btn)

        row2 = QHBoxLayout()
        row2.setSpacing(4)
        row2.addWidget(self._add_sel_btn)
        row2.addWidget(self._add_filtered_btn)

        row3 = QHBoxLayout()
        row3.setSpacing(4)
        row3.addWidget(self._rm_sel_btn)
        row3.addWidget(self._rm_filtered_btn)

        row4 = QHBoxLayout()
        row4.setSpacing(4)
        row4.addWidget(self._add_filter_btn)
        row4.addStretch()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        layout.addWidget(self._search_edit)
        layout.addWidget(self._table, stretch=1)
        layout.addWidget(self._status_label)
        layout.addLayout(row1)
        layout.addLayout(row2)
        layout.addLayout(row3)
        layout.addLayout(row4)

    # -- public API -----------------------------------------------------------

    def load_tags(self, tags: list[tuple[str, str, int]]) -> None:
        """Populate the table. tags: [(name, type_name, count)]"""
        self._all_tags = tags
        self._apply_search_filter()

    def _apply_search_filter(self) -> None:
        query = self._search_edit.text().strip().lower()
        tags = self._all_tags if not query else [
            t for t in self._all_tags
            if query in t[0].lower() or query in t[1].lower()
        ]

        prev = self.selected_tag()
        self._table.setSortingEnabled(False)
        self._table.setRowCount(len(tags))
        restore_row = -1
        for i, (name, type_name, count) in enumerate(tags):
            name_item = QTableWidgetItem(name)
            type_item = QTableWidgetItem(type_name)
            count_item = QTableWidgetItem()
            count_item.setData(Qt.ItemDataRole.DisplayRole, count)
            count_item.setTextAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            self._table.setItem(i, 0, name_item)
            self._table.setItem(i, 1, type_item)
            self._table.setItem(i, 2, count_item)
            if prev and (name, type_name) == prev:
                restore_row = i
        self._table.setSortingEnabled(True)

        if restore_row >= 0:
            self._table.selectRow(restore_row)

        n = len(self._all_tags)
        shown = len(tags)
        if query and shown != n:
            self._status_label.setText(f"{shown} of {n} tag{'s' if n != 1 else ''} shown")
        else:
            self._status_label.setText(f"{n} tag{'s' if n != 1 else ''} in filtered set")
        self._update_buttons()

    def selected_tag(self) -> Optional[tuple[str, str]]:
        """Return (name, type_name) of the selected row, or None."""
        row = self._table.currentRow()
        if row < 0 or not self._table.selectedItems():
            return None
        name_item = self._table.item(row, 0)
        type_item = self._table.item(row, 1)
        if name_item is None or type_item is None:
            return None
        return name_item.text(), type_item.text()

    def save_header_state(self) -> bytes:
        return bytes(self._table.horizontalHeader().saveState())

    def restore_header_state(self, state: bytes) -> None:
        hdr = self._table.horizontalHeader()
        hdr.restoreState(state)
        # restoreState overwrites resize modes — re-apply ours.
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)

    # -- private --------------------------------------------------------------

    def _update_buttons(self) -> None:
        enabled = self.selected_tag() is not None
        for btn in self._action_buttons:
            btn.setEnabled(enabled)

    def _emit(self, signal) -> None:
        tag = self.selected_tag()
        if tag:
            signal.emit(tag[0], tag[1])
