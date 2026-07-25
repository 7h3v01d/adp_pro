"""Core, GUI-independent download engine.

Design notes:
- DownloadManager coordinates one or more DownloadWorker threads (via a
  QThreadPool) that each fetch a byte-range of the target file.
- Progress, completion, and errors are reported via Qt signals so a GUI can
  subscribe directly, but nothing in this module depends on any GUI widget,
  which keeps it fully testable headlessly.
- Per-chunk progress is persisted to a `<file>.progress` sidecar so downloads
  can resume after a crash or restart.
"""
import os
import time
import requests
import hashlib
import json
import logging
from typing import Optional, Dict
import collections
from urllib.parse import urlparse
import urllib3

from PyQt6.QtCore import QObject, pyqtSignal, QRunnable, pyqtSlot
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from adp.core.models import Status, category_for_filename
from adp.core.speed_limiter import SpeedLimiter
from adp.utils.url_utils import filename_from_content_disposition, filename_from_url, sanitize_filename

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,'
              'image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9',
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection': 'keep-alive',
}

CHUNK_READ_SIZE = 8192


def describe_http_error(exc: "requests.RequestException") -> str:
    """Turn a requests exception into a short, plain-language explanation a
    non-developer can act on. The raw '403 Client Error: Forbidden for url:
    ...' string is accurate but unhelpful; these messages say what it likely
    means and what to try. Falls back to the raw string for anything we don't
    have specific advice for."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status == 401:
        return ("The server requires a login or authorization for this file (HTTP 401). "
                "If it's behind an account, you may need a direct/authorized link.")
    if status == 403:
        return ("The server refused the request (HTTP 403). Some sites block download "
                "managers or links opened without a browser session. The direct download "
                "may still work -- you can set a save location and try starting it anyway.")
    if status == 404:
        return ("The file wasn't found at that URL (HTTP 404). The link may be expired, "
                "mistyped, or the file has been removed.")
    if status == 429:
        return ("The server is rate-limiting requests (HTTP 429). Wait a little while and "
                "try again.")
    if status is not None and 500 <= status < 600:
        return (f"The server had an internal error (HTTP {status}). This is on the server's "
                "side; trying again later may work.")
    if isinstance(exc, requests.exceptions.SSLError):
        return ("The server's TLS certificate could not be verified. If you trust this "
                "server, re-add the download with certificate verification turned off.")
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return "The server didn't respond in time (connection timed out)."
    if isinstance(exc, requests.exceptions.ConnectionError):
        return ("Couldn't connect to the server. Check the URL and your internet "
                "connection.")
    return str(exc)


class WorkerSignals(QObject):
    finished = pyqtSignal(int)  # worker epoch (see DownloadManager.worker_epoch)
    error = pyqtSignal(tuple)   # (exctype, value, traceback, worker_epoch)
    chunk_downloaded = pyqtSignal(int)


class ChecksumSignals(QObject):
    finished = pyqtSignal(bool)
    error = pyqtSignal(str)


class CleanupWorker(QRunnable):
    def __init__(self, progress_file):
        super().__init__()
        self.progress_file = progress_file

    @pyqtSlot()
    def run(self):
        try:
            if os.path.exists(self.progress_file):
                os.remove(self.progress_file)
        except OSError as e:
            logger.error(f"Error during file cleanup: {e}")


class ChecksumWorker(QRunnable):
    def __init__(self, file_path, expected_checksum):
        super().__init__()
        self.file_path = file_path
        self.expected_checksum = expected_checksum
        self.signals = ChecksumSignals()

    @pyqtSlot()
    def run(self):
        try:
            with open(self.file_path, 'rb') as f:
                file_hash = hashlib.sha256()
                while chunk := f.read(CHUNK_READ_SIZE):
                    file_hash.update(chunk)
                computed_checksum = file_hash.hexdigest()
            is_valid = computed_checksum.lower() == self.expected_checksum.lower()
            self.signals.finished.emit(is_valid)
        except OSError as e:
            self.signals.error.emit(f"File error during checksum: {e}")


class DownloadWorker(QRunnable):
    """Downloads a single byte-range of the target file."""

    def __init__(self, manager, url, file_path, start_byte, end_byte, headers,
                 speed_limiter: Optional[SpeedLimiter] = None, session_factory=None,
                 epoch: int = 0, verify_tls: bool = True):
        super().__init__()
        self.manager = manager
        self.url = url
        self.file_path = file_path
        self.start_byte = start_byte
        self.end_byte = end_byte
        self.headers = headers
        self.speed_limiter = speed_limiter
        self.signals = WorkerSignals()
        self.is_stopped = False
        # The manager's worker_epoch at the moment this worker was spawned.
        # If the manager's epoch moves on (pause/stop/retry), this worker is
        # stale: it must exit without writing further chunks, and the manager
        # ignores any of its late-arriving finished/error signals.
        self.epoch = epoch
        self.verify_tls = verify_tls
        # session_factory allows tests to inject a fake `requests`-like session.
        self._session_factory = session_factory or self._build_default_session

    def _is_stale(self) -> bool:
        return self.is_stopped or self.manager.worker_epoch != self.epoch

    @staticmethod
    def _build_default_session():
        session = requests.Session()
        retries = Retry(
            total=3, read=3, connect=3,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        adapter = HTTPAdapter(max_retries=retries)
        session.mount('http://', adapter)
        session.mount('https://', adapter)
        return session

    @pyqtSlot()
    def run(self):
        session = self._session_factory()
        did = self.manager.download_id

        current_pos = self.start_byte
        if self.start_byte in self.manager.chunk_progress:
            current_pos += self.manager.chunk_progress[self.start_byte]

        logger.debug(
            "[%s] Worker starting: chunk %d-%d, resuming from %d (%d bytes already done)",
            did, self.start_byte, self.end_byte, current_pos, current_pos - self.start_byte,
        )

        try:
            req_headers = {'Range': f'bytes={current_pos}-{self.end_byte}'}
            req_headers.update(self.headers)
            with session.get(self.url, headers=req_headers, stream=True, timeout=30, verify=self.verify_tls) as r:
                logger.debug(
                    "[%s] Response for chunk %d-%d: HTTP %d, Content-Length=%s, Content-Range=%s",
                    did, self.start_byte, self.end_byte, r.status_code,
                    r.headers.get('Content-Length'), r.headers.get('Content-Range'),
                )
                r.raise_for_status()
                bytes_this_run = 0
                with open(self.file_path, "r+b") as f:
                    f.seek(current_pos)
                    for chunk in r.iter_content(chunk_size=CHUNK_READ_SIZE):
                        if self._is_stale():
                            # Pause/stop/retry moved the epoch on. Exit and
                            # release the connection rather than parking on an
                            # open socket -- real servers drop idle transfers
                            # after their send-timeout, which used to turn a
                            # long pause into a hard ERROR on resume. The
                            # manager respawns fresh workers from
                            # chunk_progress when resumed.
                            logger.debug(
                                "[%s] Worker for chunk %d-%d retiring (epoch %d) after %d bytes this run",
                                did, self.start_byte, self.end_byte, self.epoch, bytes_this_run,
                            )
                            return

                        if chunk:
                            if self.speed_limiter is not None:
                                self.speed_limiter.acquire(len(chunk))
                            f.write(chunk)
                            bytes_this_run += len(chunk)
                            self.signals.chunk_downloaded.emit(len(chunk))
                            self.manager.chunk_progress[self.start_byte] = (
                                self.manager.chunk_progress.get(self.start_byte, 0) + len(chunk)
                            )
            expected = self.end_byte - self.start_byte + 1
            actual = self.manager.chunk_progress.get(self.start_byte, 0)
            if actual < expected:
                logger.warning(
                    "[%s] Worker for chunk %d-%d finished but only wrote %d/%d expected bytes "
                    "(server may have closed the connection early or ignored the Range header)",
                    did, self.start_byte, self.end_byte, actual, expected,
                )
            else:
                logger.debug(
                    "[%s] Worker for chunk %d-%d completed (%d bytes this run)",
                    did, self.start_byte, self.end_byte, bytes_this_run,
                )
            self.signals.finished.emit(self.epoch)
        except (requests.RequestException, OSError) as e:
            if self._is_stale():
                # A connection torn down because we were paused/stopped is
                # expected, not a download failure.
                logger.debug("[%s] Stale worker for chunk %d-%d exiting on %s",
                             did, self.start_byte, self.end_byte, type(e).__name__)
                return
            status_code = getattr(getattr(e, 'response', None), 'status_code', None)
            logger.error(
                "[%s] Error in worker for chunk %d-%d (url=%s, http_status=%s): %s",
                did, self.start_byte, self.end_byte, self.url, status_code, e,
                exc_info=True,
            )
            self.signals.error.emit((type(e), e, e.__traceback__, self.epoch))

    def stop(self):
        self.is_stopped = True


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

        if self.total_size <= 0:
            self.handle_metadata_error("Could not determine file size.")
            return
        if accept_ranges != 'bytes':
            self.num_threads = 1

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
                                 verify_tls=self.verify_tls)
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
        if self.downloaded_size < self.total_size:
            logger.warning(
                f"[{self.download_id}] All workers finished but downloaded_size "
                f"({self.downloaded_size}) < total_size ({self.total_size}) -- treating as an error. "
                f"chunk_progress={self.chunk_progress}"
            )
            self.on_worker_error((RuntimeError, RuntimeError("Download finished with incomplete data."), None))
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

    def retry(self):
        if self.status in [Status.ERROR, Status.STOPPED]:
            logger.info(f"Retrying download {self.download_id}")
            self.workers.clear()
            self.active_workers = 0
            self.traceback_info = ""
            if self.status == Status.STOPPED:
                self.downloaded_size = 0
                self.chunk_progress = {}
            self.start()


class MetadataFetcherSignals(QObject):
    metadata_fetched = pyqtSignal('qint64', str, str, str, str)  # total_size can exceed 2 GiB
    error_occurred = pyqtSignal(str)


class MetadataFetcher(QRunnable):
    def __init__(self, url, headers=None, signals=None, verify_tls: bool = True):
        super().__init__()
        self.url = url
        self.headers = headers or BROWSER_HEADERS
        self.signals = signals
        self.verify_tls = verify_tls

    @pyqtSlot()
    def run(self):
        parsed = urlparse(self.url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            message = (
                f"'{self.url}' doesn't look like a valid URL. Make sure you copied the actual "
                "link (right-click the download button/link -> 'Copy Link Address'), not its "
                "visible text -- a real URL starts with http:// or https://"
            )
            logger.error("Metadata fetch rejected -- not a valid URL: %r", self.url)
            self.signals.error_occurred.emit(message)
            return

        session = requests.Session()
        session.headers.update(self.headers)
        retries = Retry(total=3, backoff_factor=0.5)
        adapter = HTTPAdapter(max_retries=retries)
        session.mount('http://', adapter)
        session.mount('https://', adapter)

        response = None
        try:
            response = session.head(self.url, allow_redirects=True, timeout=30, verify=self.verify_tls)
            response.raise_for_status()
            logger.debug("HEAD %s -> HTTP %d", self.url, response.status_code)
            if int(response.headers.get('content-length', 0) or 0) <= 0:
                # Plenty of servers answer HEAD but omit Content-Length
                # (dynamic download endpoints especially); a streaming GET
                # usually does report it. Treat this like a HEAD failure.
                logger.debug("HEAD %s gave no Content-Length, falling back to GET", self.url)
                response.close()
                raise requests.RequestException("HEAD response had no Content-Length")
        except requests.RequestException as head_err:
            logger.debug("HEAD %s failed (%s), falling back to GET", self.url, head_err)
            try:
                response = session.get(self.url, stream=True, allow_redirects=True, timeout=30, verify=self.verify_tls)
                response.raise_for_status()
                logger.debug("GET %s -> HTTP %d", self.url, response.status_code)
            except requests.exceptions.SSLError as e:
                logger.error("TLS verification failed for %s: %s", self.url, e, exc_info=True)
                self.signals.error_occurred.emit(
                    "The server's TLS certificate could not be verified (it may be "
                    "self-signed, expired, or for a different hostname). If you trust "
                    "this server anyway, re-add the download with certificate "
                    "verification turned off."
                )
                return
            except requests.RequestException as e:
                status_code = getattr(getattr(e, 'response', None), 'status_code', None)
                # An HTTP status (the server answered, just with a refusal) is
                # an expected outcome for many links, not an app fault -- log
                # it at warning without a stack trace. Genuine transport
                # failures (no response) keep the traceback for diagnosis.
                if status_code is not None:
                    logger.warning("Metadata fetch for %s got HTTP %s", self.url, status_code)
                else:
                    logger.error("Metadata fetch failed for %s: %s", self.url, e, exc_info=True)
                self.signals.error_occurred.emit(describe_http_error(e))
                return

        try:
            total_size = int(response.headers.get('content-length', 0))
            accept_ranges = response.headers.get('Accept-Ranges', 'none').lower()
            etag = response.headers.get('ETag')
            last_modified = response.headers.get('Last-Modified')
            # RFC 6266 Content-Disposition takes precedence (it's the server
            # explicitly naming the file); the final URL's path segment is
            # the fallback. Either source is untrusted input destined for
            # the filesystem, so both go through sanitize_filename.
            raw_name = (filename_from_content_disposition(response.headers.get('content-disposition'))
                        or filename_from_url(response.url))
            filename = sanitize_filename(raw_name)
            self.signals.metadata_fetched.emit(total_size, accept_ranges, etag, last_modified, filename)
        finally:
            response.close()
