# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
"""The single business-logic layer sitting between the API surfaces (REST,
MCP) and the actual GUI panels. Both servers call the SAME methods here, so
there's exactly one implementation of "what does add_download mean" no
matter which interface a caller used.

Every public method here is safe to call from any thread: internally it
routes through GuiBridge.call(), which executes the actual work on the Qt
main thread and blocks the caller until it's done. Callers (REST handlers,
MCP tools) never need to think about threading at all.

Return values are always plain, JSON-serializable Python data (dicts,
lists, strings, numbers) -- never raw Qt objects, DownloadManager
instances, or torrent handles -- since these cross into REST/MCP responses.
"""

from __future__ import annotations

import base64
import logging
import os
import tempfile
from typing import Optional

from PyQt6.QtCore import Qt

from adp.api.bridge import GuiBridge
from adp.core.models import Status
from adp.core.storage import ConfiguredPathUnavailableError
from adp.utils.format import format_size, format_speed

logger = logging.getLogger(__name__)

# A .torrent is a small metadata file; anything large is either a mistake or
# an attempt to exhaust memory/disk across the API boundary. 10 MiB is far
# above any legitimate .torrent (even huge multi-file torrents are well under
# 1 MiB of metadata).
MAX_TORRENT_UPLOAD_BYTES = 10 * 1024 * 1024


class ApiError(Exception):
    """Raised for caller-facing errors (bad input, not found, etc.) so REST/
    MCP layers can map them to appropriate error responses rather than a
    generic 500/tool failure."""
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class AppController:
    def __init__(self, bridge: GuiBridge, download_panel, torrent_panel, stats_panel,
                 search_service=None):
        self.bridge = bridge
        self.download_panel = download_panel
        self.torrent_panel = torrent_panel
        self.stats_panel = stats_panel
        # SearchService (adp.search.service). Optional so existing embedders
        # and tests that don't care about search keep working unchanged.
        self.search_service = search_service

    @property
    def torrent_support_available(self) -> bool:
        return self.torrent_panel is not None

    # ==================================================================
    # Downloads
    # ==================================================================
    def list_downloads(self) -> list:
        return self.bridge.call(self._list_downloads_impl)

    def _list_downloads_impl(self) -> list:
        return [self._serialize_download(m) for m in self.download_panel.downloads.values()]

    def get_download(self, download_id: str) -> dict:
        return self.bridge.call(self._get_download_impl, download_id)

    def _get_download_impl(self, download_id: str) -> dict:
        return self._serialize_download(self._require_download(download_id))

    def add_download(self, url: str, save_path: Optional[str] = None, category: Optional[str] = None,
                      num_threads: int = 4, checksum: Optional[str] = None,
                      speed_limit_bps: int = 0, verify_tls: Optional[bool] = None) -> dict:
        return self.bridge.call(
            self._add_download_impl, url, save_path, category, num_threads, checksum,
            speed_limit_bps, verify_tls
        )

    def _add_download_impl(self, url, save_path, category, num_threads, checksum,
                            speed_limit_bps, verify_tls=None) -> dict:
        if not url:
            raise ApiError("url is required")
        if not save_path:
            filename = os.path.basename(url.split("?")[0]) or "download"
            # Use the SAME destination resolution as the GUI so REST/MCP land
            # files in the user's configured download folder, not the app-data
            # state dir. Fail closed if that folder is unavailable rather than
            # silently redirecting elsewhere.
            try:
                base_dir = self.download_panel.resolve_download_dir()
            except ConfiguredPathUnavailableError as e:
                raise ApiError(str(e))
            save_path = os.path.join(base_dir, filename)
        manager, widget = self.download_panel.add_download(
            url=url, save_path=save_path, category=category, num_threads=num_threads,
            checksum=checksum, speed_limit_bps=speed_limit_bps, verify_tls=verify_tls,
        )
        if manager is None:
            raise ApiError(
                "Could not add download -- the save path may already have an active/paused download, "
                "or the URL/path was invalid."
            )
        return self._serialize_download(manager)

    def pause_download(self, download_id: str) -> dict:
        return self.bridge.call(self._pause_download_impl, download_id)

    def _pause_download_impl(self, download_id: str) -> dict:
        manager = self._require_download(download_id)
        manager.pause()
        return self._serialize_download(manager)

    def resume_download(self, download_id: str) -> dict:
        return self.bridge.call(self._resume_download_impl, download_id)

    def _resume_download_impl(self, download_id: str) -> dict:
        manager = self._require_download(download_id)
        manager.resume()
        return self._serialize_download(manager)

    def stop_download(self, download_id: str) -> dict:
        return self.bridge.call(self._stop_download_impl, download_id)

    def _stop_download_impl(self, download_id: str) -> dict:
        manager = self._require_download(download_id)
        if manager.status in (Status.DOWNLOADING, Status.PAUSED, Status.STARTING):
            manager.stop()
            self.download_panel.finish_download_slot(download_id)
        return self._serialize_download(manager)

    def retry_download(self, download_id: str) -> dict:
        return self.bridge.call(self._retry_download_impl, download_id)

    def _retry_download_impl(self, download_id: str) -> dict:
        manager = self._require_download(download_id)
        if manager.status in (Status.ERROR, Status.STOPPED, Status.COMPLETED):
            # Same single-path retry the GUI uses: reset to PENDING and let
            # process_queue start it under the concurrency limit, rather than
            # starting it directly (which ran outside the limit and dropped
            # the un-decremented manager from the queue).
            manager.prepare_retry()
            if manager not in self.download_panel.download_queue:
                self.download_panel.download_queue.append(manager)
            self.download_panel.process_queue()
        return self._serialize_download(manager)

    def remove_download(self, download_id: str) -> dict:
        return self.bridge.call(self._remove_download_impl, download_id)

    def _remove_download_impl(self, download_id: str) -> dict:
        self._require_download(download_id)
        # Reuse the exact same selection-based removal the GUI context menu
        # uses, so there's one code path for "what removal actually does".
        self._select_download_row(download_id)
        self.download_panel.remove_selected_download()
        return {"download_id": download_id, "removed": True}

    def _select_download_row(self, download_id: str):
        for i in range(self.download_panel.download_list.count()):
            item = self.download_panel.download_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == download_id:
                self.download_panel.download_list.setCurrentRow(i)
                return
        raise ApiError(f"No download with id '{download_id}'", status_code=404)

    def _require_download(self, download_id: str):
        manager = self.download_panel.downloads.get(download_id)
        if manager is None:
            raise ApiError(f"No download with id '{download_id}'", status_code=404)
        return manager

    @staticmethod
    def _serialize_download(manager) -> dict:
        return {
            "download_id": manager.download_id,
            "url": manager.url,
            "filename": manager.filename,
            "save_path": manager.save_path,
            "category": manager.category,
            "status": manager.status.name,
            "downloaded_bytes": manager.downloaded_size,
            "total_bytes": manager.total_size,
            "downloaded_human": format_size(manager.downloaded_size),
            "total_human": format_size(manager.total_size),
            "current_speed_bps": manager.current_speed,
            "current_speed_human": format_speed(manager.current_speed),
            "num_threads": manager.num_threads,
            "speed_limit_bps": manager.speed_limiter.rate,
            "verify_tls": manager.verify_tls,
            "error": manager.traceback_info or None,
        }

    # ==================================================================
    # Torrents
    # ==================================================================
    def _require_torrent_support(self):
        if self.torrent_panel is None:
            raise ApiError("Torrent support isn't available in this install.", status_code=503)

    def list_torrents(self) -> list:
        self._require_torrent_support()
        return self.bridge.call(self._list_torrents_impl)

    def _list_torrents_impl(self) -> list:
        return [self._serialize_torrent(torrent_id) for torrent_id in self.torrent_panel.engine.handles.keys()]

    def get_torrent(self, torrent_id: str) -> dict:
        self._require_torrent_support()
        return self.bridge.call(self._get_torrent_impl, torrent_id)

    def _get_torrent_impl(self, torrent_id: str) -> dict:
        self._require_torrent_handle(torrent_id)
        return self._serialize_torrent(torrent_id)

    def add_torrent(self, magnet_uri: Optional[str] = None, torrent_file_base64: Optional[str] = None,
                     torrent_file_name: str = "upload.torrent", save_path: Optional[str] = None,
                     category: str = "Torrents", seed_ratio_limit: float = 0.0) -> dict:
        self._require_torrent_support()
        if not magnet_uri and not torrent_file_base64:
            raise ApiError("Provide either magnet_uri or torrent_file_base64.")
        if magnet_uri and torrent_file_base64:
            raise ApiError("Provide exactly one of magnet_uri or torrent_file_base64, not both.")
        return self.bridge.call(
            self._add_torrent_impl, magnet_uri, torrent_file_base64, torrent_file_name,
            save_path, category, seed_ratio_limit,
        )

    def _add_torrent_impl(self, magnet_uri, torrent_file_base64, torrent_file_name,
                           save_path, category, seed_ratio_limit) -> dict:
        save_path = save_path or self.torrent_panel.default_save_path

        if torrent_file_base64:
            try:
                raw = base64.b64decode(torrent_file_base64, validate=True)
            except Exception as e:
                raise ApiError(f"torrent_file_base64 isn't valid base64: {e}")
            if not raw:
                raise ApiError("torrent_file_base64 decoded to empty data.")
            if len(raw) > MAX_TORRENT_UPLOAD_BYTES:
                raise ApiError(
                    f"torrent file too large ({len(raw)} bytes; "
                    f"max {MAX_TORRENT_UPLOAD_BYTES}).")
            # SECURITY: never build the temp path from the caller-supplied
            # name. os.path.join(tmp, name) lets `../../x` escape the temp dir
            # and, on Windows, an absolute `C:\...` name override it entirely
            # -- turning "add a torrent" into an arbitrary file-write primitive
            # across the MCP/REST trust boundary. The on-disk name is always a
            # fixed constant; torrent_file_name is metadata only, and libtorrent
            # reads the real name from the torrent's own info dict regardless.
            # TemporaryDirectory guarantees cleanup even on error.
            with tempfile.TemporaryDirectory(prefix="adp-api-torrent-") as tmp_dir:
                tmp_path = os.path.join(tmp_dir, "upload.torrent")
                with open(tmp_path, "wb") as f:
                    f.write(raw)
                torrent_id = self.torrent_panel.add_torrent(
                    mode="file", torrent_file_path=tmp_path, save_path=save_path,
                    category=category, seed_ratio_limit=seed_ratio_limit,
                )
        else:
            torrent_id = self.torrent_panel.add_torrent(
                mode="magnet", magnet_uri=magnet_uri, save_path=save_path,
                category=category, seed_ratio_limit=seed_ratio_limit,
            )

        if torrent_id is None:
            raise ApiError("Could not add torrent -- check the magnet link/.torrent data and save path.")

        self._settle_after_add(torrent_id)
        return self._serialize_torrent(torrent_id)

    def _settle_after_add(self, torrent_id: str, timeout_seconds: float = 0.5):
        """libtorrent reports a freshly-added torrent as paused=True for a
        brief moment (until its internal auto-manage queue processes it),
        even though nothing actually paused it. Left alone, an API caller
        checking status immediately after add_torrent would see a
        misleading 'PAUSED' state. This runs on the Qt main thread (inside
        the bridge-dispatched call), so a short blocking wait here is safe
        and mirrors the same pattern used for resume-data collection in
        TorrentPanel.save_session."""
        import time as _time
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance()
        handle = self.torrent_panel.engine.handles.get(torrent_id)
        if handle is None:
            return
        deadline = _time.time() + timeout_seconds
        while _time.time() < deadline:
            if not handle.status().paused:
                return
            if app is not None:
                app.processEvents()
            _time.sleep(0.01)

    def pause_torrent(self, torrent_id: str) -> dict:
        self._require_torrent_support()
        return self.bridge.call(self._pause_torrent_impl, torrent_id)

    def _pause_torrent_impl(self, torrent_id: str) -> dict:
        self._require_torrent_handle(torrent_id)
        self.torrent_panel.engine.pause(torrent_id)
        return self._serialize_torrent(torrent_id)

    def resume_torrent(self, torrent_id: str) -> dict:
        self._require_torrent_support()
        return self.bridge.call(self._resume_torrent_impl, torrent_id)

    def _resume_torrent_impl(self, torrent_id: str) -> dict:
        self._require_torrent_handle(torrent_id)
        self.torrent_panel.engine.resume(torrent_id)
        return self._serialize_torrent(torrent_id)

    def remove_torrent(self, torrent_id: str, delete_files: bool = False) -> dict:
        self._require_torrent_support()
        return self.bridge.call(self._remove_torrent_impl, torrent_id, delete_files)

    def _remove_torrent_impl(self, torrent_id: str, delete_files: bool) -> dict:
        self._require_torrent_handle(torrent_id)
        self.torrent_panel.engine.remove(torrent_id, delete_files=delete_files)
        self.torrent_panel.records.pop(torrent_id, None)
        self.torrent_panel.session_store.delete_resume_data(torrent_id)
        for i in range(self.torrent_panel.torrent_list.count()):
            item = self.torrent_panel.torrent_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == torrent_id:
                self.torrent_panel.torrent_list.takeItem(i)
                break
        return {"torrent_id": torrent_id, "removed": True, "deleted_files": delete_files}

    def force_recheck_torrent(self, torrent_id: str) -> dict:
        self._require_torrent_support()
        return self.bridge.call(self._force_recheck_torrent_impl, torrent_id)

    def _force_recheck_torrent_impl(self, torrent_id: str) -> dict:
        self._require_torrent_handle(torrent_id)
        self.torrent_panel.engine.force_recheck(torrent_id)
        return self._serialize_torrent(torrent_id)

    def select_torrent_files(self, torrent_id: str, selected_indices: list) -> dict:
        """selected_indices: the file indices (from get_torrent's `files`
        list) that should be downloaded; every other file in the torrent is
        set to skip."""
        self._require_torrent_support()
        return self.bridge.call(self._select_torrent_files_impl, torrent_id, selected_indices)

    def _select_torrent_files_impl(self, torrent_id, selected_indices) -> dict:
        from adp.torrent.models import FilePriority
        self._require_torrent_handle(torrent_id)
        entries = self.torrent_panel.engine.get_file_list(torrent_id)
        if not entries:
            raise ApiError("File list isn't available yet (metadata may still be resolving).", status_code=409)
        selected = set(selected_indices)
        priorities = {e.index: (FilePriority.NORMAL if e.index in selected else FilePriority.SKIP) for e in entries}
        self.torrent_panel.engine.set_file_priorities(torrent_id, priorities)
        record = self.torrent_panel.records.get(torrent_id)
        if record:
            record.file_priorities = {i: p.value for i, p in priorities.items()}
        return self._serialize_torrent(torrent_id)

    def _require_torrent_handle(self, torrent_id: str):
        if torrent_id not in self.torrent_panel.engine.handles:
            raise ApiError(f"No torrent with id '{torrent_id}'", status_code=404)

    def _serialize_torrent(self, torrent_id: str) -> dict:
        from adp.torrent.models import LT_STATE_TO_TORRENT_STATE, TorrentState

        handle = self.torrent_panel.engine.handles[torrent_id]
        status = handle.status()
        record = self.torrent_panel.records.get(torrent_id)

        state = LT_STATE_TO_TORRENT_STATE.get(int(status.state), TorrentState.DOWNLOADING)
        if status.paused and state != TorrentState.FINISHED:
            state = TorrentState.PAUSED

        files = []
        if status.has_metadata:
            files = [
                {
                    "index": e.index, "path": e.path, "size": e.size,
                    "selected": e.selected, "progress_bytes": e.progress_bytes,
                }
                for e in self.torrent_panel.engine.get_file_list(torrent_id)
            ]

        ratio = (status.all_time_upload / status.all_time_download) if status.all_time_download > 0 else 0.0

        return {
            "torrent_id": torrent_id,
            "name": status.name or (record.name if record else torrent_id),
            "category": record.category if record else "Torrents",
            "save_path": status.save_path,
            "state": state.name,
            "progress": status.progress,
            "downloaded_bytes": status.total_wanted_done,
            "total_bytes": status.total_wanted,
            "downloaded_human": format_size(status.total_wanted_done),
            "total_human": format_size(status.total_wanted),
            "download_rate_bps": status.download_rate,
            "upload_rate_bps": status.upload_rate,
            "download_rate_human": format_speed(status.download_rate),
            "upload_rate_human": format_speed(status.upload_rate),
            "num_peers": status.num_peers,
            "num_seeds": status.num_seeds,
            "ratio": ratio,
            "seed_ratio_limit": record.seed_ratio_limit if record else 0.0,
            "is_seeding": status.is_seeding,
            "is_finished": status.is_finished,
            "paused": status.paused,
            "has_metadata": status.has_metadata,
            "files": files,
        }

    # ==================================================================
    # Stats
    # ==================================================================
    def get_stats(self) -> dict:
        return self.bridge.call(self._get_stats_impl)

    def _get_stats_impl(self) -> dict:
        if self.stats_panel is None:
            return {}
        agg = self.stats_panel.aggregator
        result = {
            "session_downloaded_bytes": agg.session_downloaded_bytes,
            "session_uploaded_bytes": agg.session_uploaded_bytes,
            "session_completed_downloads": agg.session_completed_downloads,
            "session_completed_torrents": agg.session_completed_torrents,
            "lifetime_downloaded_bytes": agg.lifetime["lifetime_downloaded_bytes"],
            "lifetime_uploaded_bytes": agg.lifetime["lifetime_uploaded_bytes"],
            "lifetime_completed_downloads": agg.lifetime["lifetime_completed_downloads"],
            "lifetime_completed_torrents": agg.lifetime["lifetime_completed_torrents"],
        }
        if self.torrent_panel is not None:
            handles = list(self.torrent_panel.engine.handles.values())
            result["active_torrents"] = sum(1 for h in handles if h.is_valid() and not h.status().paused)
            result["total_peers"] = sum(h.status().num_peers for h in handles if h.is_valid())
            result["total_seeds"] = sum(h.status().num_seeds for h in handles if h.is_valid())
        return result

    # ==================================================================
    # Search (torrent indexers)
    # ==================================================================
    # NOTE: search never touches Qt objects, so unlike everything above it
    # does NOT route through GuiBridge -- it runs directly on the calling
    # thread (REST threadpool / MCP task). That also means it's safe to call
    # from the Qt main thread itself, though the Search tab still uses a
    # worker thread so the UI never blocks on slow indexers.
    def _require_search_support(self):
        if self.search_service is None:
            raise ApiError("Search is not available in this instance.", status_code=503)

    def search_torrents(self, text: str, category: Optional[str] = None,
                         providers: Optional[list] = None, limit: int = 50) -> dict:
        self._require_search_support()
        from adp.search.models import SearchQuery
        try:
            query = SearchQuery(text=text, category=category, providers=providers, limit=limit)
        except ValueError as e:
            raise ApiError(str(e), status_code=422)
        outcome = self.search_service.search(query)
        if not outcome.providers_queried:
            raise ApiError(
                "No search providers are enabled (or none matched the requested "
                "provider filter). Enable one under search_providers in settings.",
                status_code=503,
            )
        return {"query": text, **outcome.to_dict()}

    def list_search_providers(self) -> list:
        self._require_search_support()
        return self.search_service.provider_infos()
