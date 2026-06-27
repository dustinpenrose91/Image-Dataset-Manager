"""
imgdb_thumbs_qt — Qt adapter for the toolkit-agnostic ThumbnailWorker.

Adds two things on top of the core:

1. A small in-memory LRU of decoded QPixmap objects in front of the
   on-disk WebP cache. The core can't provide this because QPixmap is
   Qt-specific. Decoded pixmaps are what the list view actually wants
   to draw, and decoding WebP for every paint event would be wasteful.

2. Thread-correct delivery of `on_ready` / `on_error` callbacks to the
   GUI thread via per-request Relay QObjects, mirroring the same pattern
   as imgdb_worker_qt.py.

PySide6 is imported lazily so this module is safe to import in
environments where Qt is not installed (tests, CLI usage, etc.).
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Callable, Optional

from imgdb_thumbs import ThumbnailWorker, PRIORITY_BACKGROUND, PRIORITY_VISIBLE, DEFAULT_THUMB_SIZE


# Qt symbols are populated on first use.
_QObject = None
_Signal = None
_QPixmap = None
_Relay = None


def _import_qt():
    try:
        from PySide6.QtCore import QObject, Signal
        from PySide6.QtGui import QPixmap
    except ImportError as e:
        raise ImportError(
            "imgdb_thumbs_qt requires PySide6. Install with: pip install PySide6"
        ) from e
    return QObject, Signal, QPixmap


def _ensure_qt() -> type:
    global _QObject, _Signal, _QPixmap, _Relay
    if _Relay is not None:
        return _Relay

    _QObject, _Signal, _QPixmap = _import_qt()

    class Relay(_QObject):
        ready = _Signal(str, str)              # asset_id, path
        failed = _Signal(str, object)          # asset_id, exception

        def __init__(self) -> None:
            super().__init__()

        def emit_ready(self, asset_id: str, path: str) -> None:
            self.ready.emit(asset_id, path)

        def emit_failed(self, asset_id: str, exc: BaseException) -> None:
            self.failed.emit(asset_id, exc)

    _Relay = Relay
    return _Relay


# ---------------------------------------------------------------------------
# QPixmap LRU
# ---------------------------------------------------------------------------

class PixmapLRU:
    """
    Bounded LRU cache of decoded QPixmap objects keyed by thumbnail
    file path. Default capacity is generous because QPixmaps are small
    (a 128x128 RGBA pixmap is about 64 KiB) and decoding is non-trivial.
    """

    def __init__(self, capacity: int = 512) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._capacity = capacity
        self._items: "OrderedDict[str, Any]" = OrderedDict()

    def get(self, path: str) -> Optional[Any]:
        item = self._items.get(path)
        if item is None:
            return None
        self._items.move_to_end(path)
        return item

    def put(self, path: str, pixmap: Any) -> None:
        if path in self._items:
            self._items.move_to_end(path)
            self._items[path] = pixmap
            return
        self._items[path] = pixmap
        while len(self._items) > self._capacity:
            self._items.popitem(last=False)

    def invalidate(self, path: str) -> None:
        self._items.pop(path, None)

    def clear(self) -> None:
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------

class QtThumbnailBridge:
    """
    Qt-aware front end for a ThumbnailWorker.

    The worker is owned by the caller; the bridge wraps it. Hot path
    (`get_pixmap`) is synchronous and lock-free for cache hits, which
    matters because list view paint code calls it on every visible row.

    Cold path: `request` enqueues a thumbnail job at the given priority
    and connects callbacks. When the worker reports the file is ready,
    the bridge decodes the WebP into a QPixmap on the GUI thread,
    inserts it into the LRU, and invokes the user's callback.
    """

    def __init__(
        self,
        worker: ThumbnailWorker,
        cache_capacity: int = 512,
    ) -> None:
        self._worker = worker
        self._cache = PixmapLRU(capacity=cache_capacity)
        self._inflight: set = set()  # strong refs to live Relay objects

    def get_pixmap(self, dest_path: str) -> Optional[Any]:
        """
        Return a cached QPixmap for the given on-disk thumbnail path,
        or None if not cached. Does NOT touch disk; use `request` to
        ensure the thumbnail is generated and decoded.
        """
        return self._cache.get(dest_path)

    def request(
        self,
        asset_id: str,
        src_abs_path: str,
        dest_abs_path: str,
        priority: int = PRIORITY_BACKGROUND,
        size: int = DEFAULT_THUMB_SIZE,
        fast: bool = False,
        on_ready: Optional[Callable[[str, Any], None]] = None,
        on_error: Optional[Callable[[str, BaseException], None]] = None,
    ) -> None:
        """
        Ensure the thumbnail for `asset_id` exists on disk and is
        decoded into the in-memory cache. The on_ready callback receives
        (asset_id, QPixmap) and is invoked on the GUI thread.

        If the QPixmap is already cached, on_ready is invoked
        synchronously and no worker job is enqueued.
        """
        cached = self._cache.get(dest_abs_path)
        if cached is not None:
            if on_ready is not None:
                on_ready(asset_id, cached)
            return

        Relay = _ensure_qt()
        relay = Relay()
        self._inflight.add(relay)

        def _on_ready_gui(aid: str, path: str) -> None:
            try:
                pixmap = _QPixmap(path)
                if not pixmap.isNull():
                    self._cache.put(path, pixmap)
                if on_ready is not None:
                    on_ready(aid, pixmap)
            finally:
                self._inflight.discard(relay)
                relay.deleteLater()

        def _on_error_gui(aid: str, exc: BaseException) -> None:
            try:
                if on_error is not None:
                    on_error(aid, exc)
            finally:
                self._inflight.discard(relay)
                relay.deleteLater()

        relay.ready.connect(_on_ready_gui)
        relay.failed.connect(_on_error_gui)

        self._worker.submit(
            asset_id=asset_id,
            src_abs_path=src_abs_path,
            dest_abs_path=dest_abs_path,
            priority=priority,
            size=size,
            fast=fast,
            on_ready=relay.emit_ready,
            on_error=relay.emit_failed,
        )

    def invalidate_thumb(self, dest_abs_path: str) -> None:
        """Remove one entry from the pixmap LRU so the next request regenerates it."""
        self._cache.invalidate(dest_abs_path)

    def bump_priority(self, dest_abs_path: str, new_priority: int) -> bool:
        """Elevate a queued job to a higher priority. See ThumbnailWorker.bump_priority."""
        return self._worker.bump_priority(dest_abs_path, new_priority)

    def cancel(self, asset_id: str) -> bool:
        return self._worker.cancel(asset_id)

    def clear_cache(self) -> None:
        self._cache.clear()
