"""Aggregates byte-transfer stats across both engines (HTTP downloads and
torrents) into session (this run only) and lifetime (persisted) totals.

Deliberately uses delta-polling rather than subscribing to progress signals
directly: each engine already exposes a continuously-growing byte counter
(DownloadManager.downloaded_size, torrent status.all_time_download/upload),
so on every tick we just diff against the last-seen value per id. This
handles pause/resume/seeding correctly without needing a discrete
"completed" event to attribute bytes, and keeps this class Qt-independent
and easily testable.
"""
from __future__ import annotations

from typing import Dict

from adp.core.stats_store import StatsStore


class StatsAggregator:
    def __init__(self, store: StatsStore):
        self.store = store
        self.lifetime = store.load()

        self.session_downloaded_bytes = 0
        self.session_uploaded_bytes = 0
        self.session_completed_downloads = 0
        self.session_completed_torrents = 0

        # Separate id-namespaces per engine/direction -- a download_id (uuid4)
        # and a torrent_id (info-hash hex) could theoretically collide only by
        # astronomically unlikely coincidence, but keeping them in separate
        # dicts costs nothing and removes any doubt.
        self._last_download_bytes: Dict[str, int] = {}
        self._last_torrent_download_bytes: Dict[str, int] = {}
        self._last_torrent_upload_bytes: Dict[str, int] = {}

    def record_download_progress(self, download_id: str, downloaded_size: int):
        # The first observation of any id establishes a baseline (a delta of
        # zero), rather than counting the whole current value as new bytes.
        # This is deliberate: a download/torrent restored from a previous
        # session can already have a large downloaded_size the moment we
        # start polling it again after a restart, and that history was (or
        # should have been) already counted into lifetime stats back when it
        # actually happened. Only bytes that arrive *after* we start
        # watching an id count toward the running totals.
        prev = self._last_download_bytes.get(download_id, downloaded_size)
        delta = max(0, downloaded_size - prev)
        self._last_download_bytes[download_id] = downloaded_size
        if delta:
            self.session_downloaded_bytes += delta
            self.lifetime["lifetime_downloaded_bytes"] += delta

    def record_torrent_progress(self, torrent_id: str, all_time_download: int, all_time_upload: int):
        # See the comment in record_download_progress -- same baseline-on-
        # first-observation rule applies here for both directions.
        prev_d = self._last_torrent_download_bytes.get(torrent_id, all_time_download)
        prev_u = self._last_torrent_upload_bytes.get(torrent_id, all_time_upload)
        delta_d = max(0, all_time_download - prev_d)
        delta_u = max(0, all_time_upload - prev_u)
        self._last_torrent_download_bytes[torrent_id] = all_time_download
        self._last_torrent_upload_bytes[torrent_id] = all_time_upload
        if delta_d:
            self.session_downloaded_bytes += delta_d
            self.lifetime["lifetime_downloaded_bytes"] += delta_d
        if delta_u:
            self.session_uploaded_bytes += delta_u
            self.lifetime["lifetime_uploaded_bytes"] += delta_u

    def record_download_completed(self):
        self.session_completed_downloads += 1
        self.lifetime["lifetime_completed_downloads"] += 1

    def record_torrent_completed(self):
        self.session_completed_torrents += 1
        self.lifetime["lifetime_completed_torrents"] += 1

    def forget(self, item_id: str):
        """Drops tracking state for a removed download/torrent so its id
        could theoretically be reused later without inheriting stale deltas
        (ids are unique in practice, but this keeps memory from growing
        unbounded over a very long-running session with many removals)."""
        self._last_download_bytes.pop(item_id, None)
        self._last_torrent_download_bytes.pop(item_id, None)
        self._last_torrent_upload_bytes.pop(item_id, None)

    def save(self):
        self.store.save(self.lifetime)
