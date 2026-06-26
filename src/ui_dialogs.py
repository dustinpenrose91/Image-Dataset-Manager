"""
Shared dialogs for imgdb UI.

All dialogs are modal and return data via the standard Qt exec()/result()
pattern. They validate input before accepting so callers receive clean data.
"""
from __future__ import annotations

import os
import re
from typing import Callable, Optional

from PySide6.QtCore import QModelIndex, QStringListModel, Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QCompleter,
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QPlainTextEdit, QPushButton, QRadioButton, QSizePolicy,
    QStyle, QStyledItemDelegate, QStyleOptionViewItem, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

_LABEL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RESERVED = frozenset({"main", "temp"})


# ---------------------------------------------------------------------------
# Attach Root
# ---------------------------------------------------------------------------

class AttachRootDialog(QDialog):
    """
    Collects a label + absolute path for a new root.
    Validates label syntax before accepting (path existence is checked by
    the caller / federation layer).
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__()
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setWindowTitle("Attach Root")
        self.setMinimumWidth(400)

        self._label_edit = QLineEdit()
        self._label_edit.setPlaceholderText("e.g. my_photos")

        self._path_edit = QLineEdit()
        self._path_edit.setPlaceholderText("/home/user/photos")

        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse)

        path_row = QHBoxLayout()
        path_row.addWidget(self._path_edit)
        path_row.addWidget(browse_btn)

        self._error_label = QLabel()
        self._error_label.setStyleSheet("color: red;")
        self._error_label.hide()

        form = QFormLayout()
        form.addRow("Label:", self._label_edit)
        form.addRow("Path:", path_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self._error_label)
        layout.addWidget(buttons)

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select Root Directory")
        if path:
            self._path_edit.setText(path)

    def _validate_and_accept(self) -> None:
        label = self._label_edit.text().strip()
        path = self._path_edit.text().strip()
        if not label:
            self._show_error("Label is required.")
            return
        if label.lower() in _RESERVED:
            self._show_error(f"'{label}' is a reserved name. Choose another.")
            return
        if not _LABEL_RE.match(label):
            self._show_error("Label must start with a letter or _ and contain only letters, digits, _.")
            return
        if not path:
            self._show_error("Path is required.")
            return
        self._error_label.hide()
        self.accept()

    def _show_error(self, msg: str) -> None:
        self._error_label.setText(msg)
        self._error_label.show()
        self.adjustSize()

    def label(self) -> str:
        return self._label_edit.text().strip()

    def path(self) -> str:
        return self._path_edit.text().strip()


# ---------------------------------------------------------------------------
# Delete Root (GitHub-style type-to-confirm)
# ---------------------------------------------------------------------------

class DeleteRootDialog(QDialog):
    """
    Requires the user to type the root label before deletion is allowed.
    """

    def __init__(self, label: str, abs_path: str, parent: Optional[QWidget] = None) -> None:
        super().__init__()
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setWindowTitle("Delete root")
        self.setMinimumWidth(420)

        warning = QLabel(
            "<b>⚠ This action is irreversible.</b><br><br>"
            "The root will be removed from the config and its database and "
            "thumbnail cache will be permanently deleted.<br><br>"
            "<b>Image files are not deleted</b>, but all metadata "
            "(asset records, tags, captions, hash history) will be lost."
        )
        warning.setWordWrap(True)
        warning.setStyleSheet("color: #c0392b;")

        path_label = QLabel(f"<b>{label}</b>  <span style='color:gray;'>{abs_path}</span>")
        path_label.setWordWrap(True)

        confirm_label = QLabel(f'Type <b>{label}</b> to confirm:')

        self._edit = QLineEdit()
        self._edit.setPlaceholderText(label)
        self._edit.textChanged.connect(self._on_text_changed)

        self._delete_btn = QPushButton("Delete root")
        self._delete_btn.setEnabled(False)
        self._delete_btn.setStyleSheet(
            "QPushButton:enabled { background: #c0392b; color: white; font-weight: bold; }"
        )
        cancel_btn = QPushButton("Cancel")

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(self._delete_btn)

        self._delete_btn.clicked.connect(self.accept)
        cancel_btn.clicked.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.addWidget(warning)
        layout.addWidget(path_label)
        layout.addWidget(confirm_label)
        layout.addWidget(self._edit)
        layout.addLayout(btn_row)

        self._label = label

    def _on_text_changed(self, text: str) -> None:
        self._delete_btn.setEnabled(text == self._label)


# ---------------------------------------------------------------------------
# Rename Asset
# ---------------------------------------------------------------------------

class RenameDialog(QDialog):
    """
    Prompts for a new relative path within the asset's root.
    The current rel_path is pre-filled for easy editing.
    """

    def __init__(self, current_rel_path: str, parent: Optional[QWidget] = None) -> None:
        super().__init__()
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setWindowTitle("Rename Asset")
        self.setMinimumWidth(420)

        self._edit = QLineEdit(current_rel_path)
        self._edit.selectAll()

        self._error_label = QLabel()
        self._error_label.setStyleSheet("color: red;")
        self._error_label.hide()

        form = QFormLayout()
        form.addRow("New path (relative to root):", self._edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self._error_label)
        layout.addWidget(buttons)

    def _validate_and_accept(self) -> None:
        path = self._edit.text().strip()
        if not path:
            self._error_label.setText("Path cannot be empty.")
            self._error_label.show()
            return
        if path.startswith("/"):
            self._error_label.setText("Path must be relative (no leading slash).")
            self._error_label.show()
            return
        self._error_label.hide()
        self.accept()

    def new_rel_path(self) -> str:
        return self._edit.text().strip()


# ---------------------------------------------------------------------------
# Move Asset
# ---------------------------------------------------------------------------

class MoveDialog(QDialog):
    """
    Lets the user pick a destination directory within the asset's root.
    The filename is preserved; only the subdirectory changes.
    """

    def __init__(
        self,
        current_rel_path: str,
        root_abs: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__()
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setWindowTitle("Move Asset")
        self.setMinimumWidth(460)

        self._root_abs = os.path.normpath(os.path.abspath(root_abs))
        self._filename = os.path.basename(current_rel_path)
        self._dest_path = os.path.dirname(current_rel_path)  # rel, may be ""

        src_label = QLabel(f"File: <b>{current_rel_path}</b>")
        src_label.setWordWrap(True)

        self._dest_edit = QLineEdit(self._dest_path or "(root)")
        self._dest_edit.setReadOnly(True)

        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse)

        dest_row = QHBoxLayout()
        dest_row.addWidget(self._dest_edit, stretch=1)
        dest_row.addWidget(browse_btn)

        self._preview_label = QLabel()
        self._preview_label.setStyleSheet("color: gray; font-size: 11px;")
        self._update_preview()

        self._error_label = QLabel()
        self._error_label.setStyleSheet("color: red;")
        self._error_label.hide()

        form = QFormLayout()
        form.addRow("Destination folder:", dest_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Move")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(src_label)
        layout.addSpacing(8)
        layout.addLayout(form)
        layout.addWidget(self._preview_label)
        layout.addWidget(self._error_label)
        layout.addWidget(buttons)

    def _browse(self) -> None:
        start = os.path.join(self._root_abs, self._dest_path) if self._dest_path else self._root_abs
        if not os.path.isdir(start):
            start = self._root_abs
        chosen = QFileDialog.getExistingDirectory(self, "Choose Destination Folder", start)
        if not chosen:
            return
        chosen = os.path.normpath(os.path.abspath(chosen))
        root_prefix = self._root_abs + os.sep
        if chosen != self._root_abs and not chosen.startswith(root_prefix):
            self._error_label.setText("Destination must be inside the root directory.")
            self._error_label.show()
            return
        self._error_label.hide()
        if chosen == self._root_abs:
            self._dest_path = ""
            self._dest_edit.setText("(root)")
        else:
            self._dest_path = os.path.relpath(chosen, self._root_abs).replace(os.sep, "/")
            self._dest_edit.setText(self._dest_path)
        self._update_preview()

    def _update_preview(self) -> None:
        self._preview_label.setText(f"New path: {self.new_rel_path()}")

    def new_rel_path(self) -> str:
        if self._dest_path:
            return self._dest_path.rstrip("/") + "/" + self._filename
        return self._filename


# ---------------------------------------------------------------------------
# Batch Move Assets
# ---------------------------------------------------------------------------

class BatchMoveDialog(QDialog):
    """
    Picks a destination directory within a root for moving multiple assets.
    Each asset keeps its original filename; only the subdirectory changes.
    """

    def __init__(
        self,
        n_assets: int,
        root_abs: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__()
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setWindowTitle("Move Assets")
        self.setMinimumWidth(460)

        self._root_abs = os.path.normpath(os.path.abspath(root_abs))
        self._dest_path = ""  # rel path within root; "" means root itself

        header = QLabel(f"Move <b>{n_assets}</b> asset(s) to:")

        self._dest_edit = QLineEdit("(root)")
        self._dest_edit.setReadOnly(True)

        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse)

        dest_row = QHBoxLayout()
        dest_row.addWidget(self._dest_edit, stretch=1)
        dest_row.addWidget(browse_btn)

        self._error_label = QLabel()
        self._error_label.setStyleSheet("color: red;")
        self._error_label.hide()

        form = QFormLayout()
        form.addRow("Destination folder:", dest_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Move")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(header)
        layout.addSpacing(8)
        layout.addLayout(form)
        layout.addWidget(self._error_label)
        layout.addWidget(buttons)

    def _browse(self) -> None:
        start = os.path.join(self._root_abs, self._dest_path) if self._dest_path else self._root_abs
        if not os.path.isdir(start):
            start = self._root_abs
        chosen = QFileDialog.getExistingDirectory(self, "Choose Destination Folder", start)
        if not chosen:
            return
        chosen = os.path.normpath(os.path.abspath(chosen))
        root_prefix = self._root_abs + os.sep
        if chosen != self._root_abs and not chosen.startswith(root_prefix):
            self._error_label.setText("Destination must be inside the root directory.")
            self._error_label.show()
            return
        self._error_label.hide()
        if chosen == self._root_abs:
            self._dest_path = ""
            self._dest_edit.setText("(root)")
        else:
            self._dest_path = os.path.relpath(chosen, self._root_abs).replace(os.sep, "/")
            self._dest_edit.setText(self._dest_path)

    def dest_dir(self) -> str:
        """Relative path within root of the chosen destination; '' means root."""
        return self._dest_path


# ---------------------------------------------------------------------------
# Merge Assets
# ---------------------------------------------------------------------------

class MergeDialog(QDialog):
    """
    Shown when exactly two same-shard assets are selected.
    Lets the user pick which is the survivor.
    """

    def __init__(
        self,
        path_a: str,
        asset_id_a: str,
        path_b: str,
        asset_id_b: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__()
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setWindowTitle("Merge Assets")
        self.setMinimumWidth(420)
        self._id_a = asset_id_a
        self._id_b = asset_id_b

        note = QLabel(
            "Tags and captions from both assets will be merged into the survivor."
        )
        note.setWordWrap(True)

        self._radio_a = QRadioButton(path_a)
        self._radio_a.setChecked(True)
        self._radio_b = QRadioButton(path_b)

        survivor_label = QLabel("Survivor (keeps its file and identity):")

        self._delete_cb = QCheckBox("Delete the duplicate file after merging")

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Merge")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(note)
        layout.addSpacing(8)
        layout.addWidget(survivor_label)
        layout.addWidget(self._radio_a)
        layout.addWidget(self._radio_b)
        layout.addSpacing(8)
        layout.addWidget(self._delete_cb)
        layout.addSpacing(4)
        layout.addWidget(buttons)

    def survivor_id(self) -> str:
        return self._id_a if self._radio_a.isChecked() else self._id_b

    def merged_id(self) -> str:
        return self._id_b if self._radio_a.isChecked() else self._id_a

    def delete_duplicate(self) -> bool:
        return self._delete_cb.isChecked()


# ---------------------------------------------------------------------------
# Add Caption Kind
# ---------------------------------------------------------------------------

class AddCaptionDialog(QDialog):
    """Prompts for a new caption kind (e.g. 'short', 'long', 'alt_de')."""

    def __init__(
        self,
        existing_kinds: list[str],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__()
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setWindowTitle("Add Caption")
        self.setMinimumWidth(300)
        self._existing = {k.lower() for k in existing_kinds}

        self._edit = QLineEdit()
        self._edit.setPlaceholderText("e.g. short, long, alt_de")

        self._error_label = QLabel()
        self._error_label.setStyleSheet("color: red;")
        self._error_label.hide()

        form = QFormLayout()
        form.addRow("Kind:", self._edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self._error_label)
        layout.addWidget(buttons)

    def _validate_and_accept(self) -> None:
        kind = self._edit.text().strip()
        if not kind:
            self._error_label.setText("Kind cannot be empty.")
            self._error_label.show()
            return
        if kind.lower() in self._existing:
            self._error_label.setText(f"A caption of kind '{kind}' already exists.")
            self._error_label.show()
            return
        self._error_label.hide()
        self.accept()

    def kind(self) -> str:
        return self._edit.text().strip()


# ---------------------------------------------------------------------------
# Batch Tag
# ---------------------------------------------------------------------------

class _TagCountDelegate(QStyledItemDelegate):
    """Renders tag completion items as 'tagname  (count)'."""

    def __init__(self, counts: dict[str, int], parent=None) -> None:
        super().__init__(parent)
        self._counts = counts

    def paint(self, painter, option, index) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        name = opt.text
        opt.text = f"{name}  ({self._counts.get(name, 0)})"
        QApplication.style().drawControl(
            QStyle.ControlElement.CE_ItemViewItem, opt, painter,
        )


class _TagMultiCompleter(QCompleter):
    """
    QCompleter that completes the last comma-delimited token in a multi-value
    field, then re-joins with the preceding tokens on activation.

    Comma is the sole delimiter so that multi-word tags (e.g. "ai generated")
    are treated as a single unit and never accidentally split.
    """

    def splitPath(self, path: str) -> list[str]:
        parts = [p.strip() for p in path.split(",")]
        return [parts[-1]] if parts else [""]

    def pathFromIndex(self, index: QModelIndex) -> str:
        completion = QCompleter.pathFromIndex(self, index)
        text = self.widget().text() if self.widget() else ""
        parts = [p.strip() for p in text.split(",")]
        if parts:
            parts[-1] = completion
        else:
            parts = [completion]
        return ", ".join(parts)


class AddTagCategoryDialog(QDialog):
    """Prompts for a tag category name and its first required tag."""

    def __init__(
        self,
        all_types: list[str],
        shown_types: set[str],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setWindowTitle("Add Tag Category")
        self.setMinimumWidth(300)

        available = [t for t in all_types if t not in shown_types and t != "General"]

        self._type_edit = QLineEdit()
        self._type_edit.setPlaceholderText("e.g. Character, Style, Artist")
        if available:
            c = QCompleter(available, self)
            c.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            self._type_edit.setCompleter(c)

        self._tag_edit = QLineEdit()
        self._tag_edit.setPlaceholderText("tag name")
        self._tag_edit.returnPressed.connect(self._validate_and_accept)

        self._error_label = QLabel()
        self._error_label.setStyleSheet("color: red;")
        self._error_label.hide()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Add Category")
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)

        form = QFormLayout()
        form.addRow("Category:", self._type_edit)
        form.addRow("First tag:", self._tag_edit)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self._error_label)
        layout.addWidget(buttons)

    def _validate_and_accept(self) -> None:
        if not self.type_name():
            self._error_label.setText("Enter a category name.")
            self._error_label.show()
            return
        if not self.tag_name():
            self._error_label.setText("Enter at least one tag name.")
            self._error_label.show()
            return
        self._error_label.hide()
        self.accept()

    def type_name(self) -> str:
        return self._type_edit.text().strip()

    def tag_name(self) -> str:
        return self._tag_edit.text().strip()


class BatchTagDialog(QDialog):
    """
    Prompts for one or more tags to add to all selected assets.
    Tags are entered space-separated or comma-separated.
    """

    def __init__(self, n_assets: int, parent: Optional[QWidget] = None) -> None:
        super().__init__()
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setWindowTitle("Add Tags to Selection")
        self.setMinimumWidth(340)

        header = QLabel(f"Add tags to <b>{n_assets}</b> selected asset(s):")
        self._edit = QLineEdit()
        self._edit.setPlaceholderText("landscape, ai generated, water")

        hint = QLabel("Separate multiple tags with commas.")
        hint.setStyleSheet("color: gray; font-size: 11px;")

        self._types: list[str] = ["General"]
        self._type_combo = QComboBox()
        self._type_combo.addItem("General")

        type_row = QHBoxLayout()
        type_row.addWidget(QLabel("Category:"))
        type_row.addWidget(self._type_combo, stretch=1)

        self._error_label = QLabel()
        self._error_label.setStyleSheet("color: red;")
        self._error_label.hide()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Add Tags")
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(header)
        layout.addWidget(self._edit)
        layout.addWidget(hint)
        layout.addLayout(type_row)
        layout.addWidget(self._error_label)
        layout.addWidget(buttons)

    def set_suggestions(self, tags: list[tuple[str, int]]) -> None:
        counts = {name: count for name, count in tags}
        completer = _TagMultiCompleter()
        completer.setModel(QStringListModel([name for name, _ in tags], completer))
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.popup().setItemDelegate(_TagCountDelegate(counts, completer.popup()))
        self._edit.setCompleter(completer)

    def set_type_suggestions(self, types: list[str]) -> None:
        current = self._type_combo.currentText()
        self._types = types if types else ["General"]
        self._type_combo.clear()
        for t in self._types:
            self._type_combo.addItem(t)
        if current in self._types:
            self._type_combo.setCurrentText(current)

    def _validate_and_accept(self) -> None:
        if not self.tags():
            self._error_label.setText("Enter at least one tag.")
            self._error_label.show()
            return
        self._error_label.hide()
        self.accept()

    def tags(self) -> list[str]:
        return [t.strip() for t in self._edit.text().split(",") if t.strip()]

    def type_name(self) -> str:
        idx = self._type_combo.currentIndex()
        return self._types[idx] if 0 <= idx < len(self._types) else "General"


def _tag_completer(tags: list[tuple[str, int]], parent: Optional[QWidget] = None) -> QCompleter:
    """Single-value completer with contains-matching and usage-count delegate."""
    counts = {name: count for name, count in tags}
    c = QCompleter(parent)
    c.setModel(QStringListModel([name for name, _ in tags], c))
    c.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
    c.setFilterMode(Qt.MatchFlag.MatchContains)
    c.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
    c.popup().setItemDelegate(_TagCountDelegate(counts, c.popup()))
    return c


class BatchRemoveTagDialog(QDialog):
    """Prompts for a tag to remove from all selected assets."""

    def __init__(
        self,
        n_assets: int,
        tags: list[tuple[str, int]],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setWindowTitle("Remove Tag from Selection")
        self.setMinimumWidth(320)

        header = QLabel(f"Remove a tag from <b>{n_assets}</b> selected asset(s):")
        self._edit = QLineEdit()
        self._edit.setPlaceholderText("tag name")
        self._edit.setCompleter(_tag_completer(tags, self))
        self._edit.returnPressed.connect(self._validate_and_accept)

        self._error_label = QLabel()
        self._error_label.setStyleSheet("color: red;")
        self._error_label.hide()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Remove Tag")
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(header)
        layout.addWidget(self._edit)
        layout.addWidget(self._error_label)
        layout.addWidget(buttons)

    def _validate_and_accept(self) -> None:
        if not self.tag():
            self._error_label.setText("Enter a tag name.")
            self._error_label.show()
            return
        self._error_label.hide()
        self.accept()

    def tag(self) -> str:
        return self._edit.text().strip()


class BatchReplaceTagDialog(QDialog):
    """Prompts for an old tag and a new tag to replace it across all selected assets."""

    def __init__(
        self,
        n_assets: int,
        tags: list[tuple[str, int]],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setWindowTitle("Replace Tag in Selection")
        self.setMinimumWidth(340)

        header = QLabel(f"Replace a tag across <b>{n_assets}</b> selected asset(s):")

        self._old_edit = QLineEdit()
        self._old_edit.setPlaceholderText("tag to replace")
        self._old_edit.setCompleter(_tag_completer(tags, self))

        self._new_edit = QLineEdit()
        self._new_edit.setPlaceholderText("replacement tag")
        self._new_edit.setCompleter(_tag_completer(tags, self))
        self._new_edit.returnPressed.connect(self._validate_and_accept)

        form = QFormLayout()
        form.addRow("Remove:", self._old_edit)
        form.addRow("Replace with:", self._new_edit)

        self._error_label = QLabel()
        self._error_label.setStyleSheet("color: red;")
        self._error_label.hide()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Replace Tag")
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(header)
        layout.addLayout(form)
        layout.addWidget(self._error_label)
        layout.addWidget(buttons)

    def _validate_and_accept(self) -> None:
        old, new = self.old_tag(), self.new_tag()
        if not old:
            self._error_label.setText("Enter the tag to replace.")
            self._error_label.show()
            return
        if not new:
            self._error_label.setText("Enter the replacement tag.")
            self._error_label.show()
            return
        if old == new:
            self._error_label.setText("Old and new tags are the same.")
            self._error_label.show()
            return
        self._error_label.hide()
        self.accept()

    def old_tag(self) -> str:
        return self._old_edit.text().strip()

    def new_tag(self) -> str:
        return self._new_edit.text().strip()


# ---------------------------------------------------------------------------
# Confirm Delete
# ---------------------------------------------------------------------------

class ConfirmDeleteDialog(QDialog):
    """Confirmation before deleting one or more assets (files + DB rows)."""

    def __init__(
        self,
        paths: list[str],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__()
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setWindowTitle("Confirm Delete")
        self.setMinimumWidth(380)

        n = len(paths)
        header = QLabel(
            f"<b>Permanently delete {n} asset{'s' if n != 1 else ''}?</b><br>"
            "This removes the file(s) from disk and all metadata from the catalog."
        )
        header.setWordWrap(True)

        list_widget = QListWidget()
        list_widget.setMaximumHeight(120)
        for p in paths[:20]:
            list_widget.addItem(QListWidgetItem(p))
        if n > 20:
            list_widget.addItem(QListWidgetItem(f"… and {n - 20} more"))

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Delete")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setStyleSheet(
            "background-color: #c0392b; color: white;"
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(header)
        layout.addWidget(list_widget)
        layout.addWidget(buttons)


# ---------------------------------------------------------------------------
# Add to Dataset
# ---------------------------------------------------------------------------

class AddToDatasetDialog(QDialog):
    """
    Prompts for a dataset name. Shows existing datasets as a clickable list
    for quick selection; the user can also type a new name to create one.
    """

    def __init__(
        self,
        existing_names: list[str],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__()
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setWindowTitle("Add to Dataset")
        self.setMinimumWidth(320)

        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("Type a name…")

        self._error_label = QLabel()
        self._error_label.setStyleSheet("color: red;")
        self._error_label.hide()

        layout = QVBoxLayout(self)

        if existing_names:
            layout.addWidget(QLabel("Select an existing dataset or type a new name to create one:"))
            existing_list = QListWidget()
            existing_list.setMaximumHeight(140)
            for name in existing_names:
                existing_list.addItem(QListWidgetItem(name))
            existing_list.itemClicked.connect(
                lambda item: self._name_edit.setText(item.text())
            )
            layout.addWidget(existing_list)
            layout.addWidget(QLabel("Dataset name:"))
        else:
            hint = QLabel("No datasets yet. Enter a name to create one:")
            hint.setStyleSheet("color: gray; font-size: 11px;")
            layout.addWidget(hint)
            layout.addWidget(QLabel("Dataset name:"))

        layout.addWidget(self._name_edit)
        layout.addWidget(self._error_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Add / Create")
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._name_edit.setFocus()

    def _validate_and_accept(self) -> None:
        if not self._name_edit.text().strip():
            self._error_label.setText("Name cannot be empty.")
            self._error_label.show()
            return
        self._error_label.hide()
        self.accept()

    def dataset_name(self) -> str:
        return self._name_edit.text().strip()


# ---------------------------------------------------------------------------
# Bulk Import
# ---------------------------------------------------------------------------

class BulkImportDialog(QDialog):
    """
    Collects settings for a bulk import of paired image/.txt files.

    The caller reads the result via the accessor methods after exec() returns
    Accepted.
    """

    def __init__(
        self,
        root_labels: list[str],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__()
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setWindowTitle("Bulk Import Paired Files")
        self.setMinimumWidth(480)

        # -- Source directory --
        self._src_edit = QLineEdit()
        self._src_edit.setPlaceholderText("/path/to/image+txt folder")
        src_browse = QPushButton("Browse…")
        src_browse.clicked.connect(self._browse_source)
        src_row = QHBoxLayout()
        src_row.addWidget(self._src_edit)
        src_row.addWidget(src_browse)

        # -- File mode --
        mode_box = QGroupBox("File handling")
        self._mode_meta = QRadioButton("Files are already inside the selected root")
        self._mode_copy = QRadioButton("Copy images into the selected root")
        self._mode_move = QRadioButton("Move images into the selected root")
        self._mode_meta.setChecked(True)
        mode_bg = QButtonGroup(self)
        for btn in (self._mode_meta, self._mode_copy, self._mode_move):
            mode_bg.addButton(btn)
        mode_layout = QVBoxLayout(mode_box)
        mode_layout.addWidget(self._mode_meta)
        mode_layout.addWidget(self._mode_copy)
        mode_layout.addWidget(self._mode_move)

        # Destination subfolder (copy/move only)
        self._dest_edit = QLineEdit()
        self._dest_edit.setPlaceholderText("optional subfolder inside root (e.g. imported)")
        self._dest_row_label = QLabel("Destination subfolder:")
        self._dest_row_label.setVisible(False)
        self._dest_edit.setVisible(False)
        self._mode_copy.toggled.connect(self._update_dest_visibility)
        self._mode_move.toggled.connect(self._update_dest_visibility)

        # -- Target root --
        self._root_combo = QComboBox()
        for lbl in root_labels:
            self._root_combo.addItem(lbl)

        # -- Content type --
        content_box = QGroupBox("Import .txt content as")
        self._as_caption = QRadioButton("Caption")
        self._as_tags = QRadioButton("Tags (comma-separated)")
        self._as_caption.setChecked(True)
        content_bg = QButtonGroup(self)
        content_bg.addButton(self._as_caption)
        content_bg.addButton(self._as_tags)

        self._kind_label = QLabel("Caption kind:")
        self._kind_edit = QLineEdit("main")
        self._kind_edit.setPlaceholderText("e.g. main, short, alt")
        kind_row = QHBoxLayout()
        kind_row.addWidget(self._kind_label)
        kind_row.addWidget(self._kind_edit)

        self._as_caption.toggled.connect(lambda v: self._kind_label.setVisible(v))
        self._as_caption.toggled.connect(lambda v: self._kind_edit.setVisible(v))

        content_layout = QVBoxLayout(content_box)
        content_layout.addWidget(self._as_caption)
        content_layout.addWidget(self._as_tags)
        content_layout.addLayout(kind_row)

        # -- Conflict handling --
        self._overwrite_cb = QCheckBox("Overwrite existing captions of the same kind")
        self._overwrite_cb.setChecked(True)

        # -- Error label --
        self._error_label = QLabel()
        self._error_label.setStyleSheet("color: red;")
        self._error_label.hide()

        # -- Buttons --
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Import")
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)

        # -- Layout --
        form = QFormLayout()
        form.addRow("Source directory:", src_row)
        form.addRow("Target root:", self._root_combo)
        form.addRow(self._dest_row_label, self._dest_edit)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(mode_box)
        layout.addWidget(content_box)
        layout.addWidget(self._overwrite_cb)
        layout.addWidget(self._error_label)
        layout.addWidget(buttons)

    def _browse_source(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select Source Directory")
        if path:
            self._src_edit.setText(path)

    def _update_dest_visibility(self) -> None:
        show = self._mode_copy.isChecked() or self._mode_move.isChecked()
        self._dest_row_label.setVisible(show)
        self._dest_edit.setVisible(show)
        self.adjustSize()

    def _validate_and_accept(self) -> None:
        if not self._src_edit.text().strip():
            self._error_label.setText("Source directory is required.")
            self._error_label.show()
            return
        if self._root_combo.count() == 0:
            self._error_label.setText("No roots are available. Attach a root first.")
            self._error_label.show()
            return
        if self._as_caption.isChecked() and not self._kind_edit.text().strip():
            self._error_label.setText("Caption kind cannot be empty.")
            self._error_label.show()
            return
        self._error_label.hide()
        self.accept()

    # -- Accessors --

    def source_dir(self) -> str:
        return self._src_edit.text().strip()

    def shard_label(self) -> str:
        return self._root_combo.currentText()

    def file_mode(self) -> str:
        if self._mode_copy.isChecked():
            return "copy"
        if self._mode_move.isChecked():
            return "move"
        return "metadata_only"

    def dest_subdir(self) -> str:
        return self._dest_edit.text().strip()

    def caption_kind(self) -> Optional[str]:
        if self._as_caption.isChecked():
            return self._kind_edit.text().strip()
        return None

    def overwrite(self) -> bool:
        return self._overwrite_cb.isChecked()


# ---------------------------------------------------------------------------
# Tag management dialogs
# ---------------------------------------------------------------------------

class ReplaceTagDialog(QDialog):
    """Replace a tag's name and/or type globally across all shards."""

    def __init__(
        self,
        tag_name: str,
        type_name: str,
        all_types: list[str],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setWindowTitle("Replace Tag")
        self.setMinimumWidth(340)

        current_label = QLabel(f"Replacing: <b>{tag_name}</b> (category: {type_name})")
        current_label.setStyleSheet("margin-bottom: 4px;")

        self._name_edit = QLineEdit(tag_name)

        types = all_types if all_types else ["General"]
        self._types = types
        self._type_combo = QComboBox()
        for t in types:
            self._type_combo.addItem(t)
        if type_name in types:
            self._type_combo.setCurrentText(type_name)

        form = QFormLayout()
        form.addRow("New name:", self._name_edit)
        form.addRow("New category:", self._type_combo)

        self._error_label = QLabel()
        self._error_label.setStyleSheet("color: red;")
        self._error_label.hide()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Replace")
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(current_label)
        layout.addLayout(form)
        layout.addWidget(self._error_label)
        layout.addWidget(buttons)

    def _validate_and_accept(self) -> None:
        if not self.new_name():
            self._error_label.setText("Enter a tag name.")
            self._error_label.show()
            return
        self._error_label.hide()
        self.accept()

    def new_name(self) -> str:
        return self._name_edit.text().strip()

    def new_type_name(self) -> str:
        idx = self._type_combo.currentIndex()
        return self._types[idx] if 0 <= idx < len(self._types) else "General"


class ChangeTagTypeDialog(QDialog):
    """Reassign a tag to a different category (type) globally."""

    def __init__(
        self,
        tag_name: str,
        current_type_name: str,
        all_types: list[str],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setWindowTitle("Change Tag Type")
        self.setMinimumWidth(300)

        label = QLabel(f"Tag: <b>{tag_name}</b>  (currently: {current_type_name})")
        label.setStyleSheet("margin-bottom: 4px;")

        types = all_types if all_types else ["General"]
        self._type_combo = QComboBox()
        self._type_combo.setEditable(True)
        self._type_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        for t in types:
            self._type_combo.addItem(t)
        self._type_combo.setCurrentText(current_type_name)

        self._error_label = QLabel()
        self._error_label.setStyleSheet("color: #c0392b; font-size: 11px;")
        self._error_label.hide()

        form = QFormLayout()
        form.addRow("New category:", self._type_combo)

        self._ok_btn = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._ok_btn.button(QDialogButtonBox.StandardButton.Ok).setText("Change")
        self._ok_btn.accepted.connect(self._on_accept)
        self._ok_btn.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(label)
        layout.addLayout(form)
        layout.addWidget(self._error_label)
        layout.addWidget(self._ok_btn)

    def _on_accept(self) -> None:
        if not self._type_combo.currentText().strip():
            self._error_label.setText("Category name cannot be empty.")
            self._error_label.show()
            return
        self.accept()

    def new_type_name(self) -> str:
        return self._type_combo.currentText().strip() or "General"


# ---------------------------------------------------------------------------
# Import Tags from Caption
# ---------------------------------------------------------------------------

class ImportFromCaptionDialog(QDialog):
    """
    Configure and preview a caption-to-tag import for one image or in bulk.

    match_func: callable(text: str) -> list[(canonical_name, [type_names])]
                Built by the caller as a closure over the pre-built tag lookup,
                so the dialog needs no federation dependency.
    caption_texts: {kind: text} for the currently selected asset.  Used to
                   drive the live preview when scope = "This image only".
    all_caption_kinds: union of all caption kinds across the checked shards
                       (populates the caption kind dropdown for bulk use).
    """

    def __init__(
        self,
        caption_texts: dict[str, str],
        all_caption_kinds: list[str],
        match_func: Callable[[str], list[tuple[str, list[str]]]],
        filtered_count: int,
        total_count: int,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Import Tags from Caption")
        self.setMinimumWidth(440)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)

        self._caption_texts = caption_texts
        self._match_func = match_func
        self._current_matches: list[tuple[str, list[str]]] = []

        # ── Caption kind ──────────────────────────────────────────────────
        self._kind_combo = QComboBox()
        self._kind_combo.addItem("All captions")
        seen: set[str] = set()
        for k in sorted(set(all_caption_kinds) | set(caption_texts.keys())):
            if k not in seen:
                self._kind_combo.addItem(k)
                seen.add(k)
        self._kind_combo.currentIndexChanged.connect(self._on_kind_changed)

        form = QFormLayout()
        form.addRow("Caption:", self._kind_combo)

        # ── Scope ─────────────────────────────────────────────────────────
        self._single_radio   = QRadioButton("This image only")
        self._filtered_radio = QRadioButton(f"Filtered images  ({filtered_count:,})")
        self._all_radio      = QRadioButton(f"All images in checked shards  ({total_count:,})")
        self._single_radio.setChecked(True)

        self._scope_group = QButtonGroup(self)
        for btn in (self._single_radio, self._filtered_radio, self._all_radio):
            self._scope_group.addButton(btn)
        self._scope_group.buttonClicked.connect(self._on_scope_changed)

        scope_box = QGroupBox("Scope")
        scope_lay = QVBoxLayout(scope_box)
        scope_lay.addWidget(self._single_radio)
        scope_lay.addWidget(self._filtered_radio)
        scope_lay.addWidget(self._all_radio)

        # ── Ambiguous tag handling ────────────────────────────────────────
        self._general_radio = QRadioButton("Import as General  (default)")
        self._ask_radio     = QRadioButton("Ask per tag  (resolve once, applied to all)")
        self._general_radio.setChecked(True)

        self._policy_group = QButtonGroup(self)
        self._policy_group.addButton(self._general_radio)
        self._policy_group.addButton(self._ask_radio)
        self._policy_group.buttonClicked.connect(self._on_policy_changed)

        policy_note = QLabel(
            "When a tag exists in more than one category:"
        )
        policy_note.setStyleSheet("color: gray; font-size: 11px;")

        policy_box = QGroupBox("Ambiguous tag handling")
        policy_lay = QVBoxLayout(policy_box)
        policy_lay.addWidget(policy_note)
        policy_lay.addWidget(self._general_radio)
        policy_lay.addWidget(self._ask_radio)

        # ── Preview (single-image only) ───────────────────────────────────
        self._preview_header = QLabel("<b>MATCHED TAGS</b>  (this image)")
        self._preview_header.setStyleSheet("font-size: 11px;")

        self._preview_table = QTableWidget(0, 3)
        self._preview_table.setHorizontalHeaderLabels(["", "Tag", "Category"])
        hdr = self._preview_table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        hdr.resizeSection(0, 24)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        hdr.resizeSection(1, 190)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._preview_table.verticalHeader().setVisible(False)
        self._preview_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self._preview_table.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection
        )
        self._preview_table.setMaximumHeight(200)

        self._no_match_label = QLabel("No matching tags found in this caption.")
        self._no_match_label.setStyleSheet("color: gray; font-size: 11px;")

        # ── Button row ────────────────────────────────────────────────────
        self._apply_btn = QPushButton("Apply")
        self._apply_btn.setDefault(True)
        cancel_btn = QPushButton("Cancel")
        self._apply_btn.clicked.connect(self.accept)
        cancel_btn.clicked.connect(self.reject)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_row.addWidget(self._apply_btn)
        btn_row.addWidget(cancel_btn)

        # ── Main layout ───────────────────────────────────────────────────
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(scope_box)
        layout.addWidget(policy_box)
        layout.addWidget(self._preview_header)
        layout.addWidget(self._preview_table)
        layout.addWidget(self._no_match_label)
        layout.addLayout(btn_row)

        self._on_kind_changed()

    # -- public accessors ----------------------------------------------------

    def scope(self) -> str:
        if self._single_radio.isChecked():
            return "single"
        if self._filtered_radio.isChecked():
            return "filtered"
        return "all"

    def caption_kind(self) -> Optional[str]:
        text = self._kind_combo.currentText()
        return None if text == "All captions" else text

    def ambiguous_policy(self) -> str:
        return "ask" if self._ask_radio.isChecked() else "general"

    def selected_tags(self) -> list[tuple[str, str]]:
        """[(name, type_name)] for all checked preview rows. Single scope only."""
        result = []
        for row in range(self._preview_table.rowCount()):
            check = self._preview_table.item(row, 0)
            name_item = self._preview_table.item(row, 1)
            if check is None or name_item is None:
                continue
            if check.checkState() != Qt.CheckState.Checked:
                continue
            name = name_item.text()
            widget = self._preview_table.cellWidget(row, 2)
            if isinstance(widget, QComboBox):
                type_name = widget.currentText()
            else:
                ti = self._preview_table.item(row, 2)
                type_name = ti.text() if ti else "General"
            result.append((name, type_name))
        return result

    # -- private slots -------------------------------------------------------

    def _on_kind_changed(self) -> None:
        kind = self.caption_kind()
        if kind is None:
            text = " ".join(self._caption_texts.values())
        else:
            text = self._caption_texts.get(kind, "")
        self._current_matches = self._match_func(text)
        self._rebuild_preview()

    def _on_scope_changed(self) -> None:
        is_single = self._single_radio.isChecked()
        self._preview_header.setVisible(is_single)
        self._preview_table.setVisible(is_single)
        self._no_match_label.setVisible(is_single and not self._current_matches)

    def _on_policy_changed(self) -> None:
        self._rebuild_preview()

    def _rebuild_preview(self) -> None:
        ask = self._ask_radio.isChecked()
        self._preview_table.setRowCount(0)
        has_matches = bool(self._current_matches)
        self._no_match_label.setVisible(
            self._single_radio.isChecked() and not has_matches
        )

        for canon, types in self._current_matches:
            row = self._preview_table.rowCount()
            self._preview_table.insertRow(row)

            check = QTableWidgetItem()
            check.setFlags(
                Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled
            )
            check.setCheckState(Qt.CheckState.Checked)
            self._preview_table.setItem(row, 0, check)
            self._preview_table.setItem(row, 1, QTableWidgetItem(canon))

            if len(types) > 1 and ask:
                combo = QComboBox()
                for t in types:
                    combo.addItem(t)
                if "General" not in types:
                    combo.addItem("General")
                combo.setCurrentText("General")
                self._preview_table.setCellWidget(row, 2, combo)
            else:
                type_name = types[0] if len(types) == 1 else "General"
                self._preview_table.setItem(row, 2, QTableWidgetItem(type_name))

        self._preview_table.resizeRowsToContents()


class AmbiguousTagResolutionDialog(QDialog):
    """
    Shown before a bulk 'Ask per tag' import.  Presents every ambiguous tag
    found in the scanned captions and lets the user pick a category for each.
    The resolution is applied uniformly across all assets in the scope.
    """

    def __init__(
        self,
        ambiguous: list[tuple[str, list[str]]],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Resolve Ambiguous Tags")
        self.setMinimumWidth(380)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)

        note = QLabel(
            "The tags below exist in more than one category.\n"
            "Choose which category to use for each during this import."
        )
        note.setWordWrap(True)
        note.setStyleSheet("font-size: 11px; color: gray;")

        self._table = QTableWidget(len(ambiguous), 2)
        self._table.setHorizontalHeaderLabels(["Tag", "Category"])
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        hdr.resizeSection(0, 180)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)

        self._names: list[str] = []
        for row, (canon, types) in enumerate(ambiguous):
            self._names.append(canon.lower())
            self._table.setItem(row, 0, QTableWidgetItem(canon))
            combo = QComboBox()
            for t in types:
                combo.addItem(t)
            if "General" not in types:
                combo.addItem("General")
            combo.setCurrentText("General")
            self._table.setCellWidget(row, 1, combo)

        self._table.resizeRowsToContents()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(note)
        layout.addWidget(self._table)
        layout.addWidget(buttons)

    def resolution(self) -> dict[str, str]:
        """Return {lowercase_name: chosen_type_name}."""
        result = {}
        for row, key in enumerate(self._names):
            widget = self._table.cellWidget(row, 1)
            if isinstance(widget, QComboBox):
                result[key] = widget.currentText()
        return result
