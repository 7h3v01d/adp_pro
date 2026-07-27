# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
"""The QRunnable workers that do the actual I/O for a download:

- DownloadWorker fetches one byte-range of the target file, strictly
  validating that the server honoured the Range request.
- ChecksumWorker verifies a completed file's SHA-256 off the GUI thread.
- CleanupWorker deletes a progress sidecar without blocking.

All report back to DownloadManager via Qt signals; none of them touch a GUI
widget, so they're fully testable headlessly.
"""

import os
import re
import hashlib
import logging
from typing import Optional

import requests
from PyQt6.QtCore import QObject, pyqtSignal, QRunnable, pyqtSlot
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from adp.core.speed_limiter import SpeedLimiter
from adp.core.downloader.http import CHUNK_READ_SIZE, RangeNotHonoredError

logger = logging.getLogger(__name__)


class WorkerSignals(QObject):
    finished = pyqtSignal(int)  # worker epoch (see DownloadManager.worker_epoch)
    error = pyqtSignal(tuple)   # (exctype, value, traceback, worker_epoch)
    chunk_downloaded = pyqtSignal(int)


class ChecksumSignals(QObject):
    finished = pyqtSignal(bool, int)  # is_valid, worker_epoch
    error = pyqtSignal(str, int)      # message, worker_epoch


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
    def __init__(self, file_path, expected_checksum, epoch=0):
        super().__init__()
        self.file_path = file_path
        self.expected_checksum = expected_checksum
        # The manager's worker_epoch when this verification was launched. A
        # later run (stop -> retry -> verify again) bumps the epoch, so a
        # straggling result from an earlier run can be recognised and dropped
        # instead of being accepted as belonging to the current run.
        self.epoch = epoch
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
            self.signals.finished.emit(is_valid, self.epoch)
        except OSError as e:
            self.signals.error.emit(f"File error during checksum: {e}", self.epoch)


class DownloadWorker(QRunnable):
    """Downloads a single byte-range of the target file."""

    def __init__(self, manager, url, file_path, start_byte, end_byte, headers,
                 speed_limiter: Optional[SpeedLimiter] = None, session_factory=None,
                 epoch: int = 0, verify_tls: bool = True, ranges_supported: bool = True):
        super().__init__()
        self.manager = manager
        self.url = url
        self.file_path = file_path
        self.start_byte = start_byte
        self.end_byte = end_byte
        self.headers = headers
        # Whether the server supports byte ranges. When False, this worker does
        # a plain full-file GET (no Range header) and accepts a 200 -- there's
        # exactly one worker covering the whole file in that case. When True,
        # ranged requests are strictly validated as 206 + Content-Range.
        self.ranges_supported = ranges_supported
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

    def _verify_partial_response(self, response, requested_start: int):
        """Assert the server actually honoured our Range request.

        A ranged GET must answer 206 Partial Content with a Content-Range that
        matches what we asked for on ALL fields: start, end, and total. A 200
        (whole file) written at our chunk offset would corrupt the output, so
        we reject it; a Content-Range that starts/ends elsewhere or reports a
        different total means the server violated the representation contract
        and we can't trust the bytes.

        Raises RangeNotHonoredError (a RequestException subclass) so the normal
        worker error path handles it; the manager then fails the download
        rather than writing corrupt bytes.
        """
        if response.status_code != 206:
            raise RangeNotHonoredError(
                f"expected HTTP 206 for range request, got {response.status_code} "
                f"(server ignored the Range header and may be sending the whole file)")
        content_range = response.headers.get('Content-Range', '')
        # Expected form: "bytes START-END/TOTAL" (TOTAL may be '*' if unknown).
        m = re.match(r'bytes\s+(\d+)-(\d+)/(\d+|\*)', content_range.strip(), re.IGNORECASE)
        if not m:
            raise RangeNotHonoredError(
                f"206 response had a missing/unparseable Content-Range: {content_range!r}")
        resp_start = int(m.group(1))
        resp_end = int(m.group(2))
        resp_total = m.group(3)
        if resp_start != requested_start:
            raise RangeNotHonoredError(
                f"server returned the wrong slice: asked for byte {requested_start}, "
                f"got Content-Range starting at {resp_start}")
        # The end must match what we asked for -- a server sending a wider or
        # narrower slice than requested has violated the range contract even
        # if the start is right (the byte cap stops overflow, but the bytes
        # beyond our request belong to a slice we didn't ask for).
        if resp_end != self.end_byte:
            raise RangeNotHonoredError(
                f"server returned the wrong slice end: asked for {requested_start}-"
                f"{self.end_byte}, got Content-Range ending at {resp_end}")
        # The total, when the server states it (not '*'), must match the size
        # the whole download is based on. A different total means the resource
        # changed under us and our chunk offsets no longer line up.
        expected_total = getattr(self.manager, 'total_size', None)
        if expected_total:
            if resp_total == '*':
                # We committed to a total_size (split the file into ranged
                # chunks against it). A server that now claims an unknown total
                # can't confirm it's still serving the same-sized resource, so
                # the bytes can't be trusted for a resumable multi-part write.
                raise RangeNotHonoredError(
                    "server returned an unknown total size (bytes .../*) for a range "
                    "request on a sized download -- can't confirm the resource is unchanged")
            if int(resp_total) != expected_total:
                raise RangeNotHonoredError(
                    f"server reports a different total size ({resp_total}) than the "
                    f"download expects ({expected_total}) -- the resource may have changed")
        # Cross-check Content-Length when present: it must equal the slice span.
        content_length = response.headers.get('Content-Length')
        if content_length is not None:
            try:
                expected_span = resp_end - resp_start + 1
                if int(content_length) != expected_span:
                    raise RangeNotHonoredError(
                        f"Content-Length {content_length} doesn't match the "
                        f"Content-Range span {expected_span}")
            except ValueError:
                raise RangeNotHonoredError(
                    f"unparseable Content-Length: {content_length!r}")

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
            req_headers = dict(self.headers)
            if self.ranges_supported:
                req_headers['Range'] = f'bytes={current_pos}-{self.end_byte}'
            with session.get(self.url, headers=req_headers, stream=True, timeout=30, verify=self.verify_tls) as r:
                logger.debug(
                    "[%s] Response for chunk %d-%d: HTTP %d, Content-Length=%s, Content-Range=%s",
                    did, self.start_byte, self.end_byte, r.status_code,
                    r.headers.get('Content-Length'), r.headers.get('Content-Range'),
                )
                r.raise_for_status()
                if self.ranges_supported:
                    # SECURITY/CORRECTNESS: we sent a Range request, so the ONLY
                    # acceptable answer is 206 Partial Content with a matching
                    # Content-Range. A 200 means the server ignored the range and
                    # is sending the *whole file* -- writing that at current_pos
                    # would corrupt the output while our < completion check would
                    # still call it done. Reject anything that isn't a verified
                    # partial response.
                    self._verify_partial_response(r, current_pos)
                elif current_pos != 0:
                    # No range support but we're resuming mid-file: we can't ask
                    # for just the tail, so a plain GET would restart from 0 and
                    # double-write. Restart this (single) chunk cleanly instead.
                    current_pos = 0
                    self.manager.chunk_progress[self.start_byte] = 0
                # Hard cap: never consume more than the range we asked for,
                # even if the server streams extra. Overshoot = corruption.
                remaining = self.end_byte - current_pos + 1
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
                            if len(chunk) > remaining:
                                # Server sent more than the requested range.
                                # Truncate to the range and stop -- writing the
                                # overflow would push past this chunk's slot.
                                chunk = chunk[:remaining]
                            if not chunk:
                                logger.warning(
                                    "[%s] Chunk %d-%d: server sent more than the requested "
                                    "range; truncating at boundary.", did, self.start_byte, self.end_byte)
                                break
                            if self.speed_limiter is not None:
                                self.speed_limiter.acquire(len(chunk))
                                # The limiter can block (sleep) for a while. If
                                # the user paused/stopped/retried during that
                                # sleep, the epoch moved on -- re-check before
                                # writing so we don't commit one stale chunk
                                # after a pause. Small window, but real.
                                if self._is_stale():
                                    return
                            f.write(chunk)
                            bytes_this_run += len(chunk)
                            remaining -= len(chunk)
                            self.signals.chunk_downloaded.emit(len(chunk))
                            self.manager.chunk_progress[self.start_byte] = (
                                self.manager.chunk_progress.get(self.start_byte, 0) + len(chunk)
                            )
                            if remaining <= 0:
                                break
            expected = self.end_byte - self.start_byte + 1
            actual = self.manager.chunk_progress.get(self.start_byte, 0)
            if actual != expected:
                # Not exactly the expected bytes -- short (early close / ignored
                # range) or, after the cap above, at most equal. Never silently
                # accept a mismatch: surface it so the manager treats the
                # download as incomplete rather than falsely "completed".
                logger.warning(
                    "[%s] Worker for chunk %d-%d wrote %d bytes, expected exactly %d "
                    "(server closed early or misbehaved on the range).",
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
