"""
Auto-caption UI: the collapsible AutoCaptionPanel shown in the detail panel's
captions area (single- and multi-select), and the PromptManagerDialog for
creating / editing / renaming / deleting prompt presets.

The panel only gathers settings and emits an AutoCaptionRequest; queueing and
generation live in ui_controller.AutoCaptionController. Prompt presets are
plain text files managed through captioner's prompt-store functions.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
    QHBoxLayout, QInputDialog, QLabel, QListWidget, QMessageBox, QPlainTextEdit,
    QPushButton, QSizePolicy, QSpinBox, QSplitter, QToolButton, QVBoxLayout, QWidget,
)

import captioner
from ui_controller import AutoCaptionController, AutoCaptionRequest

_STATE_STYLE = {
    AutoCaptionController.UNLOADED: ("● Not loaded", "#888"),
    AutoCaptionController.LOADING: ("◌ Loading…", "#d68910"),
    AutoCaptionController.READY: ("● Ready", "#27ae60"),
    AutoCaptionController.FAILED: ("● Error", "#c0392b"),
}


def _toggle_button(text: str, expanded: bool) -> QToolButton:
    btn = QToolButton()
    btn.setText(text)
    btn.setCheckable(True)
    btn.setChecked(expanded)
    btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    btn.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
    btn.setAutoRaise(True)
    btn.toggled.connect(lambda on: btn.setArrowType(
        Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow))
    return btn


class AutoCaptionPanel(QWidget):
    """Compact settings + Generate button. One instance is shared between the
    single- and multi-select views (DetailPanel reparents it), so its state is
    naturally the same in both."""

    generate_requested = Signal(object)    # AutoCaptionRequest
    cancel_requested = Signal()
    unload_requested = Signal()
    manage_prompts_requested = Signal()
    settings_changed = Signal()            # persist on any user change

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._models: dict[str, Optional[int]] = {}   # name -> default max_pixels
        self._presets: dict[str, str] = {}
        self._target_count = 0
        self._running = False
        self._loaded_model = ""
        self._model_state = AutoCaptionController.UNLOADED

        self._toggle = _toggle_button("Auto-caption", expanded=False)
        self._toggle.setStyleSheet("font-weight: bold;")

        # -- body ---------------------------------------------------------
        self._model_combo = QComboBox()
        self._model_combo.setToolTip("Models are read from captioning/models/")
        self._state_label = QLabel()
        self._state_label.setStyleSheet("font-size: 11px;")
        self._unload_btn = QPushButton("Unload")
        self._unload_btn.setToolTip("Stop the model server and free GPU memory")
        self._unload_btn.clicked.connect(self.unload_requested)
        model_row = QHBoxLayout()
        model_row.setContentsMargins(0, 0, 0, 0)
        model_row.addWidget(self._model_combo, stretch=1)
        model_row.addWidget(self._state_label)
        model_row.addWidget(self._unload_btn)

        self._kind_combo = QComboBox()
        self._kind_combo.setEditable(True)
        self._kind_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._kind_combo.setToolTip("Caption type to write (pick existing or type a new one)")

        self._preset_combo = QComboBox()
        self._preset_combo.setToolTip("Prompt preset (loads into the prompt box below)")
        self._modified_label = QLabel("<i>edited</i>")
        self._modified_label.setStyleSheet("font-size: 11px; color: #888;")
        self._modified_label.setToolTip("The prompt differs from the selected preset; "
                                        "the text in the box is what will be used")
        self._modified_label.hide()
        manage_btn = QPushButton("✎")
        manage_btn.setFixedWidth(28)
        manage_btn.setToolTip("Manage prompt presets…")
        manage_btn.clicked.connect(self.manage_prompts_requested)
        preset_row = QHBoxLayout()
        preset_row.setContentsMargins(0, 0, 0, 0)
        preset_row.addWidget(self._preset_combo, stretch=1)
        preset_row.addWidget(self._modified_label)
        preset_row.addWidget(manage_btn)

        self._prompt_edit = QPlainTextEdit()
        self._prompt_edit.setFixedHeight(84)
        self._prompt_edit.setPlaceholderText("Prompt…")

        # Settings (collapsed by default)
        self._settings_toggle = _toggle_button("Settings", expanded=False)
        self._max_tokens = QSpinBox()
        self._max_tokens.setRange(16, captioner.MAX_NEW_TOKENS)
        self._max_tokens.setSingleStep(64)
        self._max_tokens.setValue(768)
        self._max_tokens.setToolTip("Maximum caption length in tokens")
        self._temperature = QDoubleSpinBox()
        self._temperature.setRange(0.0, 2.0)
        self._temperature.setSingleStep(0.05)
        self._temperature.setToolTip("0 = deterministic; higher = more varied wording")
        self._max_mp = QDoubleSpinBox()
        self._max_mp.setRange(0.1, captioner.MAX_IMAGE_PIXELS / 1e6)
        self._max_mp.setSingleStep(0.1)
        self._max_mp.setDecimals(2)
        self._max_mp.setSuffix(" MP")
        self._max_mp.setValue(1.0)
        self._max_mp.setToolTip("Images larger than this are downscaled before captioning.\n"
                                "Higher = more detail, slower, more GPU memory.\n"
                                "Resets to the model's default when the model changes.")
        settings_form = QFormLayout()
        settings_form.setContentsMargins(16, 0, 0, 0)
        settings_form.addRow("Max tokens", self._max_tokens)
        settings_form.addRow("Temperature", self._temperature)
        settings_form.addRow("Max image size", self._max_mp)
        self._settings_body = QWidget()
        self._settings_body.setLayout(settings_form)
        self._settings_body.setVisible(False)
        self._settings_toggle.toggled.connect(self._settings_body.setVisible)

        self._mode_combo = QComboBox()
        self._mode_combo.addItem("Overwrite", True)
        self._mode_combo.addItem("Skip", False)
        self._mode_combo.setToolTip("What to do when an image already has a caption of this type")
        self._generate_btn = QPushButton("Generate")
        self._generate_btn.clicked.connect(self._on_generate)
        gen_row = QHBoxLayout()
        gen_row.setContentsMargins(0, 0, 0, 0)
        gen_row.addWidget(QLabel("If exists:"))
        gen_row.addWidget(self._mode_combo)
        gen_row.addWidget(self._generate_btn, stretch=1)

        self._progress_label = QLabel()
        self._progress_label.setStyleSheet("font-size: 11px;")
        self._progress_label.setWordWrap(True)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.cancel_requested)
        self._progress_row = QWidget()
        pr = QHBoxLayout(self._progress_row)
        pr.setContentsMargins(0, 0, 0, 0)
        pr.addWidget(self._progress_label, stretch=1)
        pr.addWidget(cancel_btn)
        self._progress_row.hide()

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow("Model", model_row)
        form.addRow("Type", self._kind_combo)
        form.addRow("Prompt", preset_row)

        self._body = QWidget()
        bl = QVBoxLayout(self._body)
        bl.setContentsMargins(4, 0, 0, 4)
        bl.setSpacing(4)
        bl.addLayout(form)
        bl.addWidget(self._prompt_edit)
        bl.addWidget(self._settings_toggle)
        bl.addWidget(self._settings_body)
        bl.addLayout(gen_row)
        bl.addWidget(self._progress_row)
        self._body.setVisible(False)
        self._toggle.toggled.connect(self._body.setVisible)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(self._toggle)
        layout.addWidget(self._body)
        # Never grow taller than the content (no gaps when the host has room).
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

        # Change wiring
        self._model_combo.currentTextChanged.connect(self._on_model_changed)
        self._preset_combo.currentTextChanged.connect(self._on_preset_selected)
        self._prompt_edit.textChanged.connect(self._on_prompt_edited)
        self._kind_combo.currentTextChanged.connect(lambda _: self._update_enabled())
        for sig in (self._toggle.toggled, self._settings_toggle.toggled,
                    self._kind_combo.currentTextChanged, self._mode_combo.currentIndexChanged,
                    self._max_tokens.valueChanged, self._temperature.valueChanged,
                    self._max_mp.valueChanged):
            sig.connect(lambda *_: self.settings_changed.emit())
        self._render_state()
        self._update_enabled()

    # -- data population ----------------------------------------------------

    def set_models(self, specs: list[captioner.ModelSpec]) -> None:
        current = self._model_combo.currentText()
        self._models = {s.name: s.max_pixels for s in specs}
        self._model_combo.blockSignals(True)
        self._model_combo.clear()
        self._model_combo.addItems(list(self._models))
        if current in self._models:
            self._model_combo.setCurrentText(current)
        elif self._models:
            # With a placeholder set, QComboBox leaves the index at -1 when
            # items are added, so select the first model explicitly.
            self._model_combo.setCurrentIndex(0)
        self._model_combo.blockSignals(False)
        if self._model_combo.currentText() != current:
            # Selection changed implicitly (e.g. first population): apply the
            # new model's default image size, as a user selection would.
            default = self._models.get(self._model_combo.currentText())
            self._max_mp.setValue(default / 1e6 if default else 1.0)
        if not specs:
            self._model_combo.setPlaceholderText("No models found")
        self._update_enabled()

    def set_presets(self, presets: dict[str, str]) -> None:
        """Repopulate presets, keeping the selection. If the prompt box was an
        unedited copy of the selected preset, it follows that preset's new text."""
        current = self._preset_combo.currentText()
        was_unedited = self._presets.get(current) == self._prompt_text()
        self._presets = dict(presets)
        self._preset_combo.blockSignals(True)
        self._preset_combo.clear()
        self._preset_combo.addItems(list(self._presets))
        if current in self._presets:
            self._preset_combo.setCurrentText(current)
        else:
            self._preset_combo.setCurrentIndex(-1)
        self._preset_combo.blockSignals(False)
        if current in self._presets and was_unedited:
            self._prompt_edit.setPlainText(self._presets[current])
        self._update_modified()

    def set_caption_kinds(self, kinds: list[str]) -> None:
        current = self._kind_combo.currentText()
        self._kind_combo.blockSignals(True)
        self._kind_combo.clear()
        self._kind_combo.addItems(kinds)
        self._kind_combo.setCurrentText(current)
        self._kind_combo.blockSignals(False)

    def set_target_count(self, n: int) -> None:
        self._target_count = n
        self._generate_btn.setText("Generate" if n <= 1 else f"Generate for {n} images")
        self._update_enabled()

    # -- run state ----------------------------------------------------------

    def set_model_state(self, model: str, state: str) -> None:
        self._loaded_model = model if state in (AutoCaptionController.READY,
                                                AutoCaptionController.LOADING) else ""
        self._model_state = state
        self._render_state()

    def set_running(self, running: bool) -> None:
        self._running = running
        self._progress_row.setVisible(running)
        if not running:
            self._progress_label.clear()
        self._render_state()

    def set_progress(self, done: int, total: int, name: str) -> None:
        text = f"Captioning {min(done + 1, total)} / {total}"
        if name:
            text += f" — {name}"
        self._progress_label.setText(text)

    # -- persistence --------------------------------------------------------

    def state(self) -> dict:
        return {
            "expanded": self._toggle.isChecked(),
            "settings_expanded": self._settings_toggle.isChecked(),
            "model": self._model_combo.currentText(),
            "kind": self._kind_combo.currentText(),
            "preset": self._preset_combo.currentText(),
            "prompt": self._prompt_text(),
            "max_tokens": self._max_tokens.value(),
            "temperature": self._temperature.value(),
            "max_mp": self._max_mp.value(),
            "overwrite": bool(self._mode_combo.currentData()),
        }

    def restore_state(self, s: dict) -> None:
        self.blockSignals(True)
        self._toggle.setChecked(bool(s.get("expanded", False)))
        self._settings_toggle.setChecked(bool(s.get("settings_expanded", False)))
        if s.get("model") in self._models:
            self._model_combo.setCurrentText(s["model"])
        self._kind_combo.setCurrentText(s.get("kind", "") or "")
        # Only restore what was saved: absent keys keep current values (e.g.
        # the selected model's default image size applied by set_models).
        if "max_tokens" in s:
            self._max_tokens.setValue(int(s["max_tokens"]))
        if "temperature" in s:
            self._temperature.setValue(float(s["temperature"]))
        if "max_mp" in s:
            self._max_mp.setValue(float(s["max_mp"]))
        self._mode_combo.setCurrentIndex(0 if s.get("overwrite", True) else 1)
        preset = s.get("preset", "")
        self._preset_combo.blockSignals(True)
        if preset in self._presets:
            self._preset_combo.setCurrentText(preset)
        else:
            self._preset_combo.setCurrentIndex(-1)
        self._preset_combo.blockSignals(False)
        self._prompt_edit.setPlainText(s.get("prompt", "") or self._presets.get(preset, ""))
        self.blockSignals(False)
        self._update_modified()
        self._update_enabled()

    # -- internals ----------------------------------------------------------

    def _prompt_text(self) -> str:
        return self._prompt_edit.toPlainText().strip()

    def _on_model_changed(self, name: str) -> None:
        default = self._models.get(name)
        self._max_mp.setValue(default / 1e6 if default else 1.0)
        self._render_state()
        self.settings_changed.emit()

    def _on_preset_selected(self, name: str) -> None:
        if name in self._presets:
            self._prompt_edit.setPlainText(self._presets[name])
        self.settings_changed.emit()

    def _on_prompt_edited(self) -> None:
        self._update_modified()
        self._update_enabled()
        self.settings_changed.emit()

    def _update_modified(self) -> None:
        name = self._preset_combo.currentText()
        self._modified_label.setVisible(
            name in self._presets and self._presets[name] != self._prompt_text()
        )

    def _render_state(self) -> None:
        text, color = _STATE_STYLE[self._model_state]
        selected = self._model_combo.currentText()
        if self._loaded_model and self._loaded_model != selected:
            text = f"● {self._loaded_model} loaded"
            color = "#888"
        self._state_label.setText(text)
        self._state_label.setStyleSheet(f"font-size: 11px; color: {color};")
        self._unload_btn.setEnabled(bool(self._loaded_model) and not self._running)

    def _update_enabled(self) -> None:
        ok = (self._target_count > 0 and bool(self._models)
              and bool(self._kind_combo.currentText().strip()) and bool(self._prompt_text()))
        self._generate_btn.setEnabled(ok)

    def _on_generate(self) -> None:
        req = AutoCaptionRequest(
            model=self._model_combo.currentText(),
            kind=self._kind_combo.currentText().strip(),
            prompt=self._prompt_text(),
            params=captioner.CaptionParams(
                max_tokens=self._max_tokens.value(),
                temperature=self._temperature.value(),
                max_pixels=int(self._max_mp.value() * 1e6),
            ),
            overwrite=bool(self._mode_combo.currentData()),
        )
        self.generate_requested.emit(req)


class PromptManagerDialog(QDialog):
    """Create / edit / rename / delete prompt presets. Edits are saved when
    switching presets and when the dialog closes (like caption editors)."""

    def __init__(self, prompts_dir: str, select: str = "",
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Caption Prompt Presets")
        self.resize(760, 420)
        self._dir = prompts_dir
        self._current: Optional[str] = None
        self._saved_text = ""

        self._list = QListWidget()
        new_btn = QPushButton("New…")
        dup_btn = QPushButton("Duplicate…")
        self._rename_btn = QPushButton("Rename…")
        self._delete_btn = QPushButton("Delete")
        self._delete_btn.setStyleSheet("color: #c0392b;")
        new_btn.clicked.connect(self._new)
        dup_btn.clicked.connect(self._duplicate)
        self._rename_btn.clicked.connect(self._rename)
        self._delete_btn.clicked.connect(self._delete)
        self._dup_btn = dup_btn
        btns = QHBoxLayout()
        for b in (new_btn, dup_btn, self._rename_btn, self._delete_btn):
            btns.addWidget(b)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(self._list)
        ll.addLayout(btns)

        self._editor = QPlainTextEdit()
        self._editor.setPlaceholderText("Select or create a preset")
        hint = QLabel(f"Stored as text files in {prompts_dir}")
        hint.setStyleSheet("font-size: 11px; color: #888;")
        hint.setWordWrap(True)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self._editor)
        rl.addWidget(hint)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 2)

        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(splitter)
        layout.addWidget(close)

        self._list.currentTextChanged.connect(self._on_selected)
        self._reload(select)

    # -- list ---------------------------------------------------------------

    def _reload(self, select: str = "") -> None:
        try:
            names = list(captioner.list_prompts(self._dir))
        except OSError as e:
            QMessageBox.critical(self, "Prompt presets", str(e))
            names = []
        self._list.blockSignals(True)
        self._list.clear()
        self._list.addItems(names)
        self._list.blockSignals(False)
        if select in names:
            self._list.setCurrentRow(names.index(select))
        elif names:
            self._list.setCurrentRow(0)
        else:
            self._on_selected("")

    def _on_selected(self, name: str) -> None:
        self._save_current()
        self._current = name or None
        text = ""
        if self._current:
            try:
                text = captioner.read_prompt(self._dir, self._current)
            except OSError as e:
                QMessageBox.critical(self, "Prompt presets", str(e))
        self._saved_text = text
        self._editor.setPlainText(text)
        has = self._current is not None
        self._editor.setEnabled(has)
        for b in (self._rename_btn, self._delete_btn, self._dup_btn):
            b.setEnabled(has)

    def _save_current(self) -> None:
        if self._current is None:
            return
        text = self._editor.toPlainText()
        if text.strip() == self._saved_text.strip():
            return
        try:
            captioner.save_prompt(self._dir, self._current, text)
            self._saved_text = text
        except (OSError, ValueError) as e:
            QMessageBox.critical(self, "Prompt presets", f"Could not save '{self._current}': {e}")

    # -- actions ------------------------------------------------------------

    def _ask_name(self, title: str, initial: str = "") -> Optional[str]:
        name, ok = QInputDialog.getText(self, title, "Name:", text=initial)
        if not ok:
            return None
        try:
            return captioner.validate_prompt_name(name)
        except ValueError as e:
            QMessageBox.warning(self, title, str(e))
            return None

    def _exists(self, name: str) -> bool:
        return any(n.lower() == name.lower()
                   for n in (self._list.item(i).text() for i in range(self._list.count())))

    def _create(self, title: str, text: str) -> None:
        name = self._ask_name(title)
        if name is None:
            return
        if self._exists(name):
            QMessageBox.warning(self, title, f"A prompt named '{name}' already exists.")
            return
        self._save_current()
        try:
            captioner.save_prompt(self._dir, name, text)
        except (OSError, ValueError) as e:
            QMessageBox.critical(self, title, str(e))
            return
        self._current = None   # already saved; don't re-save on reload
        self._reload(name)
        self._editor.setFocus()

    def _new(self) -> None:
        self._create("New Prompt", "")

    def _duplicate(self) -> None:
        self._create("Duplicate Prompt", self._editor.toPlainText())

    def _rename(self) -> None:
        old = self._current
        if old is None:
            return
        new = self._ask_name("Rename Prompt", old)
        if new is None or new == old:
            return
        self._save_current()
        try:
            captioner.rename_prompt(self._dir, old, new)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "Rename Prompt", str(e))
            return
        self._current = None
        self._reload(new)

    def _delete(self) -> None:
        name = self._current
        if name is None:
            return
        if QMessageBox.question(self, "Delete Prompt", f"Delete prompt '{name}'?") \
                != QMessageBox.StandardButton.Yes:
            return
        try:
            captioner.delete_prompt(self._dir, name)
        except OSError as e:
            QMessageBox.critical(self, "Delete Prompt", str(e))
            return
        self._current = None
        self._reload()

    def done(self, result: int) -> None:
        self._save_current()
        super().done(result)
