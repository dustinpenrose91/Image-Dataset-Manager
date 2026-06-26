"""
imgdb_worker — single-threaded background executor for federation calls.

This module is the bridge between any UI toolkit and the synchronous
federation/imgdb library layer. It owns:

    - A dedicated worker thread that holds the Federation object
    - A FIFO job queue
    - A clean shutdown protocol that drains in-flight work

The worker is deliberately toolkit-agnostic. It knows nothing about Qt,
Tkinter, or any event loop. Results are delivered through plain callables
(`on_result`, `on_error`) that the caller supplies. A thin Qt adapter
lives in a separate module and translates those callbacks into signals
emitted on the Qt main thread.

Why a single worker thread instead of a thread pool:

    - SQLite connections are not safe to share across threads. The
      Federation object holds one connection per shard. Pinning all DB
      access to one thread sidesteps the thread-safety question entirely
      and serializes writes naturally without locks.
    - The bottleneck for this workload is disk I/O, not CPU dispatch.
      Multiple worker threads would not make scans or queries faster;
      they would only multiply the failure modes.

Threading model:

    - The main thread submits jobs via `submit()`.
    - The worker thread pulls jobs from a queue and runs them
      synchronously.
    - When a job finishes, the worker calls `on_result(value)` or
      `on_error(exception)` from the worker thread. The caller is
      responsible for marshalling those callbacks back to its own UI
      thread if it needs to (the Qt adapter does this with signals).
    - Jobs can be cancelled before they start running. A job that has
      already started cannot be cancelled — federation calls are not
      preemptible.

Lifecycle:

    worker = DBWorker()
    worker.start(lambda: federation.open_federation(cfg))
    handle = worker.submit(federation.scan_shard, fed, "alpha",
                           on_result=..., on_error=...)
    ...
    worker.shutdown()  # waits for in-flight job, closes federation

The federation object is constructed inside the worker thread (via the
factory passed to `start`) so that the SQLite connections are owned by
the thread that will use them. This is enforced by SQLite's default
`check_same_thread=True`.
"""

from __future__ import annotations

import itertools
import queue
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# Job priority levels.  Lower number = higher priority.
PRIORITY_NORMAL     = 0   # user-initiated actions (tag add, rename, page fetch, …)
PRIORITY_BACKGROUND = 1   # scan batches and other bulk operations

import federation
import imgdb


# ---------------------------------------------------------------------------
# Job representation
# ---------------------------------------------------------------------------

# Sentinel pushed onto the queue to tell the worker thread to exit.
_SHUTDOWN = object()


@dataclass
class Job:
    """A single unit of work submitted to the worker."""
    job_id: int
    fn: Callable[..., Any]
    args: tuple
    kwargs: dict
    on_result: Optional[Callable[[Any], None]] = None
    on_error: Optional[Callable[[BaseException], None]] = None
    # Set when cancelled before the worker has started running it.
    # Atomic-enough for our needs because Python sets/gets of single
    # references are GIL-protected.
    cancelled: bool = field(default=False)


class JobHandle:
    """
    Returned by `submit()`. The caller can use it to cancel the job
    before it starts running.
    """

    def __init__(self, job: Job):
        self._job = job

    @property
    def job_id(self) -> int:
        return self._job.job_id

    @property
    def cancelled(self) -> bool:
        return self._job.cancelled

    def cancel(self) -> bool:
        """
        Mark the job cancelled. Returns True if the cancellation was
        recorded; the job may still run if the worker has already
        picked it up. There is no way to interrupt a running federation
        call.
        """
        if self._job.cancelled:
            return False
        self._job.cancelled = True
        return True


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

FederationFactory = Callable[[], federation.Federation]


class DBWorker:
    """
    Single-threaded executor that owns a Federation and runs federation
    calls in FIFO order.

    The worker is created in an idle state and must be started with
    `start(factory)`. The factory is called inside the worker thread to
    construct the Federation, ensuring its SQLite connections are owned
    by the thread that will use them.
    """

    def __init__(self) -> None:
        # PriorityQueue gives lower-numbered priorities first.  Ties broken by
        # _seq so same-priority jobs execute in submission order (FIFO).
        self._queue: queue.PriorityQueue = queue.PriorityQueue()
        self._seq = itertools.count()
        self._thread: Optional[threading.Thread] = None
        self._fed: Optional[federation.Federation] = None
        self._startup_error: Optional[BaseException] = None
        self._ready = threading.Event()
        self._job_counter = itertools.count(1)
        self._on_warning: Optional[Callable[[str], None]] = None

    # -- lifecycle ----------------------------------------------------------

    def start(
        self,
        factory: FederationFactory,
        on_warning: Optional[Callable[[str], None]] = None,
        timeout: float = 10.0,
    ) -> None:
        """
        Spin up the worker thread and construct the Federation inside it.
        Blocks until the federation is ready or startup fails.

        `on_warning` is forwarded to `open_federation` and called from the
        worker thread for any shard that could not be opened.
        """
        if self._thread is not None:
            raise RuntimeError("DBWorker already started")
        self._on_warning = on_warning
        self._thread = threading.Thread(
            target=self._run,
            args=(factory,),
            name="imgdb-db-worker",
            daemon=False,
        )
        self._thread.start()
        if not self._ready.wait(timeout):
            raise TimeoutError(f"DBWorker did not start within {timeout}s")
        if self._startup_error is not None:
            err = self._startup_error
            self._thread.join()
            self._thread = None
            raise err

    def shutdown(self, timeout: float = 30.0) -> None:
        """
        Drain pending work, close the federation, and stop the worker
        thread. Safe to call multiple times.

        Cancelled jobs are skipped during the drain. Any job already
        submitted but not yet cancelled will run before shutdown
        completes.
        """
        if self._thread is None:
            return
        self._queue.put((PRIORITY_NORMAL, next(self._seq), _SHUTDOWN))
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise TimeoutError(
                f"DBWorker did not shut down within {timeout}s; "
                f"a federation call may be hung"
            )
        self._thread = None
        self._fed = None

    # -- submission ---------------------------------------------------------

    def submit(
        self,
        fn: Callable[..., Any],
        *args: Any,
        priority: int = PRIORITY_NORMAL,
        on_result: Optional[Callable[[Any], None]] = None,
        on_error: Optional[Callable[[BaseException], None]] = None,
        **kwargs: Any,
    ) -> JobHandle:
        """
        Schedule a federation call. Returns a JobHandle that can be used
        to cancel the job before it starts.

        priority=PRIORITY_NORMAL (default) runs before PRIORITY_BACKGROUND
        jobs even if the background jobs were submitted first. Use
        PRIORITY_BACKGROUND for bulk scan batches so user actions always
        feel instantaneous.

        The first positional argument to `fn` will be the live Federation
        object — pass federation functions directly:

            worker.submit(federation.scan_shard_batch, "alpha", session,
                          priority=PRIORITY_BACKGROUND,
                          on_result=self.on_batch_done)
        """
        if self._thread is None:
            raise RuntimeError("DBWorker is not running; call start() first")
        job = Job(
            job_id=next(self._job_counter),
            fn=fn,
            args=args,
            kwargs=kwargs,
            on_result=on_result,
            on_error=on_error,
        )
        self._queue.put((priority, next(self._seq), job))
        return JobHandle(job)

    # -- internals ----------------------------------------------------------

    def _run(self, factory: FederationFactory) -> None:
        """Worker thread entry point."""
        try:
            self._fed = factory()
        except BaseException as e:
            self._startup_error = e
            self._ready.set()
            return
        self._ready.set()

        try:
            while True:
                _prio, _seq, item = self._queue.get()
                if item is _SHUTDOWN:
                    break
                self._execute(item)
        finally:
            if self._fed is not None:
                try:
                    self._fed.close()
                except Exception:
                    pass

    def _execute(self, job: Job) -> None:
        if job.cancelled:
            return
        try:
            result = job.fn(self._fed, *job.args, **job.kwargs)
        except BaseException as e:
            if job.on_error is not None:
                try:
                    job.on_error(e)
                except Exception:
                    # A failing error handler must not take down the
                    # worker thread.
                    pass
            return
        if job.on_result is not None:
            try:
                job.on_result(result)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Convenience: synchronous wait (mainly for tests)
# ---------------------------------------------------------------------------

def call_sync(
    worker: DBWorker,
    fn: Callable[..., Any],
    *args: Any,
    timeout: float = 30.0,
    **kwargs: Any,
) -> Any:
    """
    Submit a job and block until it completes. Useful for tests and for
    one-off scripts. UI code should never call this — it defeats the
    point of the worker.
    """
    done = threading.Event()
    box: dict[str, Any] = {}

    def _ok(value: Any) -> None:
        box["value"] = value
        done.set()

    def _err(exc: BaseException) -> None:
        box["error"] = exc
        done.set()

    worker.submit(fn, *args, on_result=_ok, on_error=_err, priority=PRIORITY_NORMAL, **kwargs)
    if not done.wait(timeout):
        raise TimeoutError(f"call_sync timed out after {timeout}s")
    if "error" in box:
        raise box["error"]
    return box["value"]
