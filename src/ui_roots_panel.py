"""
RootsPanel — left sidebar showing attached roots, per-root scan controls,
and inline live scan progress.

Signals emitted (connect from the main window):
    roots_changed()       — config changed; caller should reload the federation
    filter_changed(labels: list[str])  — checked root set changed
"""
from __future__ import annotations

import threading
from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

import imgdb

import federation
import imgdb_worker
from imgdb_worker import DBWorker
from imgdb_worker_qt import QtDBBridge
from ui_dialogs import AttachRootDialog, DeleteRootDialog


# ---------------------------------------------------------------------------
# Per-root widget
# ---------------------------------------------------------------------------

class RootEntry(QWidget):
    """
    Displays one attached root with:
    - A checkbox-style label (click to toggle visibility in asset list)
    - A Scan / Stop button
    - Live scan counters while scanning
    """

    scan_requested = Signal(str)        # label
    detach_requested = Signal(str)      # label
    delete_requested = Signal(str)      # label
    relocate_requested = Signal(str)    # label
    toggled = Signal(str, bool)         # label, checked
    # Emitted from the worker thread; AutoConnection marshals to GUI thread.
    scan_event = Signal(str, str)       # kind, rel_path

    def __init__(
        self,
        label: str,
        abs_path: str,
        status: str,
        checked: bool = True,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._label = label
        self._abs_path = abs_path
        self._scanning = False
        self._cancel: Optional[threading.Event] = None
        self.scan_event.connect(self.update_scan_event)

        self._check_btn = QPushButton("\U0001f4d6" if checked else "\U0001f4d7")
        self._check_btn.setCheckable(True)
        self._check_btn.setChecked(checked)
        self._check_btn.setFixedWidth(28)
        self._check_btn.setFixedHeight(28)
        self._check_btn.setFlat(True)
        self._check_btn.setStyleSheet("font-size: 16px; padding: 0;")
        self._check_btn.toggled.connect(self._on_book_toggled)
        self._check_btn.toggled.connect(lambda v: self.toggled.emit(label, v))

        self._name_label = QLabel(f"<b>{label}</b>")
        self._name_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )

        self._scan_btn = QPushButton("Scan")
        self._scan_btn.setFixedWidth(54)
        self._scan_btn.clicked.connect(self._on_scan_clicked)
        if status != "ok":
            self._scan_btn.setEnabled(False)
            self._scan_btn.setToolTip(f"Unavailable: {status}")

        detach_btn = QPushButton("✕")
        detach_btn.setFixedWidth(24)
        detach_btn.setFixedHeight(24)
        detach_btn.setFlat(True)
        detach_btn.setToolTip("Detach root")
        detach_btn.setStyleSheet("color: #888; font-size: 13px;")
        detach_btn.clicked.connect(lambda: self.detach_requested.emit(self._label))

        delete_btn = QPushButton("Delete ❌")
        delete_btn.setFlat(True)
        delete_btn.setToolTip("Delete root (removes database and thumbnails)")
        delete_btn.setStyleSheet("color: #c0392b; font-size: 12px;")
        delete_btn.clicked.connect(lambda: self.delete_requested.emit(self._label))

        top_row = QHBoxLayout()
        top_row.setContentsMargins(0, 0, 0, 0)
        top_row.addWidget(self._check_btn)
        top_row.addSpacing(4)
        top_row.addWidget(self._name_label)
        top_row.addWidget(self._scan_btn)
        top_row.addWidget(detach_btn)
        top_row.addWidget(delete_btn)

        path_text = abs_path if len(abs_path) <= 38 else "…" + abs_path[-37:]
        self._path_label = QLabel(f'<span style="color:gray;font-size:11px;">{path_text}</span>')
        self._path_label.setToolTip(abs_path)

        self._status_label = QLabel()
        self._status_label.setStyleSheet("font-size: 11px; color: gray;")
        self._status_label.setWordWrap(True)
        self._set_status_text(status)

        self._relocate_btn = QPushButton("Relocate…")
        self._relocate_btn.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self._relocate_btn.setStyleSheet(
            "QPushButton { color: #e67e22; border: 1px solid #e67e22; "
            "border-radius: 3px; padding: 2px 8px; font-size: 11px; }"
            "QPushButton:hover { background: #fef5ec; }"
            "QPushButton:pressed { background: #fdebd0; }"
        )
        self._relocate_btn.clicked.connect(lambda: self.relocate_requested.emit(self._label))
        self._relocate_btn.setVisible(status != "ok")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(1)
        layout.addLayout(top_row)
        layout.addWidget(self._path_label)
        layout.addWidget(self._status_label)
        layout.addWidget(self._relocate_btn)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("color: #ddd;")
        layout.addWidget(line)

    # -- public interface ---------------------------------------------------

    def set_scanning(self, scanning: bool) -> None:
        self._scanning = scanning
        self._cancel = None
        self._scan_btn.setEnabled(True)
        self._scan_btn.setText("■ Stop" if scanning else "Scan")
        if not scanning:
            self._status_label.setStyleSheet("font-size: 11px; color: gray;")

    def set_scan_progress(self, done: int, total: int) -> None:
        self._status_label.setStyleSheet("font-size: 11px; color: #2980b9;")
        self._status_label.setText(f"Scanning… ({done:,} of {total:,})")

    def update_scan_event(self, kind: str, rel_path: str) -> None:
        """Called from the worker thread via a queued signal (missing-file phase only)."""
        self._status_label.setStyleSheet("font-size: 11px; color: #2980b9;")
        short = rel_path if len(rel_path) <= 42 else "…" + rel_path[-41:]
        self._status_label.setText(f"↳ {short}")

    def set_scan_summary(
        self,
        new: int,
        edited: int,
        unchanged: int,
        missing: int,
        stale_staging: Optional[list[str]] = None,
    ) -> None:
        self.set_scanning(False)
        parts = []
        if new:
            parts.append(f"{new} new")
        if edited:
            parts.append(f"{edited} edited")
        if missing:
            parts.append(f"{missing} missing")
        if not parts:
            parts.append(f"{unchanged} unchanged")
        if stale_staging:
            # Orphaned staging files from a crashed rename/delete. Warn (orange)
            # and surface the paths as a tooltip; the user decides whether to delete.
            self._status_label.setStyleSheet("font-size: 11px; color: #e67e22;")
            self._status_label.setText(
                "⚠ " + " · ".join(parts) + f" · {len(stale_staging)} stale staging file(s)"
            )
            self._status_label.setToolTip(
                "Orphaned staging files (delete manually):\n" + "\n".join(stale_staging)
            )
            return
        self._status_label.setToolTip("")
        self._set_status_text("ok", extra=" · ".join(parts))

    def set_error(self, msg: str) -> None:
        self.set_scanning(False)
        self._status_label.setStyleSheet("font-size: 11px; color: #c0392b;")
        self._status_label.setText(f"⚠ {msg}")

    def is_checked(self) -> bool:
        return self._check_btn.isChecked()

    def _on_book_toggled(self, checked: bool) -> None:
        self._check_btn.setText("\U0001f4d6" if checked else "\U0001f4d7")

    @property
    def abs_path(self) -> str:
        return self._abs_path

    # -- private -----------------------------------------------------------

    def start_scan(self) -> threading.Event:
        """Called by the panel before submitting the scan job. Returns the cancel token."""
        self._cancel = threading.Event()
        self._scanning = True
        self._scan_btn.setText("■ Stop")
        self._status_label.setStyleSheet("font-size: 11px; color: #2980b9;")
        self._status_label.setText("Scanning…")
        return self._cancel

    def _on_scan_clicked(self) -> None:
        if self._scanning:
            if self._cancel is not None:
                self._cancel.set()
            self._scan_btn.setEnabled(False)
            self._scan_btn.setText("Stopping…")
            return
        self.scan_requested.emit(self._label)

    def _set_status_text(self, status: str, extra: str = "") -> None:
        if status == "ok":
            text = extra if extra else "ok"
            self._status_label.setStyleSheet("font-size: 11px; color: gray;")
        else:
            text = f"⚠ {status}"
            self._status_label.setStyleSheet("font-size: 11px; color: #e67e22;")
        self._status_label.setText(text)


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------

class RootsPanel(QWidget):
    """
    Sidebar that shows all attached roots and provides scan / attach / detach
    controls. Stateless with respect to the federation — it reads config at
    build time and emits signals when the caller should reload.
    """

    roots_changed = Signal()
    filter_changed = Signal(list)   # list[str] of checked labels

    def __init__(
        self,
        bridge: QtDBBridge,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._bridge = bridge
        self._entries: dict[str, RootEntry] = {}
        self._checked_labels: set[str] = set()

        self.setMinimumWidth(120)

        header = QLabel("<b>ROOTS</b>")
        header.setContentsMargins(6, 6, 6, 2)

        self._scroll_content = QWidget()
        self._scroll_layout = QVBoxLayout(self._scroll_content)
        self._scroll_layout.setContentsMargins(0, 0, 0, 0)
        self._scroll_layout.setSpacing(0)
        self._scroll_layout.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._scroll_content)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        attach_btn = QPushButton("+ Attach Root")
        attach_btn.clicked.connect(self._attach_root)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(header)
        layout.addWidget(scroll, stretch=1)
        layout.addWidget(attach_btn)

    def load_roots(self, fed: "federation.Federation") -> None:
        """Rebuild the entry list from a live federation object."""
        for entry in list(self._entries.values()):
            self._scroll_layout.removeWidget(entry)
            entry.deleteLater()
        self._entries.clear()

        roots = federation.list_roots(fed)
        for label, abs_path, status in roots:
            # First load: all available roots checked by default.
            if label not in self._checked_labels and status == "ok":
                self._checked_labels.add(label)
            entry = RootEntry(
                label=label,
                abs_path=abs_path,
                status=status,
                checked=(label in self._checked_labels),
            )
            entry.scan_requested.connect(self._on_scan_requested)
            entry.detach_requested.connect(self._on_detach_requested)
            entry.delete_requested.connect(self._on_delete_requested)
            entry.relocate_requested.connect(self._on_relocate_requested)
            entry.toggled.connect(self._on_toggled)
            self._entries[label] = entry
            # Insert before the stretch item at the end.
            self._scroll_layout.insertWidget(self._scroll_layout.count() - 1, entry)

    def checked_labels(self) -> list[str]:
        return [lbl for lbl, e in self._entries.items() if e.is_checked()]

    # -- private -----------------------------------------------------------

    def _on_toggled(self, label: str, checked: bool) -> None:
        if checked:
            self._checked_labels.add(label)
        else:
            self._checked_labels.discard(label)
        self.filter_changed.emit(self.checked_labels())

    def _on_scan_requested(self, label: str) -> None:
        entry = self._entries.get(label)
        if entry is None:
            return
        cancel = entry.start_scan()
        lbl = label
        stale_staging: list[str] = []

        def on_event(kind: str, rel_path: str) -> None:
            # Only forward missing-file events (finish phase); batch progress
            # is shown via set_scan_progress in on_batch_done instead.
            if kind == "missing":
                e = self._entries.get(lbl)
                if e:
                    e.scan_event.emit(kind, rel_path)
            elif kind == "stale_staging":
                # Orphaned .imgdb-tmp-* file from a crashed disk op. Surface it;
                # deletion stays a user action (invariant #6, no silent fallbacks).
                stale_staging.append(rel_path)

        def on_error(exc: BaseException) -> None:
            e = self._entries.get(lbl)
            if e:
                e.set_error(str(exc))
            # Always try to clean up the temp table even after an error.
            # (scan_shard_finish handles a missing table gracefully.)

        def on_finish_done(summary: "imgdb.ScanSummary") -> None:
            e = self._entries.get(lbl)
            if e:
                e.set_scan_summary(
                    summary.new, summary.edited, summary.unchanged, summary.missing,
                    stale_staging=list(stale_staging),
                )
            self.roots_changed.emit()

        def submit_finish(session) -> None:
            self._bridge.submit(
                federation.scan_shard_finish, lbl, session,
                on_event=on_event,
                on_result=on_finish_done,
                on_error=on_error,
                priority=imgdb_worker.PRIORITY_BACKGROUND,
            )

        def submit_batch(session) -> None:
            def on_batch_done(result) -> None:
                sess, done = result
                e = self._entries.get(lbl)
                if e:
                    e.set_scan_progress(sess.offset, len(sess.all_paths))
                if done or (cancel is not None and cancel.is_set()):
                    submit_finish(sess)
                else:
                    submit_batch(sess)

            self._bridge.submit(
                federation.scan_shard_batch, lbl, session,
                cancel=cancel,
                on_event=on_event,
                on_result=on_batch_done,
                on_error=on_error,
                priority=imgdb_worker.PRIORITY_BACKGROUND,
            )

        def on_init_done(session) -> None:
            submit_batch(session)

        self._bridge.submit(
            federation.scan_shard_init, lbl,
            on_result=on_init_done,
            on_error=on_error,
            priority=imgdb_worker.PRIORITY_BACKGROUND,
        )

    def _on_relocate_requested(self, label: str) -> None:
        from PySide6.QtWidgets import QFileDialog
        new_path = QFileDialog.getExistingDirectory(
            self, f"Relocate root '{label}'", "",
        )
        if not new_path:
            return

        def on_result(_) -> None:
            self.roots_changed.emit()

        def on_error(exc: BaseException) -> None:
            QMessageBox.critical(self, "Relocate failed", str(exc))

        self._bridge.submit(
            federation.relocate_root,
            label,
            new_path,
            on_result=on_result,
            on_error=on_error,
        )

    def _on_delete_requested(self, label: str) -> None:
        entry = self._entries.get(label)
        if entry is None:
            return
        dlg = DeleteRootDialog(label, entry.abs_path)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        def on_result(_) -> None:
            self._checked_labels.discard(label)
            self.roots_changed.emit()

        def on_error(exc: BaseException) -> None:
            QMessageBox.critical(self, "Delete failed", str(exc))

        self._bridge.submit(
            federation.delete_root,
            label,
            on_result=on_result,
            on_error=on_error,
        )

    def _on_detach_requested(self, label: str) -> None:
        btn = QMessageBox.question(
            self, "⚠ Detach root",
            f"Detach '{label}' from the federation?\n\n"
            "The root folder and its database are not deleted.",
        )
        if btn != QMessageBox.StandardButton.Yes:
            return

        def on_result(_) -> None:
            self._checked_labels.discard(label)
            self.roots_changed.emit()

        def on_error(exc: BaseException) -> None:
            QMessageBox.critical(self, "Detach failed", str(exc))

        self._bridge.submit(
            federation.detach_root,
            label,
            on_result=on_result,
            on_error=on_error,
        )

    def _attach_root(self) -> None:
        dlg = AttachRootDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        label = dlg.label()
        path = dlg.path()

        def on_result(_entry) -> None:
            self.roots_changed.emit()

        def on_error(exc: BaseException) -> None:
            QMessageBox.critical(self, "Attach failed", str(exc))

        self._bridge.submit(
            federation.attach_root,
            label,
            path,
            on_result=on_result,
            on_error=on_error,
        )
