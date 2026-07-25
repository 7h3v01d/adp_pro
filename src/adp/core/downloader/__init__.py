# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
"""GUI-independent download engine.

This package was split out of a single downloader.py for maintainability. The
public surface is unchanged: everything that was importable from
`adp.core.downloader` is re-exported here, so `from adp.core.downloader import
DownloadManager` (etc.) keeps working exactly as before.

Layout:
- http.py      shared HTTP headers, RangeNotHonoredError, describe_http_error
- workers.py   DownloadWorker / ChecksumWorker / CleanupWorker (+ their signals)
- metadata.py  MetadataFetcher (+ signals)
- manager.py   DownloadManager, the coordinator
"""

from adp.core.downloader.http import (
    BROWSER_HEADERS,
    CHUNK_READ_SIZE,
    RangeNotHonoredError,
    describe_http_error,
)
from adp.core.downloader.workers import (
    WorkerSignals,
    ChecksumSignals,
    CleanupWorker,
    ChecksumWorker,
    DownloadWorker,
)
from adp.core.downloader.metadata import (
    MetadataFetcher,
    MetadataFetcherSignals,
)
from adp.core.downloader.manager import DownloadManager

__all__ = [
    "BROWSER_HEADERS",
    "CHUNK_READ_SIZE",
    "RangeNotHonoredError",
    "describe_http_error",
    "WorkerSignals",
    "ChecksumSignals",
    "CleanupWorker",
    "ChecksumWorker",
    "DownloadWorker",
    "MetadataFetcher",
    "MetadataFetcherSignals",
    "DownloadManager",
]
