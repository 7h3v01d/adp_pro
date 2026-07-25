# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
"""Metadata probing: given a URL, discover the file's size, whether the server
supports byte ranges, its ETag/Last-Modified, and a filename -- via a HEAD
with a GET fallback. Runs off the GUI thread and reports via Qt signals.
"""

import re
import logging
from urllib.parse import urlparse

import requests
from PyQt6.QtCore import QObject, pyqtSignal, QRunnable, pyqtSlot
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from adp.utils.url_utils import (
    filename_from_content_disposition, filename_from_url, sanitize_filename,
)
from adp.core.downloader.http import BROWSER_HEADERS, describe_http_error

logger = logging.getLogger(__name__)


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
