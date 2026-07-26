# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
"""DownloadManager: coordinates the workers for a single download.

Owns the lifecycle (queue -> metadata -> ranged workers -> checksum -> done),
the per-chunk progress sidecar for crash/restart resume, the epoch mechanism
that retires stale workers on pause/stop/retry, and strict size validation at
completion. GUI-independent: it reports everything via Qt signals.
"""

import os
import re
import time
import json
import logging
import collections
from typing import Optional, Dict

from PyQt6.QtCore import QObject, pyqtSignal

from adp.core.models import Status, category_for_filename
from adp.core.speed_limiter import SpeedLimiter
from adp.core.downloader.http import BROWSER_HEADERS
from adp.core.downloader.workers import (
    DownloadWorker, ChecksumWorker, CleanupWorker,
)
from adp.core.downloader.metadata import MetadataFetcher, MetadataFetcherSignals

logger = logging.getLogger(__name__)


class DownloadManager(QObject):
    """Coordinates the workers for a single download."""

    # byte counts are 'qint64' -- a plain `int` signal parameter is C++
    # 32-bit and wraps negative past 2 GiB (same bug class as the torrent
    # engine's metadata_received signal).
    progress_updated = pyqtSignal(str, 'qint64', 'qint64', float, str)
    download_finished = pyqtSignal(str, str)
    error_occurred = pyqtSignal(str, str)

    def __init__(self, download_id: str, url: str, save_path: str, thread_pool,
                 num_threads: int = 4, checksum: Optional[str] = None,
                 headers: Optional[Dict] = None, category: Optional[str] = None,
                 speed_limit_bps: int = 0, verify_tls: bool = True):
        super().__init__()
        self.download_id = download_id
        self.url = url
        self.save_path = save_path
        self.filename = os.path.basename(save_path)
        self.num_threads = max(1, num_threads)
        self.thread_pool = thread_pool
        self.checksum = checksum
        self.headers = headers or BROWSER_HEADERS
        self.category = category or category_for_filename(self.filename)
        # TLS certificate verification. On by default; a per-download opt-out
        # exists for servers with broken/self-signed certs the user has
        # decided to trust anyway. Never disable globally: an unverified
        # download is exactly the man-in-the-middle scenario a downloader
        # that also checks SHA-256 sums is supposed to protect against.
        self.verify_tls = verify_tls

        self.total_size = 0
        self.downloaded_size = 0
        # Whether start() has run and fetched metadata (total_size, range
        # support, restored progress). A runtime pause happens after this is
        # True, so resume() can respawn workers directly. But a download
        # *restored from a saved session as paused* has never run start(): it
        # has total_size=0 and no metadata, so resume() must route it through
        # start() first rather than spawning workers against a 0-byte file.
        self.metadata_initialized = False
        self.workers = []
        self.active_workers = 0
        self.start_time = None
        self.downloaded_at_start = 0
        self.status = Status.PENDING
        self.traceback_info = ""
        self.progress_file = f"{self.save_path}.progress"
        self.last_save_time = 0
        self.server_etag = None
        self.server_last_modified = None
        self.speed_history = collections.deque(maxlen=10)
        self.chunk_progress: Dict[int, int] = {}
        # Bumped whenever the current generation of workers must die
        # (pause/stop/retry). Workers spawned earlier see the mismatch, stop
        # writing, and exit; their late signals are ignored (see
        # on_worker_finished / on_worker_error).
        self.worker_epoch = 0
        self.current_speed = 0.0
        self._metadata_signals = None
        self._metadata_fetcher = None
        self.speed_limiter = SpeedLimiter(speed_limit_bps)

    # -- status / limits -------------------------------------------------
    def set_status(self, new_status: Status):
        if self.status != new_status:
            self.status = new_status
            logger.info(f"Download {self.download_id} status changed to {self.status.name}")
            self.update_progress()

    def set_speed_limit(self, bytes_per_second: int):
        self.speed_limiter.set_limit(bytes_per_second)

    # -- persistence -------------------------------------------------------
    def load_progress(self):
        if os.path.exists(self.progress_file):
            try:
                with open(self.progress_file, 'r') as f:
                    data = json.load(f)
                if data.get('url') != self.url or data.get('save_path') != self.save_path:
                    return False
                if self.server_etag and data.get('etag') != self.server_etag:
                    return False
                # Even without an ETag, a size mismatch means the remote file
                # changed since the progress was written -- resuming would
                # splice old and new content together. self.total_size is
                # already set from the fresh metadata fetch at this point.
                if self.total_size > 0 and data.get('total_size') != self.total_size:
                    logger.warning(
                        f"[{self.download_id}] Progress file size ({data.get('total_size')}) "
                        f"doesn't match the server's current size ({self.total_size}); "
                        "discarding old progress and starting fresh.")
                    return False

                self.total_size = data.get('total_size', 0)
                self.chunk_progress = {int(k): v for k, v in data.get('chunk_progress', {}).items()}
                self.downloaded_size = sum(self.chunk_progress.values())

                logger.info(f"[{self.download_id}] Resuming download. Loaded progress: {self.downloaded_size} bytes")
                return True
            except (json.JSONDecodeError, KeyError, OSError) as e:
                logger.error(f"[{self.download_id}] Failed to load progress file: {e}", exc_info=True)
        return False

    def save_progress(self):
        if self.status in [Status.DOWNLOADING, Status.PAUSED]:
            try:
                with open(self.progress_file, 'w') as f:
                    json.dump({
                        'url': self.url, 'save_path': self.save_path,
                        'total_size': self.total_size, 'etag': self.server_etag,
                        'last_modified': self.server_last_modified,
                        # Snapshot: worker threads insert/update keys while we
                        # serialize; iterating the live dict can raise
                        # "dictionary changed size during iteration".
                        'chunk_progress': dict(self.chunk_progress)
                    }, f, indent=4)
            except OSError as e:
                logger.error(f"[{self.download_id}] Failed to save progress: {e}", exc_info=True)

    # -- lifecycle -----------------------------------------------------
    def start(self):
        logger.info(f"[{self.download_id}] Starting download: url={self.url} save_path={self.save_path} "
                    f"num_threads={self.num_threads} category={self.category} "
                    f"speed_limit={self.speed_limiter.rate or 'unlimited'}")
        self.set_status(Status.STARTING)
        # Keep these as instance attributes, not bare locals: nothing else
        # holds a Python-level reference to them once start() returns, and
        # without one, the GC can (and sometimes does, especially on a fast
        # failure) collect the wrapper object while the background thread is
        # still trying to emit a signal on it -- surfacing as
        # "RuntimeError: wrapped C/C++ object ... has been deleted".
        self._metadata_signals = MetadataFetcherSignals()
        self._metadata_fetcher = MetadataFetcher(self.url, self.headers, self._metadata_signals,
                                                 verify_tls=self.verify_tls)
        self._metadata_signals.metadata_fetched.connect(self.handle_metadata_fetched)
        self._metadata_signals.error_occurred.connect(self.handle_metadata_error)
        self.thread_pool.start(self._metadata_fetcher)

    def handle_metadata_fetched(self, total_size, accept_ranges, etag, last_modified, _):
        if self.status in (Status.STOPPED, Status.ERROR):
            # The download was stopped/removed while metadata was in flight;
            # don't resurrect it by spawning workers now.
            return

        logger.info(f"[{self.download_id}] Metadata: total_size={total_size} "
                    f"accept_ranges={accept_ranges!r} etag={etag!r}")
        self.total_size = total_size
        self.server_etag = etag
        self.server_last_modified = last_modified
        self.metadata_initialized = True

        if self.total_size <= 0:
            self.handle_metadata_error("Could not determine file size.")
            return
        if accept_ranges != 'bytes':
            self.num_threads = 1
            self.ranges_supported = False
        else:
            self.ranges_supported = True

        if not (os.path.exists(self.save_path) and self.load_progress()):
            self.downloaded_size = 0
            self.chunk_progress = {}
            try:
                with open(self.save_path, 'wb') as f:
                    f.seek(self.total_size - 1)
                    f.write(b'\0')
            except OSError:
                with open(self.save_path, 'wb'):
                    pass

        if self.downloaded_size >= self.total_size:
            self.finish_download()
            return

        self.start_time = time.time()
        self.downloaded_at_start = self.downloaded_size
        self.set_status(Status.DOWNLOADING)

        self._spawn_workers()

    def _spawn_workers(self):
        self.active_workers = 0
        # A server without Range support can't resume: the only option is a
        # fresh full GET. If we have partial progress (a paused/restarted
        # non-range download), reset the GLOBAL counters and the file here,
        # before spawning -- otherwise the single full-GET worker adds another
        # whole-file's worth on top of the old downloaded_size, blowing the
        # exact-size check. This must happen at the manager level: the worker
        # can only reset its own chunk, not the manager's downloaded_size.
        if not getattr(self, 'ranges_supported', True) and self.downloaded_size:
            logger.info(
                f"[{self.filename}] Server has no range support; restarting the "
                f"download from scratch (can't resume a non-range transfer).")
            self.chunk_progress = {}
            self.downloaded_size = 0
            self.downloaded_at_start = 0
            try:
                with open(self.save_path, 'wb') as f:
                    if self.total_size > 0:
                        f.seek(self.total_size - 1)
                        f.write(b'\0')
            except OSError as e:
                logger.error(f"Could not reset file for non-range restart: {e}")
        chunk_size = self.total_size // self.num_threads
        for i in range(self.num_threads):
            start = i * chunk_size
            end = start + chunk_size - 1 if i < self.num_threads - 1 else self.total_size - 1

            chunk_total = end - start + 1
            chunk_downloaded = self.chunk_progress.get(start, 0)

            if chunk_downloaded < chunk_total:
                self._start_worker(start, end)
            else:
                logger.debug(f"[{self.filename}] Chunk {i} is already complete.")

        # FAIL-SAFE: if nothing started but the file is incomplete, the progress
        # map is likely corrupt; nuke it and fall back to one full-file worker.
        if self.active_workers == 0 and self.downloaded_size < self.total_size:
            logger.warning(
                f"[{self.filename}] No workers started but download is incomplete! "
                f"Total: {self.total_size}, Downloaded: {self.downloaded_size}. "
                "FAIL-SAFE: falling back to a fresh, single-threaded download."
            )
            self.chunk_progress = {}
            self.downloaded_size = 0
            self.downloaded_at_start = 0
            self.speed_history.clear()
            try:
                if os.path.exists(self.progress_file):
                    os.remove(self.progress_file)
                with open(self.save_path, 'wb') as f:
                    if self.total_size > 0:
                        f.seek(self.total_size - 1)
                        f.write(b'\0')
            except OSError as e:
                logger.error(f"Fail-safe could not reset file: {e}")

            self._start_worker(0, self.total_size - 1)
        elif self.active_workers == 0:
            self.finish_download()

    def _start_worker(self, start, end):
        worker = DownloadWorker(self, self.url, self.save_path, start, end, self.headers,
                                 speed_limiter=self.speed_limiter, epoch=self.worker_epoch,
                                 verify_tls=self.verify_tls,
                                 ranges_supported=getattr(self, 'ranges_supported', True))
        worker.signals.chunk_downloaded.connect(self.on_chunk_downloaded)
        worker.signals.finished.connect(self.on_worker_finished)
        worker.signals.error.connect(self.on_worker_error)
        self.workers.append(worker)
        self.active_workers += 1
        self.thread_pool.start(worker)

    def handle_metadata_error(self, error_message):
        if self.status == Status.STOPPED:
            return
        logger.warning(f"[{self.download_id}] Metadata fetch failed for {self.url}: {error_message}")
        self.traceback_info = error_message
        # This download's engine needs the file size up front (it splits the
        # file into ranged chunks), so a failed metadata fetch currently means
        # it can't proceed. Surface the plain-language reason rather than a
        # raw exception string.
        self.error_occurred.emit(self.download_id, error_message)
        self.set_status(Status.ERROR)

    def on_chunk_downloaded(self, size: int):
        self.downloaded_size += size
        current_time = time.time()
        if current_time - self.last_save_time > 1.0:
            self.save_progress()
            self.last_save_time = current_time
        self.update_progress()

    def on_worker_finished(self, epoch: int = -1):
        if epoch != self.worker_epoch:
            # A worker from a previous generation (pre-pause/stop/retry)
            # finishing late must not skew the live generation's accounting.
            return
        self.active_workers -= 1
        if self.active_workers <= 0 and self.status == Status.DOWNLOADING:
            self.finish_download()

    def finish_download(self):
        self.save_progress()
        if self.downloaded_size != self.total_size:
            logger.warning(
                f"[{self.download_id}] All workers finished but downloaded_size "
                f"({self.downloaded_size}) != total_size ({self.total_size}) -- treating as an error. "
                f"chunk_progress={self.chunk_progress}"
            )
            self.on_worker_error((RuntimeError, RuntimeError(
                "Download finished with a byte count that doesn't match the expected size."), None))
            return

        # Belt-and-suspenders: the bytes we *think* we wrote must match what's
        # actually on disk. Catches a truncated/overwritten file that the
        # in-memory counters wouldn't reflect.
        try:
            on_disk = os.path.getsize(self.save_path)
        except OSError as e:
            self.on_worker_error((OSError, e, None))
            return
        if on_disk != self.total_size:
            logger.error(
                f"[{self.download_id}] On-disk size {on_disk} != expected {self.total_size}.")
            self.on_worker_error((RuntimeError, RuntimeError(
                f"Downloaded file is {on_disk} bytes, expected {self.total_size}."), None))
            return

        logger.info(f"[{self.download_id}] All chunks complete ({self.downloaded_size} bytes).")
        if self.checksum:
            self.set_status(Status.VERIFYING)
            checksum_worker = ChecksumWorker(self.save_path, self.checksum)
            checksum_worker.signals.finished.connect(self.on_verification_finished)
            checksum_worker.signals.error.connect(self.on_verification_error)
            self.thread_pool.start(checksum_worker)
        else:
            logger.info(f"[{self.download_id}] Download completed: {self.save_path}")
            self.set_status(Status.COMPLETED)
            self.download_finished.emit(self.download_id, self.filename)
            self.thread_pool.start(CleanupWorker(self.progress_file))

    def on_verification_finished(self, is_valid: bool):
        if is_valid:
            logger.info(f"[{self.download_id}] Checksum verified OK: {self.save_path}")
            self.set_status(Status.COMPLETED)
            self.download_finished.emit(self.download_id, self.filename)
            self.thread_pool.start(CleanupWorker(self.progress_file))
        else:
            logger.error(f"[{self.download_id}] Checksum verification FAILED for {self.save_path} "
                         f"(expected {self.checksum})")
            self.traceback_info = "Checksum verification failed."
            self.error_occurred.emit(self.download_id, self.traceback_info)
            self.set_status(Status.ERROR)

    def on_verification_error(self, error_message: str):
        logger.error(f"[{self.download_id}] Checksum verification errored: {error_message}")
        self.traceback_info = error_message
        self.error_occurred.emit(self.download_id, self.traceback_info)
        self.set_status(Status.ERROR)

    def on_worker_error(self, error_tuple):
        if len(error_tuple) == 4:
            exctype, value, tb, epoch = error_tuple
            if epoch != self.worker_epoch:
                # Stale worker's connection died after we paused/stopped --
                # not an error for the current generation.
                return
        else:
            exctype, value, tb = error_tuple
        self.traceback_info = f"{exctype.__name__}: {value}"
        logger.error(
            f"[{self.download_id}] Download failed: url={self.url} save_path={self.save_path} "
            f"downloaded={self.downloaded_size}/{self.total_size} -- {self.traceback_info}",
            exc_info=(exctype, value, tb) if tb else False,
        )
        self.error_occurred.emit(self.download_id, self.traceback_info)
        self.set_status(Status.ERROR)
        self.stop_all_workers()

    def update_progress(self):
        speed = 0
        if self.start_time and self.status == Status.DOWNLOADING:
            elapsed = time.time() - self.start_time
            if elapsed > 0:
                bytes_since_start = self.downloaded_size - self.downloaded_at_start
                if bytes_since_start > 0 and elapsed > 0.5:
                    speed = bytes_since_start / elapsed
                    self.speed_history.append(speed)
            if self.speed_history:
                speed = sum(self.speed_history) / len(self.speed_history)

        self.current_speed = speed
        self.progress_updated.emit(
            self.download_id, self.downloaded_size, self.total_size, speed, self.status.name.capitalize()
        )

    def pause(self):
        if self.status == Status.DOWNLOADING:
            self.set_status(Status.PAUSED)
            # Retire the workers entirely rather than making them sleep on an
            # open connection: real servers drop idle transfers after their
            # send-timeout, which turned any pause longer than ~a minute into
            # a hard ERROR on resume. resume() respawns fresh workers from
            # chunk_progress instead.
            self._retire_workers()
            self.save_progress()

    def resume(self):
        if self.status == Status.PAUSED:
            if not self.metadata_initialized:
                # Restored-from-session pause: never ran start(), so total_size
                # is 0 and there's no range info. Go through the normal start
                # path, which fetches metadata and calls load_progress() to
                # restore partial bytes from the .progress sidecar, then spawns
                # workers. Spawning directly here would divide a 0-byte file.
                self.start()
                return
            self.start_time = time.time()
            self.downloaded_at_start = self.downloaded_size
            self.speed_history.clear()
            self.set_status(Status.DOWNLOADING)
            self._spawn_workers()

    def stop(self):
        if self.status not in [Status.STOPPED, Status.COMPLETED, Status.ERROR]:
            self.set_status(Status.STOPPED)
            self.stop_all_workers()
            self.thread_pool.start(CleanupWorker(self.progress_file))

    def stop_all_workers(self):
        self._retire_workers()

    def _retire_workers(self):
        """Invalidates the current worker generation. Workers notice the
        epoch mismatch at their next chunk boundary and exit without writing
        further; any late finished/error signals they emit are dropped."""
        self.worker_epoch += 1
        for worker in self.workers:
            worker.stop()
        self.workers.clear()
        self.active_workers = 0

    def prepare_retry(self):
        """Reset a finished/failed download to PENDING so the queue can start
        it under the normal concurrency limit. Unlike the old retry(), this
        does NOT call start() itself -- process_queue owns starting downloads
        and the active-count accounting, so there's exactly one path and the
        concurrency limit is always honoured. Handles COMPLETED too (a
        re-download), which the old retry() silently ignored.
        """
        logger.info(f"Preparing retry for download {self.download_id} (was {self.status.name})")
        self.workers.clear()
        self.active_workers = 0
        self.traceback_info = ""
        # Bump the epoch so any stragglers from the previous run retire.
        self.worker_epoch += 1
        if self.status in (Status.STOPPED, Status.COMPLETED):
            # Start clean: STOPPED discarded partial state; COMPLETED is a
            # deliberate re-download. ERROR keeps its .progress so it can
            # resume from where it failed.
            self.downloaded_size = 0
            self.chunk_progress = {}
        self.set_status(Status.PENDING)

    def retry(self):
        # Retained for API/back-compat: reset and start immediately. The GUI
        # now routes through prepare_retry() + process_queue() instead so the
        # concurrency limit is respected.
        if self.status in [Status.ERROR, Status.STOPPED]:
            logger.info(f"Retrying download {self.download_id}")
            self.workers.clear()
            self.active_workers = 0
            self.traceback_info = ""
            if self.status == Status.STOPPED:
                self.downloaded_size = 0
                self.chunk_progress = {}
            self.start()


