"""
PreviewWindow — image viewer with mask editor and crop tool.

Ported from the pygame reference tool with Qt replacing pygame.
No pygame dependency.

Controls (Mask mode):
  Left-drag       paint mask (black = inpaint region)
  Right-drag      erase mask
  Scroll wheel    brush size
  Ctrl+Scroll     zoom
  Middle-drag     pan
  Enter           save mask

Mask file: <basename>_mask.png beside the source image.
Black = inpaint / replace.  White = keep.  OneTrainer / standard inpainting format.

Crop mode:
  Drag to select region, then Save crop...
"""
from __future__ import annotations

import os
from enum import Enum, auto
from typing import Optional

import numpy as np

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor, QImage, QKeySequence, QPainter, QPen, QPixmap, QShortcut,
    QTransform,
)
from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QSizePolicy, QSlider, QVBoxLayout, QWidget,
)


class _Mode(Enum):
    VIEW = auto()
    MASK = auto()
    CROP = auto()


_MASK_COLOUR   = QColor(220,  50,  50, 160)   # semi-transparent red overlay
_CROP_BORDER   = QColor(255, 220,   0, 220)
_CROP_FILL     = QColor(255, 220,   0,  30)
_UNMASKED_BG   = 0x4D                          # grey level for "keep" areas


# ---------------------------------------------------------------------------
# Canvas
# ---------------------------------------------------------------------------

class _Canvas(QWidget):

    crop_rect_changed = Signal(int, int)   # w, h in image pixels

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setStyleSheet("background: #1a1a1a;")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._source:  Optional[QImage] = None   # original resolution
        # Mask stored as uint8 numpy array (h, w): 0=masked, 255=keep.
        # Kept in sync with self._mask_img (QImage Grayscale8) for painting.
        self._mask_arr: Optional[np.ndarray] = None
        self._mask_img: Optional[QImage]     = None  # view into _mask_arr

        self._zoom: float = 1.0
        self._fit_pending: bool = False
        self._user_zoomed: bool = False   # True once the user adjusts zoom manually
        self._pan:  QPointF = QPointF(0, 0)

        self._mode     = _Mode.VIEW
        self._brush_px = 50           # display-pixel diameter

        self._drawing  = False
        self._erasing  = False
        self._panning  = False
        self._last_pan: Optional[QPointF] = None
        self._last_draw: Optional[QPointF] = None   # image coords

        # Crop
        self._crop_start: Optional[QPointF] = None
        self._crop_rect:  Optional[QRect]   = None
        self._crop_drag   = False

    # -- public API ---------------------------------------------------------

    def set_source(self, img: Optional[QImage]) -> None:
        self._source    = img
        self._mask_arr  = None
        self._mask_img  = None
        self._crop_rect = None
        self._crop_start = None
        self._zoom = 1.0
        self._fit_pending = img is not None
        self._user_zoomed = False
        self._pan = QPointF(0, 0)
        self.update()

    def set_mask_from_image(self, grey_img: QImage) -> None:
        """Load a greyscale mask PNG (black=masked) into the editor."""
        if self._source is None:
            return
        # Scale to source size if needed.
        if grey_img.size() != self._source.size():
            grey_img = grey_img.scaled(self._source.size())
        g8 = grey_img.convertToFormat(QImage.Format.Format_Grayscale8)
        h, w = g8.height(), g8.width()
        ptr = g8.constBits()
        arr = np.frombuffer(ptr, dtype=np.uint8).reshape(h, g8.bytesPerLine())[:, :w].copy()
        self._set_mask_arr(arr)
        self.update()

    def set_mode(self, mode: _Mode) -> None:
        self._mode = mode
        if mode in (_Mode.MASK, _Mode.CROP):
            self.setCursor(Qt.CursorShape.CrossCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)
        if mode != _Mode.CROP:
            self._crop_rect = None
        self.update()

    def set_brush_px(self, px: int) -> None:
        self._brush_px = max(2, px)
        self.update()

    def clear_crop(self) -> None:
        self._crop_rect = None
        self._crop_start = None
        self.update()

    def clear_mask(self) -> None:
        self._mask_arr = None
        self._mask_img = None
        self.update()

    def mask_as_image(self) -> Optional[QImage]:
        """Return the mask as a Grayscale8 QImage (black=masked, white=keep)."""
        if self._mask_arr is None:
            return None
        arr = self._mask_arr
        h, w = arr.shape
        img = QImage(arr.data, w, h, w, QImage.Format.Format_Grayscale8)
        return img.copy()   # copy so the returned image owns its data

    def crop_rect(self) -> Optional[QRect]:
        return self._crop_rect

    def source_image(self) -> Optional[QImage]:
        return self._source

    # -- painting -----------------------------------------------------------

    def paintEvent(self, _) -> None:
        if self._source is None:
            return
        if self._fit_pending and self.width() > 0 and self.height() > 0:
            self._zoom = min(1.0,
                             self.width() / self._source.width(),
                             self.height() / self._source.height())
            self._fit_pending = False
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        ir = self._image_rect()

        # 1. Image
        p.drawImage(ir, self._source)

        # 2. Mask overlay (semi-transparent red where mask=0)
        if self._mask_arr is not None:
            overlay = self._build_overlay()
            p.drawImage(ir, overlay)

        # 3. Crop rect
        if self._crop_rect and self._mode == _Mode.CROP:
            wr = self._img_rect_to_widget(self._crop_rect)
            p.setPen(QPen(_CROP_BORDER, 2, Qt.PenStyle.DashLine))
            from PySide6.QtGui import QBrush
            p.setBrush(QBrush(_CROP_FILL))
            p.drawRect(wr)
            p.setPen(QPen(QColor(255, 255, 255)))
            p.drawText(int(wr.x()) + 4, int(wr.y()) + 14,
                       f"{self._crop_rect.width()} × {self._crop_rect.height()}")

        # 4. Brush cursor (mask mode only)
        if self._mode == _Mode.MASK:
            mp = self.mapFromGlobal(self.cursor().pos())
            if self.rect().contains(mp):
                r = self._brush_px // 2
                colour = QColor(255, 80, 80, 200) if not self._erasing else QColor(80, 80, 255, 200)
                p.setPen(QPen(colour, 1))
                from PySide6.QtGui import QBrush
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(mp, r, r)

        p.end()

    def _build_overlay(self) -> QImage:
        """Convert mask array → ARGB32 red overlay (fast via numpy)."""
        arr = self._mask_arr
        h, w = arr.shape
        # BGRA layout for QImage Format_ARGB32 on little-endian
        rgba = np.zeros((h, w, 4), dtype=np.uint8)
        m = arr < 128
        rgba[m, 0] = _MASK_COLOUR.blue()
        rgba[m, 1] = _MASK_COLOUR.green()
        rgba[m, 2] = _MASK_COLOUR.red()
        rgba[m, 3] = _MASK_COLOUR.alpha()
        img = QImage(rgba.tobytes(), w, h, w * 4, QImage.Format.Format_ARGB32)
        return img

    # -- mouse events -------------------------------------------------------

    def mousePressEvent(self, event) -> None:
        if self._source is None:
            return
        pos = event.position()
        if event.button() == Qt.MouseButton.MiddleButton:
            self._panning = True
            self._last_pan = pos
        elif self._mode == _Mode.MASK:
            if event.button() == Qt.MouseButton.LeftButton:
                self._drawing = True
                self._erasing = False
            elif event.button() == Qt.MouseButton.RightButton:
                self._drawing = True
                self._erasing = True
            if self._drawing:
                ip = self._widget_to_image(pos)
                self._draw_brush(ip)
                self._last_draw = ip
        elif self._mode == _Mode.CROP:
            if event.button() == Qt.MouseButton.LeftButton:
                self._crop_start = self._clamp_to_image(self._widget_to_image(pos))
                self._crop_rect  = None
                self._crop_drag  = True

    def mouseMoveEvent(self, event) -> None:
        if self._source is None:
            return
        pos = event.position()
        self.update()   # brush cursor

        if self._panning and self._last_pan is not None:
            delta = pos - self._last_pan
            self._pan += delta
            self._last_pan = pos
            self.update()

        elif self._drawing and self._mode == _Mode.MASK:
            ip = self._widget_to_image(pos)
            self._draw_stroke(self._last_draw, ip)
            self._last_draw = ip

        elif self._crop_drag and self._crop_start is not None:
            end = self._clamp_to_image(self._widget_to_image(pos))
            start = self._crop_start
            self._crop_rect = QRect(
                QPoint(int(start.x()), int(start.y())),
                QPoint(int(end.x()), int(end.y())),
            ).normalized()
            self.crop_rect_changed.emit(self._crop_rect.width(), self._crop_rect.height())
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        self._drawing   = False
        self._erasing   = False
        self._panning   = False
        self._last_pan  = None
        self._last_draw = None
        self._crop_drag = False

    def wheelEvent(self, event) -> None:
        if self._source is None:
            return
        delta = event.angleDelta().y()
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            # Zoom towards the cursor.
            mp = event.position()
            img_pt = self._widget_to_image(mp)
            factor = 1.1 if delta > 0 else (1 / 1.1)
            self._zoom = max(0.05, min(20.0, self._zoom * factor))
            self._user_zoomed = True
            # Adjust pan so the image point under the cursor stays fixed.
            new_wp = self._image_to_widget(img_pt)
            self._pan += mp - new_wp
            self.update()
        else:
            # Brush size.
            step = 2 if delta > 0 else -2
            self._brush_px = max(2, min(300, self._brush_px + step * 2))
            self.update()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Return and self._mode == _Mode.MASK:
            # Enter = quick-save shortcut (forwarded to parent).
            p = self.parent()
            if hasattr(p, '_save_mask'):
                p._save_mask()
        else:
            super().keyPressEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._source is not None and not self._user_zoomed:
            self._zoom = min(1.0,
                             self.width() / self._source.width(),
                             self.height() / self._source.height())
            self._fit_pending = False
        self.update()

    # -- coordinate helpers -------------------------------------------------

    def _image_rect(self) -> QRectF:
        """Widget-space rect where the image is drawn."""
        if self._source is None:
            return QRectF()
        iw = self._source.width()  * self._zoom
        ih = self._source.height() * self._zoom
        cx = self.width()  / 2 + self._pan.x()
        cy = self.height() / 2 + self._pan.y()
        return QRectF(cx - iw / 2, cy - ih / 2, iw, ih)

    def _widget_to_image(self, wp: QPointF) -> QPointF:
        r = self._image_rect()
        if r.isEmpty() or self._source is None:
            return QPointF()
        return QPointF(
            (wp.x() - r.x()) / self._zoom,
            (wp.y() - r.y()) / self._zoom,
        )

    def _clamp_to_image(self, p: QPointF) -> QPointF:
        if self._source is None:
            return p
        return QPointF(
            max(0.0, min(p.x(), float(self._source.width()))),
            max(0.0, min(p.y(), float(self._source.height()))),
        )

    def _image_to_widget(self, ip: QPointF) -> QPointF:
        r = self._image_rect()
        return QPointF(r.x() + ip.x() * self._zoom, r.y() + ip.y() * self._zoom)

    def _img_rect_to_widget(self, r: QRect) -> QRectF:
        tl = self._image_to_widget(QPointF(r.x(), r.y()))
        br = self._image_to_widget(QPointF(r.right(), r.bottom()))
        return QRectF(tl, br)

    # -- brush / mask -------------------------------------------------------

    def _set_mask_arr(self, arr: np.ndarray) -> None:
        self._mask_arr = arr
        # Keep a QImage view for painting (shares memory).
        h, w = arr.shape
        self._mask_img = QImage(arr.data, w, h, w, QImage.Format.Format_Grayscale8)

    def _ensure_mask(self) -> None:
        if self._mask_arr is None and self._source is not None:
            arr = np.full(
                (self._source.height(), self._source.width()), 255, dtype=np.uint8
            )
            self._set_mask_arr(arr)

    def _brush_radius_image(self) -> float:
        """Brush radius in image pixels (matches display size regardless of zoom)."""
        return max(1.0, (self._brush_px / 2) / self._zoom)

    def _draw_brush(self, ip: QPointF) -> None:
        self._ensure_mask()
        if self._mask_img is None:
            return
        p = QPainter(self._mask_img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self._brush_radius_image()
        colour = QColor(255, 255, 255) if self._erasing else QColor(0, 0, 0)
        p.setBrush(colour)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(ip, r, r)
        p.end()
        self.update()

    def _draw_stroke(self, a: Optional[QPointF], b: QPointF) -> None:
        if a is None:
            self._draw_brush(b)
            return
        dx = b.x() - a.x()
        dy = b.y() - a.y()
        dist = (dx * dx + dy * dy) ** 0.5
        step = max(1.0, self._brush_radius_image() * 0.5)
        if dist < step:
            self._draw_brush(b)
            return
        n = max(1, int(dist / step))
        for i in range(n + 1):
            t = i / n
            self._draw_brush(QPointF(a.x() + dx * t, a.y() + dy * t))


# ---------------------------------------------------------------------------
# Toolbars
# ---------------------------------------------------------------------------

class _MaskToolbar(QWidget):
    brush_changed  = Signal(int)
    clear_clicked  = Signal()
    save_clicked   = Signal()
    load_clicked   = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(4, 2, 4, 2)
        row.setSpacing(6)
        row.addWidget(QLabel("Brush:"))
        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(2, 300)
        self._slider.setValue(50)
        self._slider.setFixedWidth(110)
        self._slider.valueChanged.connect(self.brush_changed)
        self._lbl = QLabel("50 px")
        self._slider.valueChanged.connect(lambda v: self._lbl.setText(f"{v} px"))
        row.addWidget(self._slider)
        row.addWidget(self._lbl)
        row.addWidget(QLabel("  Left=draw  Right=erase  Scroll=size  Ctrl+Scroll=zoom  Middle=pan  Enter/Ctrl+S=save"))
        row.addStretch()
        for text, sig in [("Clear", self.clear_clicked), ("Load…", self.load_clicked),
                          ("Save", self.save_clicked)]:
            btn = QPushButton(text)
            if text == "Save":
                btn.setStyleSheet("font-weight:bold;")
            btn.clicked.connect(sig)
            row.addWidget(btn)


class _CropToolbar(QWidget):
    apply_clicked        = Signal()
    cancel_clicked       = Signal()
    flip_h_clicked       = Signal()
    flip_v_clicked       = Signal()
    rotate_left_clicked  = Signal()
    rotate_right_clicked = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(4, 2, 4, 2)
        row.setSpacing(6)
        row.addWidget(QLabel("Drag to select crop region."))
        self._dims = QLabel()
        row.addWidget(self._dims)

        sep = QLabel("  |  ")
        sep.setStyleSheet("color: gray;")
        row.addWidget(sep)

        for label, tooltip, sig in (
            ("↔ Flip H",  "Flip horizontally",         self.flip_h_clicked),
            ("↕ Flip V",  "Flip vertically",            self.flip_v_clicked),
            ("↺ 90°",     "Rotate 90° counter-clockwise", self.rotate_left_clicked),
            ("↻ 90°",     "Rotate 90° clockwise",      self.rotate_right_clicked),
        ):
            btn = QPushButton(label)
            btn.setToolTip(tooltip)
            btn.clicked.connect(sig)
            row.addWidget(btn)

        row.addStretch()
        apply = QPushButton("Apply crop")
        apply.setStyleSheet("font-weight:bold;")
        apply.clicked.connect(self.apply_clicked)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.cancel_clicked)
        row.addWidget(apply)
        row.addWidget(cancel)

    def set_dims(self, w: int, h: int) -> None:
        self._dims.setText(f"{w} × {h}" if w and h else "")


# ---------------------------------------------------------------------------
# PreviewWindow
# ---------------------------------------------------------------------------

class PreviewWindow(QWidget):
    """
    Persistent floating preview / mask-editor / crop window.

    Public API (stable):
        set_image(abs_path, rel_path)
        visibility_changed  [Signal(bool)]
    """

    visibility_changed = Signal(bool)
    image_modified     = Signal(str)    # abs_path — emitted after any destructive save

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle("Preview")
        self.resize(900, 650)
        self.setMinimumSize(300, 200)

        self._abs_path: Optional[str] = None
        self._rel_path: str = ""

        # Mode buttons
        self._view_btn = QPushButton("View")
        self._mask_btn = QPushButton("Mask")
        self._crop_btn = QPushButton("Crop && Transform")
        for b in (self._view_btn, self._mask_btn, self._crop_btn):
            b.setCheckable(True)
        self._view_btn.setChecked(True)

        mode_row = QHBoxLayout()
        mode_row.setSpacing(2)
        mode_row.addWidget(self._view_btn)
        mode_row.addWidget(self._mask_btn)
        mode_row.addWidget(self._crop_btn)
        mode_row.addStretch()

        self._mask_tb = _MaskToolbar()
        self._crop_tb = _CropToolbar()
        self._mask_tb.hide()
        self._crop_tb.hide()

        self._canvas = _Canvas(self)

        tb_row = QHBoxLayout()
        tb_row.setContentsMargins(4, 4, 4, 0)
        tb_row.setSpacing(8)
        tb_row.addLayout(mode_row)
        tb_row.addWidget(self._mask_tb, stretch=1)
        tb_row.addWidget(self._crop_tb, stretch=1)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addLayout(tb_row)
        root.addWidget(self._canvas, stretch=1)

        # Wiring
        self._view_btn.clicked.connect(lambda: self._set_mode(_Mode.VIEW))
        self._mask_btn.clicked.connect(lambda: self._set_mode(_Mode.MASK))
        self._crop_btn.clicked.connect(lambda: self._set_mode(_Mode.CROP))

        self._mask_tb.brush_changed.connect(self._canvas.set_brush_px)
        self._mask_tb.clear_clicked.connect(self._canvas.clear_mask)
        self._mask_tb.save_clicked.connect(self._save_mask)
        self._mask_tb.load_clicked.connect(self._load_mask)

        self._crop_tb.apply_clicked.connect(self._save_crop)
        self._crop_tb.cancel_clicked.connect(lambda: (
            self._canvas.clear_crop(),
            self._crop_tb.set_dims(0, 0),
        ))
        self._canvas.crop_rect_changed.connect(self._crop_tb.set_dims)
        self._crop_tb.flip_h_clicked.connect(
            lambda: self._apply_transform(lambda img: img.mirrored(True, False))
        )
        self._crop_tb.flip_v_clicked.connect(
            lambda: self._apply_transform(lambda img: img.mirrored(False, True))
        )
        self._crop_tb.rotate_left_clicked.connect(
            lambda: self._apply_transform(
                lambda img: img.transformed(QTransform().rotate(-90))
            )
        )
        self._crop_tb.rotate_right_clicked.connect(
            lambda: self._apply_transform(
                lambda img: img.transformed(QTransform().rotate(90))
            )
        )

        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=self.hide)
        QShortcut(QKeySequence("V"), self, activated=lambda: self._set_mode(_Mode.VIEW))
        QShortcut(QKeySequence("M"), self, activated=lambda: self._set_mode(_Mode.MASK))
        QShortcut(QKeySequence("C"), self, activated=lambda: self._set_mode(_Mode.CROP))
        QShortcut(QKeySequence("Ctrl+S"), self, activated=self._save_current)

    # -- public API ---------------------------------------------------------

    def set_image(self, abs_path: Optional[str], rel_path: str = "") -> None:
        self._abs_path = abs_path
        self._rel_path = rel_path
        if abs_path is None:
            self._canvas.set_source(None)
            self.setWindowTitle("Preview")
            return
        img = QImage(abs_path)
        if img.isNull():
            self._canvas.set_source(None)
            self.setWindowTitle(f"Preview — {rel_path} (cannot load)")
            return
        self._canvas.set_source(img)
        self.setWindowTitle(
            f"Preview — {rel_path or abs_path}  [{img.width()} × {img.height()}]"
        )
        # Auto-load existing mask.
        mp = _mask_path_for(abs_path)
        if mp and os.path.isfile(mp):
            mask_img = QImage(mp)
            if not mask_img.isNull():
                self._canvas.set_mask_from_image(mask_img)

    def clear(self) -> None:
        self.set_image(None)

    # -- mode ---------------------------------------------------------------

    def _set_mode(self, mode: _Mode) -> None:
        self._view_btn.setChecked(mode == _Mode.VIEW)
        self._mask_btn.setChecked(mode == _Mode.MASK)
        self._crop_btn.setChecked(mode == _Mode.CROP)
        self._mask_tb.setVisible(mode == _Mode.MASK)
        self._crop_tb.setVisible(mode == _Mode.CROP)
        self._canvas.set_mode(mode)

    # -- mode save dispatch -------------------------------------------------

    def _save_current(self) -> None:
        if self._mask_btn.isChecked():
            self._save_mask()
        elif self._crop_btn.isChecked():
            self._save_crop()

    # -- mask ---------------------------------------------------------------

    def _save_mask(self) -> None:
        dest = _mask_path_for(self._abs_path) if self._abs_path else None
        img = self._canvas.mask_as_image()
        if img is None:
            if dest and os.path.isfile(dest):
                try:
                    os.remove(dest)
                except OSError as e:
                    QMessageBox.critical(self, "Save mask", f"Failed to delete:\n{dest}\n{e}")
                    return
                self.setWindowTitle(
                    self.windowTitle().split(" [mask")[0] + " [mask cleared]"
                )
            else:
                QMessageBox.information(self, "Mask", "No mask painted yet.")
            return
        if dest is None:
            return
        if not img.save(dest, "PNG"):
            QMessageBox.critical(self, "Save mask", f"Failed to write:\n{dest}")
            return
        self.setWindowTitle(
            self.windowTitle().split(" [mask")[0] + " [mask saved]"
        )

    def _load_mask(self) -> None:
        default = _mask_path_for(self._abs_path) if self._abs_path else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "Load mask", default or "", "PNG images (*.png)"
        )
        if not path:
            return
        img = QImage(path)
        if img.isNull():
            QMessageBox.critical(self, "Load mask", "Could not read image.")
            return
        self._canvas.set_mask_from_image(img)

    # -- crop & transform ---------------------------------------------------

    def _apply_transform(self, transform_fn) -> None:
        src = self._canvas.source_image()
        if src is None or not self._abs_path:
            return
        reply = QMessageBox.question(
            self, "Apply transform",
            f"Overwrite the original file with the transformed image?\n\n{self._abs_path}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        result = transform_fn(src)
        if not result.save(self._abs_path):
            QMessageBox.critical(self, "Transform", f"Failed to write:\n{self._abs_path}")
            return
        self.image_modified.emit(self._abs_path)
        self.set_image(self._abs_path, self._rel_path)

    def _save_crop(self) -> None:
        rect = self._canvas.crop_rect()
        src  = self._canvas.source_image()
        if not rect or rect.isEmpty() or src is None:
            QMessageBox.information(self, "Crop", "Draw a crop region first.")
            return
        if not self._abs_path:
            return
        reply = QMessageBox.question(
            self, "Apply crop",
            f"Overwrite the original file with the cropped region?\n\n{self._abs_path}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        cropped = src.copy(rect)
        if not cropped.save(self._abs_path):
            QMessageBox.critical(self, "Crop", f"Failed to write:\n{self._abs_path}")
            return
        self.image_modified.emit(self._abs_path)
        self.set_image(self._abs_path, self._rel_path)

    # -- visibility ---------------------------------------------------------

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.visibility_changed.emit(True)

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self.visibility_changed.emit(False)


def _mask_path_for(image_path: Optional[str]) -> Optional[str]:
    if not image_path:
        return None
    base, _ = os.path.splitext(image_path)
    return f"{base}_mask.png"
