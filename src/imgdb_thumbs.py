"""
imgdb_thumbs — toolkit-agnostic thumbnail generator and disk cache.

Layout
------
Thumbnails live under <root>/imgdb_thumbs/ with two-level hex fan-out
based on the asset_id, and the current content hash embedded in the
filename:

    <root>/imgdb_thumbs/<aa>/<asset_id>-<hash12>.webp

where `aa` is the first two hex characters of `asset_id` and `hash12`
is the first 12 hex characters of the asset's `file_hash`. The hash
in the filename means an in-place edit naturally produces a new path,
so the new thumbnail does not overwrite the old one and stale
thumbnails are simply orphaned. A later sweep can collect them.

Threading model
---------------
Single dedicated worker thread (same reasoning as imgdb_worker: the
bottleneck is decode + disk I/O, not CPU dispatch, and one thread keeps
the implementation simple). Three priority levels:

    - PRIORITY_SELECTED:   HQ for the image currently open in the detail panel
    - PRIORITY_VISIBLE:    LQ for rows currently visible in the table
    - PRIORITY_BACKGROUND: bulk pre-generation for off-screen rows

Submitting a job for a dest_abs_path that is already enqueued at a
lower priority cancels the old job and re-enqueues at the higher
priority (bump). The inflight dict is keyed by dest_abs_path so that
LQ and HQ thumbnails for the same asset do not collide in dedup.

Jobs are cancellable: setting Job.cancelled = True causes the worker to
skip the job when it pops it.

Result delivery is the same callback-based contract as imgdb_worker:
`on_ready(asset_id, path)` and `on_error(asset_id, exception)` are
invoked from the worker thread. A Qt adapter lives in a separate module
and translates these into signals on the GUI thread.

The core has no Qt dependency. Memory cache is left to the Qt adapter,
because decoded pixmaps are toolkit-specific.
"""

from __future__ import annotations

import heapq
import itertools
import os
import time
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

try:
    from PIL import Image
except ImportError as e:
    raise ImportError(
        "imgdb_thumbs requires the 'Pillow' package. Install with: pip install Pillow"
    ) from e

import imgdb


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

THUMB_SIZE_LQ   = 128          # table grid: fast BILINEAR, stored as <id>-<hash12>.webp
THUMB_SIZE_HQ   = 256          # detail panel: LANCZOS, stored as <id>-<hash12>-hq.webp
DEFAULT_THUMB_SIZE = THUMB_SIZE_LQ
THUMB_FORMAT = "WEBP"
THUMB_EXT = ".webp"
THUMB_QUALITY = 85
THUMB_QUALITY_LQ = 70          # lower quality for fast LQ generation
HASH_PREFIX_LEN = 12           # how many hex chars of file_hash to embed

BACKGROUND_JOB_SLEEP = 0.05   # seconds to yield between background thumbnail jobs

# Priority values: smaller = higher priority.
PRIORITY_SELECTED   = 0   # HQ for the actively-selected image
PRIORITY_VISIBLE    = 1   # LQ for currently-visible table rows
PRIORITY_BACKGROUND = 2   # bulk generation for off-screen rows


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def thumb_path(root_abs_path: str, asset_id: str, file_hash: str) -> str:
    """LQ (fast) thumbnail path. Pure function."""
    base = imgdb.thumbs_dir(root_abs_path)
    fan = asset_id[:2]
    name = f"{asset_id}-{file_hash[:HASH_PREFIX_LEN]}{THUMB_EXT}"
    return str(base / fan / name)


def thumb_path_hq(root_abs_path: str, asset_id: str, file_hash: str) -> str:
    """HQ (LANCZOS, 256px) thumbnail path. Pure function."""
    base = imgdb.thumbs_dir(root_abs_path)
    fan = asset_id[:2]
    name = f"{asset_id}-{file_hash[:HASH_PREFIX_LEN]}-hq{THUMB_EXT}"
    return str(base / fan / name)


def ensure_thumb_dir(root_abs_path: str, asset_id: str) -> None:
    """Create the per-fan-out subdirectory if it doesn't exist."""
    base = imgdb.thumbs_dir(root_abs_path)
    os.makedirs(base / asset_id[:2], exist_ok=True)


# ---------------------------------------------------------------------------
# Generation primitive
# ---------------------------------------------------------------------------

def generate_thumbnail(
    src_abs_path: str,
    dest_abs_path: str,
    size: int = DEFAULT_THUMB_SIZE,
    fast: bool = False,
) -> None:
    """
    Decode `src_abs_path`, fit it inside a `size`x`size` box preserving
    aspect ratio, and write it to `dest_abs_path` as WebP.

    fast=True uses BILINEAR resampling and lower quality for bulk scanning.
    fast=False uses LANCZOS for detail-panel HQ thumbnails.

    Atomic via write-to-temp + rename so a partially written thumbnail
    is never observed by readers.
    """
    resampling = Image.Resampling.BILINEAR if fast else Image.Resampling.LANCZOS
    quality    = THUMB_QUALITY_LQ if fast else THUMB_QUALITY
    method     = 0 if fast else 4
    os.makedirs(os.path.dirname(dest_abs_path), exist_ok=True)
    with Image.open(src_abs_path) as im:
        im.thumbnail((size, size), resampling)
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA" if "A" in im.mode else "RGB")
        tmp = dest_abs_path + ".tmp"
        im.save(tmp, format=THUMB_FORMAT, quality=quality, method=method)
    os.replace(tmp, dest_abs_path)


# ---------------------------------------------------------------------------
# Job and queue
# ---------------------------------------------------------------------------

@dataclass
class ThumbJob:
    """A single thumbnail-generation request."""
    asset_id: str
    src_abs_path: str
    dest_abs_path: str
    priority: int = PRIORITY_BACKGROUND
    size: int = DEFAULT_THUMB_SIZE
    fast: bool = False
    on_ready: Optional[Callable[[str, str], None]] = None
    on_error: Optional[Callable[[str, BaseException], None]] = None
    cancelled: bool = field(default=False)


_SHUTDOWN = object()


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

class ThumbnailWorker:
    """
    Single-threaded thumbnail generator with priority queue and dedup.

    The dedup table maps dest_abs_path -> current Job so that LQ and HQ
    thumbnails for the same asset can be queued independently. If a new
    submission arrives for a dest_abs_path that is already enqueued at a
    lower priority (higher number), the existing job is cancelled and the
    new one is enqueued at the higher priority (bump). Same-or-lower
    priority duplicates are dropped.
    """

    def __init__(self) -> None:
        self._heap: list[tuple[int, int, ThumbJob]] = []  # (priority, seq, job)
        self._seq = itertools.count()
        self._lock = threading.Lock()
        self._not_empty = threading.Condition(self._lock)
        self._inflight: dict[str, ThumbJob] = {}  # dest_abs_path -> job
        self._shutdown = False
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("ThumbnailWorker already started")
        self._thread = threading.Thread(
            target=self._run,
            name="imgdb-thumb-worker",
            daemon=False,
        )
        self._thread.start()

    def shutdown(self, timeout: float = 10.0) -> None:
        if self._thread is None:
            return
        with self._not_empty:
            self._shutdown = True
            self._not_empty.notify_all()
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise TimeoutError(
                f"ThumbnailWorker did not shut down within {timeout}s"
            )
        self._thread = None

    # -- submission ---------------------------------------------------------

    def submit(
        self,
        asset_id: str,
        src_abs_path: str,
        dest_abs_path: str,
        priority: int = PRIORITY_BACKGROUND,
        size: int = DEFAULT_THUMB_SIZE,
        fast: bool = False,
        on_ready: Optional[Callable[[str, str], None]] = None,
        on_error: Optional[Callable[[str, BaseException], None]] = None,
    ) -> None:
        """
        Enqueue a thumbnail job. If the destination already exists this
        is a no-op and on_ready is invoked synchronously.

        Submitting an already-queued dest_abs_path at a higher priority
        bumps it; at the same or lower priority is a no-op.
        """
        if os.path.exists(dest_abs_path):
            if on_ready is not None:
                on_ready(asset_id, dest_abs_path)
            return

        job = ThumbJob(
            asset_id=asset_id,
            src_abs_path=src_abs_path,
            dest_abs_path=dest_abs_path,
            priority=priority,
            size=size,
            fast=fast,
            on_ready=on_ready,
            on_error=on_error,
        )
        with self._not_empty:
            existing = self._inflight.get(dest_abs_path)
            if existing is not None and not existing.cancelled:
                if priority < existing.priority:
                    existing.cancelled = True
                    self._inflight[dest_abs_path] = job
                    heapq.heappush(self._heap, (priority, next(self._seq), job))
                    self._not_empty.notify()
                # else: same-or-lower priority duplicate, drop it
                return
            self._inflight[dest_abs_path] = job
            heapq.heappush(self._heap, (priority, next(self._seq), job))
            self._not_empty.notify()

    def bump_priority(self, dest_abs_path: str, new_priority: int) -> bool:
        """
        Elevate an already-queued job to a higher priority without
        creating a new relay or callback. Returns True if bumped.

        Used by the viewport tracker to move visible-row LQ jobs ahead
        of off-screen jobs without duplicating callback machinery.
        """
        with self._not_empty:
            existing = self._inflight.get(dest_abs_path)
            if existing is None or existing.cancelled:
                return False
            if new_priority >= existing.priority:
                return False
            existing.cancelled = True
            bumped = ThumbJob(
                asset_id=existing.asset_id,
                src_abs_path=existing.src_abs_path,
                dest_abs_path=existing.dest_abs_path,
                priority=new_priority,
                size=existing.size,
                fast=existing.fast,
                on_ready=existing.on_ready,
                on_error=existing.on_error,
            )
            self._inflight[dest_abs_path] = bumped
            heapq.heappush(self._heap, (new_priority, next(self._seq), bumped))
            self._not_empty.notify()
            return True

    def cancel(self, asset_id: str) -> bool:
        """Mark all in-flight jobs for this asset_id cancelled."""
        with self._lock:
            cancelled = False
            for dest, job in list(self._inflight.items()):
                if job.asset_id == asset_id and not job.cancelled:
                    job.cancelled = True
                    del self._inflight[dest]
                    cancelled = True
            return cancelled

    def queue_depth(self) -> int:
        """Number of pending non-cancelled jobs. Best-effort; for status bar."""
        with self._lock:
            return sum(1 for _, _, j in self._heap if not j.cancelled)

    # -- internals ----------------------------------------------------------

    def _run(self) -> None:
        while True:
            with self._not_empty:
                while not self._heap and not self._shutdown:
                    self._not_empty.wait()
                if self._shutdown and not self._heap:
                    return
                _prio, _seq, job = heapq.heappop(self._heap)
                if not job.cancelled:
                    if self._inflight.get(job.dest_abs_path) is job:
                        del self._inflight[job.dest_abs_path]
            if job.cancelled:
                continue
            self._execute(job)
            # Yield between background jobs so the application stays responsive.
            if _prio >= PRIORITY_BACKGROUND:
                time.sleep(BACKGROUND_JOB_SLEEP)

    def _execute(self, job: ThumbJob) -> None:
        try:
            if not os.path.exists(job.dest_abs_path):
                generate_thumbnail(job.src_abs_path, job.dest_abs_path, job.size, fast=job.fast)
        except BaseException as e:
            if job.on_error is not None:
                try:
                    job.on_error(job.asset_id, e)
                except Exception:
                    pass
            return
        if job.on_ready is not None:
            try:
                job.on_ready(job.asset_id, job.dest_abs_path)
            except Exception:
                pass
