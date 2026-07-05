"""
TagManagementPanel — right-pane tab for browsing and bulk-editing tags.

Displays all tags used by assets matching the current filter, with columns
for tag name, category, and usage count.  Buttons operate on the selected row:

  Row 1 (global): Replace Tag, Delete Tag, Change Tag Type
  Row 2 (add):    Add to Selection, Add to Filtered
  Row 3 (remove): Remove from Selection, Remove from Filtered
  Row 4:          Add as Filter (adds a tag filter rule to the Browse panel)

Backed by a QAbstractTableModel + QSortFilterProxyModel (search) so a reload
updates only changed rows: when the tag *set* is unchanged (e.g. a count shifts
after "Add to Selection") the model emits dataChanged for the affected counts
and keeps selection/scroll, instead of clearing and repopulating the whole grid.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import (
    QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt, Signal,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QPushButton, QTableView, QVBoxLayout, QWidget,
)


class _TagTableModel(QAbstractTableModel):
    HEADERS = ["Tag", "Category", "Count"]

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._rows: list[tuple[str, str, int]] = []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 3

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        name, type_name, count = self._rows[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            return (name, type_name, count)[col]
        if role == Qt.ItemDataRole.TextAlignmentRole and col == 2:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    def tag_at(self, row: int) -> Optional[tuple[str, str]]:
        if 0 <= row < len(self._rows):
            return self._rows[row][0], self._rows[row][1]
        return None

    def set_tags(self, tags: list[tuple[str, str, int]]) -> None:
        new_by_key = {(n, t): c for n, t, c in tags}
        old_keys = {(r[0], r[1]) for r in self._rows}
        if set(new_by_key) == old_keys and len(new_by_key) == len(self._rows):
            # Same tag set — patch only the counts that moved.
            for i, (n, t, c) in enumerate(self._rows):
                nc = new_by_key[(n, t)]
                if nc != c:
                    self._rows[i] = (n, t, nc)
                    idx = self.index(i, 2)
                    self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DisplayRole])
        else:
            self.beginResetModel()
            self._rows = list(tags)
            self.endResetModel()


class _TagFilterProxy(QSortFilterProxyModel):
    """Case-insensitive substring match against the name and category columns."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._query = ""

    def set_query(self, text: str) -> None:
        self._query = text.strip().lower()
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        if not self._query:
            return True
        model = self.sourceModel()
        name = model.index(source_row, 0, source_parent).data() or ""
        type_name = model.index(source_row, 1, source_parent).data() or ""
        return self._query in name.lower() or self._query in type_name.lower()


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

        # Tag table — model/view with a search proxy.
        self._model = _TagTableModel(self)
        self._proxy = _TagFilterProxy(self)
        self._proxy.setSourceModel(self._model)
        self._table = QTableView()
        self._table.setModel(self._proxy)
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
        self._table.selectionModel().selectionChanged.connect(self._update_buttons)

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
        prev = self.selected_tag()
        self._model.set_tags(tags)
        if prev is not None and self.selected_tag() != prev:
            self._select_tag(prev)
        self._update_status()
        self._update_buttons()

    def selected_tag(self) -> Optional[tuple[str, str]]:
        """Return (name, type_name) of the selected row, or None."""
        idx = self._table.currentIndex()
        if not idx.isValid() or not self._table.selectionModel().hasSelection():
            return None
        return self._model.tag_at(self._proxy.mapToSource(idx).row())

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

    def _apply_search_filter(self) -> None:
        self._proxy.set_query(self._search_edit.text())
        self._update_status()
        self._update_buttons()

    def _select_tag(self, tag: tuple[str, str]) -> None:
        for proxy_row in range(self._proxy.rowCount()):
            src = self._proxy.mapToSource(self._proxy.index(proxy_row, 0))
            if self._model.tag_at(src.row()) == tag:
                self._table.selectRow(proxy_row)
                return

    def _update_status(self) -> None:
        n = len(self._all_tags)
        shown = self._proxy.rowCount()
        if self._search_edit.text().strip() and shown != n:
            self._status_label.setText(f"{shown} of {n} tag{'s' if n != 1 else ''} shown")
        else:
            self._status_label.setText(f"{n} tag{'s' if n != 1 else ''} in filtered set")

    def _update_buttons(self) -> None:
        enabled = self.selected_tag() is not None
        for btn in self._action_buttons:
            btn.setEnabled(enabled)

    def _emit(self, signal) -> None:
        tag = self.selected_tag()
        if tag:
            signal.emit(tag[0], tag[1])
