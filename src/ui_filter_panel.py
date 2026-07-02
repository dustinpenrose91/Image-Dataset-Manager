"""
FilterPanel — left-panel Browse tab.

Sections:
  FILTERS — a list of FilterRule rows (field selector, operator, value, ✕)
  SORT    — a list of SortRule rows (field selector, ↑↓ toggle, ✕)
  DATASETS — checkboxes; checking one adds a 'dataset is_in' rule automatically

All state is ephemeral (never persisted). The panel emits filter_changed
whenever any rule or dataset checkbox changes.

Public API:
    current_filter_rules() -> list[FilterRule]
    current_sort_rules()   -> list[SortRule]
    add_filter_rule(rule)  — append a rule (e.g. from "Add as Filter" in tag panel)
    set_datasets(names)    — rebuild dataset checkbox list
    set_tag_names(names)   — update tag autocomplete in existing/future tag rows
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton, QScrollArea,
    QSizePolicy, QSplitter, QVBoxLayout, QWidget,
)

from filter_model import (
    DTYPE_OPS, FILTER_FIELDS, SORTABLE_FIELDS,
    FilterRule, SortRule,
)


# ---------------------------------------------------------------------------
# Single filter-rule row (normal field-based rules)
# ---------------------------------------------------------------------------

class _FilterRow(QWidget):
    """One editable filter rule (field | op | value | ✕)."""

    changed = Signal()
    remove_requested = Signal(object)   # self

    def __init__(
        self,
        rule: FilterRule,
        tag_names: list[str],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._tag_names = tag_names
        self._is_sql = (rule.field_id == "sql")

        rm_btn = QPushButton("✕")
        rm_btn.setFixedWidth(24)
        rm_btn.setFlat(True)
        rm_btn.setStyleSheet("color: #888; font-size: 13px;")
        rm_btn.clicked.connect(lambda: self.remove_requested.emit(self))

        if self._is_sql:
            # SQL rules show just the clause text — no dropdowns.
            self._sql_clause = rule.value
            self._field_cb = None
            self._op_cb = None
            self._value_edit = None

            lbl = QLabel(rule.value)
            lbl.setStyleSheet(
                "font-family: monospace; font-size: 11px; color: #444;"
                " padding: 2px 4px; background: #f5f5f5; border-radius: 2px;"
            )
            lbl.setWordWrap(True)
            lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

            row = QHBoxLayout(self)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(4)
            row.addWidget(lbl)
            row.addWidget(rm_btn)
        else:
            self._sql_clause = ""
            self._field_cb = QComboBox()
            # Exclude "sql" — it's entered via the dedicated SQL text area.
            for fid, ff in FILTER_FIELDS.items():
                if fid == "sql":
                    continue
                self._field_cb.addItem(ff.display_name, fid)
            idx = self._field_cb.findData(rule.field_id)
            if idx >= 0:
                self._field_cb.setCurrentIndex(idx)

            self._op_cb = QComboBox()
            self._op_cb.setMinimumWidth(110)

            self._value_edit = QLineEdit()
            self._value_edit.setText(rule.value)
            self._value_edit.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
            )

            row = QHBoxLayout(self)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(4)
            row.addWidget(self._field_cb)
            row.addWidget(self._op_cb)
            row.addWidget(self._value_edit)
            row.addWidget(rm_btn)

            self._field_cb.currentIndexChanged.connect(self._on_field_changed)
            self._op_cb.currentIndexChanged.connect(self.changed)
            self._value_edit.textChanged.connect(self.changed)

            self._on_field_changed()
            op_idx = self._op_cb.findData(rule.op)
            if op_idx >= 0:
                self._op_cb.setCurrentIndex(op_idx)

    def rule(self) -> FilterRule:
        if self._is_sql:
            return FilterRule("sql", "sql", self._sql_clause)
        fid = self._field_cb.currentData() or ""
        op = self._op_cb.currentData() or ""
        val = self._value_edit.text()
        return FilterRule(fid, op, val)

    def set_tag_names(self, names: list[str]) -> None:
        if self._is_sql:
            return
        self._tag_names = names
        if self._current_dtype() == "tag":
            self._install_tag_completer()

    def _current_dtype(self) -> str:
        fid = self._field_cb.currentData() or ""
        return FILTER_FIELDS[fid].dtype if fid in FILTER_FIELDS else "text"

    def _on_field_changed(self) -> None:
        dtype = self._current_dtype()
        ops = DTYPE_OPS.get(dtype, [])

        self._op_cb.blockSignals(True)
        self._op_cb.clear()
        for op_id, label in ops:
            self._op_cb.addItem(label, op_id)
        self._op_cb.blockSignals(False)

        has_value = dtype not in ("boolean",)
        self._value_edit.setVisible(has_value)

        if dtype == "tag":
            self._install_tag_completer()
        else:
            self._value_edit.setCompleter(None)

        self.changed.emit()

    def _install_tag_completer(self) -> None:
        from PySide6.QtWidgets import QCompleter
        from PySide6.QtCore import Qt
        c = QCompleter(self._tag_names, self._value_edit)
        c.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        c.setFilterMode(Qt.MatchFlag.MatchContains)
        self._value_edit.setCompleter(c)


# ---------------------------------------------------------------------------
# Single sort-rule row
# ---------------------------------------------------------------------------

class _SortRow(QWidget):
    """One sort level (field | ↑/↓ | ✕)."""

    changed = Signal()
    remove_requested = Signal(object)   # self

    def __init__(
        self,
        rule: SortRule,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)

        self._field_cb = QComboBox()
        for fid, sf in SORTABLE_FIELDS.items():
            # Show both display name and SQL column name so users can reference
            # the column in custom SQL filters.
            self._field_cb.addItem(f"{sf.display_name}  ({sf.sql_col})", fid)
        idx = self._field_cb.findData(rule.field_id)
        if idx >= 0:
            self._field_cb.setCurrentIndex(idx)

        self._dir_btn = QPushButton("↓ Desc" if rule.desc else "↑ Asc")
        self._dir_btn.setCheckable(True)
        self._dir_btn.setChecked(rule.desc)
        self._dir_btn.setFixedWidth(68)
        self._dir_btn.toggled.connect(self._on_dir_toggled)

        rm_btn = QPushButton("✕")
        rm_btn.setFixedWidth(24)
        rm_btn.setFlat(True)
        rm_btn.setStyleSheet("color: #888; font-size: 13px;")
        rm_btn.clicked.connect(lambda: self.remove_requested.emit(self))

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        row.addWidget(self._field_cb)
        row.addWidget(self._dir_btn)
        row.addWidget(rm_btn)

        self._field_cb.currentIndexChanged.connect(self.changed)

    def rule(self) -> SortRule:
        fid = self._field_cb.currentData() or "rel_path"
        return SortRule(fid, self._dir_btn.isChecked())

    def _on_dir_toggled(self, checked: bool) -> None:
        self._dir_btn.setText("↓ Desc" if checked else "↑ Asc")
        self.changed.emit()


# ---------------------------------------------------------------------------
# FilterPanel
# ---------------------------------------------------------------------------

class FilterPanel(QWidget):
    """
    Browse-tab left panel.

    Signals:
        filter_changed  — emitted whenever any filter or sort rule changes,
                          or a dataset checkbox toggles.
    """

    filter_changed = Signal()
    rename_dataset_requested = Signal(str)   # dataset name
    delete_dataset_requested = Signal(str)   # dataset name

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(180)

        self._tag_names: list[str] = []
        self._filter_rows: list[_FilterRow] = []
        self._sort_rows: list[_SortRow] = []

        # Coalesce value-edit keystrokes: each fires row.changed, but a full
        # model reset per keystroke stalls the UI at 100k+ rows. Structural
        # changes (add/remove/clear rule, dataset toggle) bypass this and emit
        # immediately via _emit_now, which also cancels any pending keystroke.
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(250)
        self._debounce.timeout.connect(self.filter_changed)

        # ── Top half: FILTERS + SORT (scrollable) ────────────────────────
        filters_label = QLabel("<b>FILTERS</b>")
        filters_label.setContentsMargins(4, 6, 4, 2)

        self._filter_body = QWidget()
        self._filter_layout = QVBoxLayout(self._filter_body)
        self._filter_layout.setContentsMargins(0, 0, 0, 0)
        self._filter_layout.setSpacing(3)

        # Custom SQL entry area — a dedicated text box + commit button.
        sql_entry_widget = QWidget()
        sql_entry_layout = QVBoxLayout(sql_entry_widget)
        sql_entry_layout.setContentsMargins(4, 4, 4, 2)
        sql_entry_layout.setSpacing(3)

        sql_header_row = QHBoxLayout()
        sql_header_row.setContentsMargins(0, 0, 0, 0)
        sql_lbl = QLabel("Custom SQL:")
        sql_lbl.setStyleSheet("color: #555; font-size: 11px;")
        self._sql_add_btn = QPushButton("+")
        self._sql_add_btn.setFixedWidth(28)
        self._sql_add_btn.setToolTip("Add WHERE clause as filter")
        self._sql_add_btn.clicked.connect(self._on_commit_sql)
        sql_header_row.addWidget(sql_lbl)
        sql_header_row.addStretch()
        sql_header_row.addWidget(self._sql_add_btn)

        self._sql_edit = QPlainTextEdit()
        self._sql_edit.setPlaceholderText("a.width > 1000 AND a.format = 'PNG'")
        self._sql_edit.setFixedHeight(72)
        self._sql_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self._sql_edit.setStyleSheet(
            "font-family: monospace; font-size: 11px;"
            " background: #fafafa; border: 1px solid #ccc; border-radius: 2px;"
        )

        sql_entry_layout.addLayout(sql_header_row)
        sql_entry_layout.addWidget(self._sql_edit)

        add_filter_btn = QPushButton("+ Add filter")
        add_filter_btn.setFlat(True)
        add_filter_btn.setStyleSheet("color: #2980b9; text-align: left; padding: 2px 4px;")
        add_filter_btn.clicked.connect(self._add_default_filter)

        clear_filters_btn = QPushButton("Clear all")
        clear_filters_btn.setFlat(True)
        clear_filters_btn.setStyleSheet("color: #888; font-size: 11px;")
        clear_filters_btn.clicked.connect(self._clear_filters)

        filter_btns = QHBoxLayout()
        filter_btns.setContentsMargins(4, 0, 4, 0)
        filter_btns.addWidget(add_filter_btn)
        filter_btns.addStretch()
        filter_btns.addWidget(clear_filters_btn)

        sep1 = _hline()
        sort_label = QLabel("<b>SORT</b>")
        sort_label.setContentsMargins(4, 6, 4, 2)

        self._sort_body = QWidget()
        self._sort_layout = QVBoxLayout(self._sort_body)
        self._sort_layout.setContentsMargins(0, 0, 0, 0)
        self._sort_layout.setSpacing(3)

        add_sort_btn = QPushButton("+ Add sort level")
        add_sort_btn.setFlat(True)
        add_sort_btn.setStyleSheet("color: #2980b9; text-align: left; padding: 2px 4px;")
        add_sort_btn.clicked.connect(self._add_default_sort)

        sort_btns = QHBoxLayout()
        sort_btns.setContentsMargins(4, 0, 4, 0)
        sort_btns.addWidget(add_sort_btn)
        sort_btns.addStretch()

        top_inner = QWidget()
        top_layout = QVBoxLayout(top_inner)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(0)
        top_layout.addWidget(filters_label)
        top_layout.addWidget(self._filter_body)
        top_layout.addWidget(sql_entry_widget)
        top_layout.addLayout(filter_btns)
        top_layout.addWidget(sep1)
        top_layout.addWidget(sort_label)
        top_layout.addWidget(self._sort_body)
        top_layout.addLayout(sort_btns)
        top_layout.addStretch()

        top_scroll = QScrollArea()
        top_scroll.setWidgetResizable(True)
        top_scroll.setWidget(top_inner)
        top_scroll.setFrameShape(QFrame.Shape.NoFrame)
        top_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        # ── Bottom half: DATASETS ────────────────────────────────────────
        datasets_label = QLabel("<b>DATASETS</b>")
        datasets_label.setContentsMargins(4, 6, 4, 2)

        self._datasets_list = QListWidget()
        self._datasets_list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self._datasets_list.setFrameShape(QFrame.Shape.NoFrame)
        self._datasets_list.itemChanged.connect(self._on_dataset_item_changed)
        self._datasets_list.itemSelectionChanged.connect(
            self._on_dataset_selection_changed)

        self._ds_rename_btn = QPushButton("Rename…")
        self._ds_rename_btn.setEnabled(False)
        self._ds_delete_btn = QPushButton("Delete")
        self._ds_delete_btn.setEnabled(False)
        self._ds_delete_btn.setStyleSheet("color: #c0392b;")
        self._ds_rename_btn.clicked.connect(self._on_ds_rename)
        self._ds_delete_btn.clicked.connect(self._on_ds_delete)

        ds_btn_row = QHBoxLayout()
        ds_btn_row.setContentsMargins(4, 2, 4, 4)
        ds_btn_row.setSpacing(4)
        ds_btn_row.addWidget(self._ds_rename_btn)
        ds_btn_row.addWidget(self._ds_delete_btn)
        ds_btn_row.addStretch()

        bot_widget = QWidget()
        bot_layout = QVBoxLayout(bot_widget)
        bot_layout.setContentsMargins(0, 0, 0, 0)
        bot_layout.setSpacing(0)
        bot_layout.addWidget(datasets_label)
        bot_layout.addWidget(self._datasets_list, stretch=1)
        bot_layout.addLayout(ds_btn_row)

        # ── Splitter: top (filters+sort) above bottom (datasets) ─────────
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(1)
        splitter.setStyleSheet(
            "QSplitter::handle:vertical {"
            "  background: #d0d0d0;"
            "  border-top: 1px solid #b8b8b8;"
            "}"
        )
        splitter.addWidget(top_scroll)
        splitter.addWidget(bot_widget)
        splitter.setSizes([1000, 1000])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(splitter)

        # Default rules: hide missing files + sort by path.
        self._add_filter_row(FilterRule("exists_flag", "is_true", ""))
        self._add_sort_row(SortRule("rel_path", False))

    # -- public API -----------------------------------------------------------

    def current_filter_rules(self) -> list[FilterRule]:
        """Return all active filter rules (explicit rows + checked datasets)."""
        rules = [row.rule() for row in self._filter_rows]
        for name in self.checked_dataset_names():
            rules.append(FilterRule("dataset", "is_in", name))
        return rules

    def current_sort_rules(self) -> list[SortRule]:
        return [row.rule() for row in self._sort_rows]

    def add_filter_rule(self, rule: FilterRule) -> None:
        """Append a rule programmatically (e.g. from 'Add as Filter' in tag panel)."""
        self._add_filter_row(rule)
        self._emit_now()

    def set_tag_names(self, names: list[str]) -> None:
        self._tag_names = names
        for row in self._filter_rows:
            row.set_tag_names(names)

    def set_datasets(self, names: list[str]) -> None:
        """Rebuild dataset list. Preserves checked and selected state by name."""
        checked = set(self.checked_dataset_names())
        selected = self._selected_dataset_name()

        self._datasets_list.blockSignals(True)
        self._datasets_list.clear()
        for name in names:
            item = QListWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            state = Qt.CheckState.Checked if name in checked else Qt.CheckState.Unchecked
            item.setCheckState(state)
            self._datasets_list.addItem(item)
            if name == selected:
                item.setSelected(True)
        self._datasets_list.blockSignals(False)

        self._on_dataset_selection_changed()

    def checked_dataset_names(self) -> list[str]:
        result = []
        for i in range(self._datasets_list.count()):
            item = self._datasets_list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                result.append(item.data(Qt.ItemDataRole.UserRole))
        return result

    # -- private: emit policy ------------------------------------------------

    def _emit_now(self) -> None:
        """Structural change — emit immediately and drop any pending keystroke."""
        self._debounce.stop()
        self.filter_changed.emit()

    def _emit_debounced(self) -> None:
        """Value-edit change — coalesce bursts into one emit after the interval."""
        self._debounce.start()

    # -- private: filter rows ------------------------------------------------

    def _add_default_filter(self) -> None:
        self._add_filter_row(FilterRule("tag", "has", ""))
        self._emit_now()

    def _add_filter_row(self, rule: FilterRule) -> None:
        row = _FilterRow(rule, self._tag_names, self._filter_body)
        row.changed.connect(self._emit_debounced)
        row.remove_requested.connect(self._remove_filter_row)
        self._filter_layout.addWidget(row)
        self._filter_rows.append(row)

    def _remove_filter_row(self, row: _FilterRow) -> None:
        if row in self._filter_rows:
            self._filter_rows.remove(row)
        self._filter_layout.removeWidget(row)
        row.deleteLater()
        self._emit_now()

    def _clear_filters(self) -> None:
        for row in list(self._filter_rows):
            self._filter_layout.removeWidget(row)
            row.deleteLater()
        self._filter_rows.clear()
        self._datasets_list.blockSignals(True)
        for i in range(self._datasets_list.count()):
            self._datasets_list.item(i).setCheckState(Qt.CheckState.Unchecked)
        self._datasets_list.blockSignals(False)
        self._emit_now()

    def _on_commit_sql(self) -> None:
        clause = self._sql_edit.toPlainText().strip()
        if not clause:
            return
        self._add_filter_row(FilterRule("sql", "sql", clause))
        self._sql_edit.clear()
        self._emit_now()

    # -- private: sort rows --------------------------------------------------

    def _add_default_sort(self) -> None:
        self._add_sort_row(SortRule("rel_path", False))
        self._emit_now()

    def _add_sort_row(self, rule: SortRule) -> None:
        row = _SortRow(rule, self._sort_body)
        row.changed.connect(self._emit_debounced)
        row.remove_requested.connect(self._remove_sort_row)
        self._sort_layout.addWidget(row)
        self._sort_rows.append(row)

    def _remove_sort_row(self, row: _SortRow) -> None:
        if row in self._sort_rows:
            self._sort_rows.remove(row)
        self._sort_layout.removeWidget(row)
        row.deleteLater()
        self._emit_now()

    # -- private: dataset management -----------------------------------------

    def _selected_dataset_name(self) -> Optional[str]:
        items = self._datasets_list.selectedItems()
        return items[0].data(Qt.ItemDataRole.UserRole) if items else None

    def _on_dataset_item_changed(self, _item: QListWidgetItem) -> None:
        self._emit_now()

    def _on_dataset_selection_changed(self) -> None:
        enabled = self._selected_dataset_name() is not None
        self._ds_rename_btn.setEnabled(enabled)
        self._ds_delete_btn.setEnabled(enabled)

    def _on_ds_rename(self) -> None:
        name = self._selected_dataset_name()
        if name:
            self.rename_dataset_requested.emit(name)

    def _on_ds_delete(self) -> None:
        name = self._selected_dataset_name()
        if name:
            self.delete_dataset_requested.emit(name)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _hline() -> QFrame:
    sep = QFrame()
    sep.setFrameShape(QFrame.Shape.HLine)
    sep.setStyleSheet("color: #ddd;")
    return sep
