"""
QueryTab — SQL and FTS caption search interface.

Two modes selectable via radio buttons:
    SQL   — runs a user SELECT on the federation read connection
    FTS   — runs federation.search_captions across all shards

Results are shown in a QTableWidget. SQL errors are shown inline below
the editor. The default row limit matches the CLI default (20) but is
adjustable.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QFrame, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QPlainTextEdit, QPushButton, QRadioButton, QSizePolicy, QSpinBox,
    QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

import federation
from imgdb_worker_qt import QtDBBridge

DEFAULT_LIMIT = 20


class QueryTab(QWidget):
    """Full-width panel shown in Query mode."""

    save_as_dataset_requested = Signal(str)  # the raw SQL

    def __init__(
        self,
        bridge: QtDBBridge,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._bridge = bridge
        self._last_sql: Optional[str] = None

        # Mode radios
        self._sql_radio = QRadioButton("SQL Query")
        self._sql_radio.setChecked(True)
        self._fts_radio = QRadioButton("Caption FTS")
        self._sql_radio.toggled.connect(self._on_mode_changed)

        radio_row = QHBoxLayout()
        radio_row.addWidget(self._sql_radio)
        radio_row.addWidget(self._fts_radio)
        radio_row.addStretch()

        # -- SQL editor --
        self._sql_editor = QPlainTextEdit()
        self._sql_editor.setPlaceholderText(
            "SELECT _root, asset_id, rel_path, width, height\n"
            "FROM all_assets\n"
            "WHERE format = 'PNG'\n"
            "LIMIT 20"
        )
        self._sql_editor.setMinimumHeight(100)
        self._sql_editor.setMaximumHeight(200)
        font = self._sql_editor.font()
        font.setFamily("Monospace")
        self._sql_editor.setFont(font)

        self._limit_spin = QSpinBox()
        self._limit_spin.setRange(0, 100_000)
        self._limit_spin.setValue(DEFAULT_LIMIT)
        self._limit_spin.setSpecialValueText("No limit")
        self._limit_spin.setToolTip("Row limit (0 = no limit). Ignored if query already has LIMIT.")

        sql_run_btn = QPushButton("▶ Run")
        sql_run_btn.setFixedWidth(80)
        sql_run_btn.clicked.connect(self._run_sql)

        self._save_dataset_btn = QPushButton("Save as dataset…")
        self._save_dataset_btn.setEnabled(False)
        self._save_dataset_btn.clicked.connect(self._on_save_as_dataset)

        sql_ctrl_row = QHBoxLayout()
        sql_ctrl_row.addWidget(sql_run_btn)
        sql_ctrl_row.addWidget(QLabel("Limit:"))
        sql_ctrl_row.addWidget(self._limit_spin)
        sql_ctrl_row.addWidget(self._save_dataset_btn)
        sql_ctrl_row.addStretch()

        self._sql_error = QLabel()
        self._sql_error.setStyleSheet("color: #c0392b; font-family: monospace;")
        self._sql_error.setWordWrap(True)
        self._sql_error.hide()

        sql_panel = QWidget()
        sp = QVBoxLayout(sql_panel)
        sp.setContentsMargins(0, 0, 0, 0)
        sp.addWidget(self._sql_editor)
        sp.addLayout(sql_ctrl_row)
        sp.addWidget(self._sql_error)

        # -- FTS editor --
        self._fts_edit = QLineEdit()
        self._fts_edit.setPlaceholderText('mountain AND sunset   |   "golden hour"   |   land*')
        self._fts_edit.returnPressed.connect(self._run_fts)

        fts_run_btn = QPushButton("Search")
        fts_run_btn.clicked.connect(self._run_fts)

        self._fts_error = QLabel()
        self._fts_error.setStyleSheet("color: #c0392b;")
        self._fts_error.hide()

        fts_input_row = QHBoxLayout()
        fts_input_row.addWidget(QLabel("Expression:"))
        fts_input_row.addWidget(self._fts_edit, stretch=1)
        fts_input_row.addWidget(fts_run_btn)

        fts_panel = QWidget()
        fp = QVBoxLayout(fts_panel)
        fp.setContentsMargins(0, 0, 0, 0)
        fp.addLayout(fts_input_row)
        fp.addWidget(self._fts_error)

        # Stacked input area
        self._input_stack = QStackedWidget()
        self._input_stack.addWidget(sql_panel)   # 0 = SQL
        self._input_stack.addWidget(fts_panel)   # 1 = FTS

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #ddd;")

        # Results
        self._result_label = QLabel("No results.")
        self._result_label.setStyleSheet("color: gray; font-size: 12px;")

        self._result_table = QTableWidget()
        self._result_table.setAlternatingRowColors(True)
        self._result_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._result_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._result_table.horizontalHeader().setStretchLastSection(True)
        self._result_table.verticalHeader().setVisible(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        layout.addLayout(radio_row)
        layout.addWidget(self._input_stack)
        layout.addWidget(sep)
        layout.addWidget(self._result_label)
        layout.addWidget(self._result_table, stretch=1)

    # -- private -----------------------------------------------------------

    def _on_mode_changed(self, sql_checked: bool) -> None:
        self._input_stack.setCurrentIndex(0 if sql_checked else 1)
        self._sql_error.hide()
        self._fts_error.hide()

    def _run_sql(self) -> None:
        self._sql_error.hide()
        raw_sql = self._sql_editor.toPlainText().strip()
        if not raw_sql:
            return

        limit = self._limit_spin.value()
        sql = _apply_limit(raw_sql, limit)

        def on_result(data) -> None:
            cols, rows = data
            self._populate_table(cols, rows)
            self._result_label.setText(f"{len(rows)} row{'s' if len(rows) != 1 else ''}.")
            has_asset_id = any(c.lower() == "asset_id" for c in cols)
            self._save_dataset_btn.setEnabled(has_asset_id)
            self._last_sql = raw_sql

        def on_error(exc: BaseException) -> None:
            self._sql_error.setText(f"SQL error: {exc}")
            self._sql_error.show()

        def run(fed: federation.Federation):
            return federation.run_user_query(fed, sql)

        self._bridge.submit(run, on_result=on_result, on_error=on_error)

    def _on_save_as_dataset(self) -> None:
        if self._last_sql:
            self.save_as_dataset_requested.emit(self._last_sql)

    def _run_fts(self) -> None:
        self._fts_error.hide()
        expr = self._fts_edit.text().strip()
        if not expr:
            return

        def on_result(results: list) -> None:
            cols = ["Root", "Asset ID", "Kind", "Content"]
            rows = [(r[0], r[1], r[2], r[3]) for r in results]
            self._populate_table(cols, rows)
            self._result_label.setText(f"{len(rows)} match{'es' if len(rows) != 1 else ''}.")

        def on_error(exc: BaseException) -> None:
            self._fts_error.setText(f"FTS error: {exc}")
            self._fts_error.show()

        self._bridge.submit(
            federation.search_captions,
            expr,
            on_result=on_result,
            on_error=on_error,
        )

    def _populate_table(self, cols: list[str], rows: list[tuple]) -> None:
        self._result_table.clear()
        self._result_table.setColumnCount(len(cols))
        self._result_table.setRowCount(len(rows))
        self._result_table.setHorizontalHeaderLabels(cols)
        for r_idx, row in enumerate(rows):
            for c_idx, val in enumerate(row):
                item = QTableWidgetItem(str(val) if val is not None else "")
                self._result_table.setItem(r_idx, c_idx, item)
        self._result_table.resizeColumnsToContents()


def _apply_limit(sql: str, limit: int) -> str:
    """Wrap sql with LIMIT if it doesn't already have one and limit > 0."""
    if limit == 0:
        return sql
    lower = sql.lower()
    if "limit" in lower:
        return sql
    return f"SELECT * FROM ({sql}) _q LIMIT {limit}"
