"""
imgdb_worker_qt — Qt adapter for the toolkit-agnostic DBWorker.

The DBWorker invokes its on_result/on_error callbacks from the worker
thread. Qt widgets must only be touched from the GUI thread. This module
provides a thin QObject wrapper that translates worker callbacks into Qt
signals; signals are connected with the default `AutoConnection` type, so
they are delivered to the GUI thread via Qt's event loop when emitter
and receiver live in different threads.

Usage:

    from PySide6.QtWidgets import QApplication
    from imgdb_worker import DBWorker
    from imgdb_worker_qt import QtDBBridge
    import federation

    app = QApplication([])
    worker = DBWorker()
    worker.start(lambda: federation.open_federation("imgdb.conf"))
    bridge = QtDBBridge(worker)

    bridge.submit(federation.scan_shard, "alpha",
                  on_result=lambda summary: status.setText(str(summary)),
                  on_error=lambda exc: QMessageBox.critical(win, "Error", str(exc)))

    app.exec()
    worker.shutdown()

The bridge owns no state of its own beyond a reference to the worker
and the per-call Relay objects it spawns. Each Relay is a tiny QObject
with two signals; it lives until the job finishes, then deletes itself
via deleteLater(). This keeps the API as simple as the underlying
worker — one call, two callbacks — while guaranteeing thread-correct
delivery.

This module imports PySide6 lazily so the rest of the codebase remains
usable in environments where Qt is not installed (tests, the CLI, etc).
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from imgdb_worker import DBWorker, JobHandle


def _import_qt():
    try:
        from PySide6.QtCore import QObject, Signal, Slot
    except ImportError as e:
        raise ImportError(
            "imgdb_worker_qt requires PySide6. Install with: pip install PySide6"
        ) from e
    return QObject, Signal, Slot


# Module-level Qt symbols are populated on first use so importing this
# module does not require PySide6 to be installed.
_QObject = None
_Signal = None
_Slot = None
_Relay = None


def _ensure_relay_class() -> type:
    global _QObject, _Signal, _Slot, _Relay
    if _Relay is not None:
        return _Relay

    _QObject, _Signal, _Slot = _import_qt()

    class Relay(_QObject):
        """
        One-shot signal carrier for a single job. Created on the GUI
        thread, signals connected, then handed to the worker which
        invokes its emit_result / emit_error methods from the worker
        thread. Because the relay's affinity is the GUI thread, the
        emitted signals are queued onto the GUI event loop.
        """
        result_ready = _Signal(object)
        error_raised = _Signal(object)

        def __init__(self) -> None:
            super().__init__()

        def emit_result(self, value: Any) -> None:
            self.result_ready.emit(value)

        def emit_error(self, exc: BaseException) -> None:
            self.error_raised.emit(exc)

    _Relay = Relay
    return _Relay


class QtDBBridge:
    """
    Qt-aware front end for a DBWorker.

    The worker is created and started by the caller; the bridge does not
    own its lifecycle. This keeps shutdown explicit and lets the same
    worker be reused if a future iteration of the app rebuilds the main
    window.
    """

    def __init__(self, worker: DBWorker) -> None:
        self._worker = worker
        # Hold strong references to in-flight relays so they are not
        # garbage-collected before their signals fire.
        self._inflight: set = set()

    def root_abs_path(self, label: str) -> Optional[str]:
        """
        Return the absolute path for a root label, or None if the federation
        is not yet open or the label is not attached. Reads the worker's
        federation snapshot from the GUI thread — safe under CPython's GIL
        for a single dict lookup, and encapsulated here so callers don't
        need to pierce _worker._fed directly.
        """
        fed = self._worker._fed
        if fed is None:
            return None
        shard = fed.shards.get(label)
        return shard.abs_path if shard is not None else None

    def submit(
        self,
        fn: Callable[..., Any],
        *args: Any,
        on_result: Optional[Callable[[Any], None]] = None,
        on_error: Optional[Callable[[BaseException], None]] = None,
        **kwargs: Any,
    ) -> JobHandle:
        """
        Submit a federation call. The on_result / on_error callbacks
        are invoked on the GUI thread regardless of which thread the
        worker calls them from.
        """
        Relay = _ensure_relay_class()
        relay = Relay()
        self._inflight.add(relay)

        def _cleanup(_v: Any = None) -> None:
            self._inflight.discard(relay)
            relay.deleteLater()

        if on_result is not None:
            relay.result_ready.connect(on_result)
        relay.result_ready.connect(_cleanup)

        if on_error is not None:
            relay.error_raised.connect(on_error)
        relay.error_raised.connect(_cleanup)

        return self._worker.submit(
            fn, *args,
            on_result=relay.emit_result,
            on_error=relay.emit_error,
            **kwargs,
        )
