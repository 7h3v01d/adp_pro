# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
"""GUI-independent torrent engine, wrapping a single libtorrent session.

Mirrors the shape of core/downloader.py (Qt signals for progress/finish/
error, no GUI imports) so the two engines feel consistent even though a
torrent's lifecycle is materially different -- it can resolve metadata from
a bare magnet link, it keeps running after "done" (seeding), and it tracks
upload as well as download.

libtorrent's own alert queue is the source of truth; we poll it on a timer
rather than blocking on it, since it needs to interleave with the Qt event
loop.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Dict, List, Optional

import libtorrent as lt
from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from adp.torrent.models import (
    FilePriority, LT_STATE_TO_TORRENT_STATE, TorrentFileEntry, TorrentState,
)

logger = logging.getLogger(__name__)

POLL_INTERVAL_MS = 500
RESUME_DATA_SUFFIX = ".fastresume"


def _info_hash_str(handle_or_params) -> str:
    """Best-effort extraction of a stable hex info-hash string. Careful:
    torrent_handle.info_hashes is a METHOD (must be called), while
    add_torrent_params.info_hashes is a plain ATTRIBUTE -- treating them the
    same silently "works" (no exception) but for a handle it returns the
    bound method object itself, whose repr embeds a transient memory
    address that changes on every call, corrupting every id-based lookup
    that follows."""
    info_hashes = getattr(handle_or_params, "info_hashes", None)
    if callable(info_hashes):
        info_hashes = info_hashes()
    if info_hashes is not None:
        try:
            return str(info_hashes.get_best())
        except AttributeError:
            return str(info_hashes)

    info_hash = getattr(handle_or_params, "info_hash", None)
    if callable(info_hash):
        return str(info_hash())
    return str(info_hash)


class TorrentEngine(QObject):
    """Owns one libtorrent session and every torrent handle in it."""

    progress_updated = pyqtSignal(str, dict)  # torrent_id, status dict (see _status_dict)
    # total_size is 'qint64', not int: PyQt marshals a plain `int` signal
    # parameter as C++ 32-bit int, so any torrent over 2 GiB wrapped
    # negative (observed in the field: 4261190412 bytes -> -33776884).
    metadata_received = pyqtSignal(str, str, 'qint64', list)  # torrent_id, name, total_size, files
    torrent_finished = pyqtSignal(str, str)  # torrent_id, name
    torrent_error = pyqtSignal(str, str)  # torrent_id, message
    resume_data_saved = pyqtSignal(str, bytes)  # torrent_id, resume_data_bytes
    # Fired on libtorrent's state_changed_alert into downloading_metadata /
    # downloading. Alert-driven (not polled), so even a torrent that starts
    # and finishes between two polls is reported -- in order, before its
    # torrent_finished.
    download_started = pyqtSignal(str)  # torrent_id
    storage_moved = pyqtSignal(str, str)  # torrent_id, new save path
    storage_move_failed = pyqtSignal(str, str)  # torrent_id, error message

    def __init__(self, listen_port: int = 6881, enable_dht: bool = True,
                 bind_address: str = "0.0.0.0", enable_lsd: bool = True,
                 enable_upnp: bool = True, enable_natpmp: bool = True,
                 max_active_downloads: int = 3,
                 extra_settings: Optional[dict] = None, parent=None):
        super().__init__(parent)
        settings = {
            "listen_interfaces": f"{bind_address}:{listen_port}",
            "enable_dht": enable_dht,
            "enable_lsd": enable_lsd,
            "enable_upnp": enable_upnp,
            "enable_natpmp": enable_natpmp,
            # Queueing: at most N torrents download at once; the rest wait
            # as QUEUED and start automatically as slots free up. Stalled
            # torrents (libtorrent's dont_count_slow_torrents, on by default)
            # don't hold a slot. Seeding is never queued: a torrent only
            # seeds when the user chose to (stop-on-complete off, or Keep
            # Seeding), so a hidden seed cap would silently override that.
            "active_downloads": self._limit_value(max_active_downloads),
            "active_seeds": -1,
            "alert_mask": (
                lt.alert.category_t.status_notification
                | lt.alert.category_t.error_notification
                | lt.alert.category_t.storage_notification
            ),
        }
        if extra_settings:
            settings.update(extra_settings)
        self.session = lt.session(settings)
        self.handles: Dict[str, "lt.torrent_handle"] = {}
        # Names we can show immediately (from a magnet's dn= or a .torrent's
        # own metadata) before/without needing a live handle round-trip.
        self.known_names: Dict[str, str] = {}

        self._timer = QTimer(self)
        self._timer.setInterval(POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._poll)

    def start(self):
        self._timer.start()

    def stop(self):
        self._timer.stop()

    @staticmethod
    def _limit_value(n: int) -> int:
        """Our convention is 0 = unlimited; libtorrent's is -1."""
        return int(n) if n and int(n) > 0 else -1

    def set_max_active_downloads(self, n: int):
        """How many torrents may download at once (0 = no limit). Applies
        live: raising it starts queued torrents, lowering it queues the
        excess (most recently queued first)."""
        self.session.apply_settings({"active_downloads": self._limit_value(n)})

    def move_storage(self, torrent_id: str, destination: str) -> bool:
        """Asks libtorrent to move the torrent's files into `destination`
        (a folder; the torrent keeps its own name/sub-folder inside it).
        Runs on libtorrent's disk thread -- a rename on the same drive, a
        copy-then-delete across drives -- and reports via storage_moved /
        storage_move_failed. fail_if_exist: never overwrite files already at
        the destination; that fails the move and leaves everything where it
        was. Works on paused torrents too."""
        handle = self.handles.get(torrent_id)
        if handle is None or not handle.is_valid():
            return False
        handle.move_storage(destination, lt.move_flags_t.fail_if_exist)
        return True

    def start_now(self, torrent_id: str):
        """Starts a queued torrent immediately, bypassing the download limit
        ("force start"): it leaves auto-management, so the queue neither
        holds it back nor pauses it later. Pause/Resume hands it back."""
        handle = self.handles.get(torrent_id)
        if handle is not None:
            handle.unset_flags(lt.torrent_flags.auto_managed)
            handle.resume()

    @staticmethod
    def is_queued(status) -> bool:
        """Waiting for a download slot: paused by libtorrent's auto-manager
        (still auto-managed), not by the user (whose pause clears the flag)
        and not because it's complete."""
        return bool(status.paused and not status.is_finished
                    and status.flags & lt.torrent_flags.auto_managed)

    @staticmethod
    def state_from_status(status) -> TorrentState:
        """The one place a libtorrent status becomes our display state --
        shared by the GUI rows and the REST/MCP serializer."""
        state = LT_STATE_TO_TORRENT_STATE.get(int(status.state), TorrentState.DOWNLOADING)
        if status.paused:
            if status.is_finished:
                # Stopped with everything it wants (e.g. by 'stop on
                # complete'): "Finished", not an interrupted "Paused".
                state = TorrentState.FINISHED
            elif status.flags & lt.torrent_flags.auto_managed:
                state = TorrentState.QUEUED  # waiting for a download slot
            else:
                state = TorrentState.PAUSED  # the user paused it
        return state

    def shutdown(self):
        """Stops polling and destroys the libtorrent session *now*.

        libtorrent's session destructor blocks while it says goodbye to the
        network (tracker 'stopped' announces, UPnP/NAT-PMP port unmapping
        with the router, LSD). Left to Python, the session is only freed
        whenever the cyclic GC gets round to this engine -- at an arbitrary
        later moment on the GUI thread, freezing whatever is running then.
        Calling this pins that cost to a moment of our choosing. The engine
        is unusable afterwards."""
        self._timer.stop()
        self.handles.clear()
        session, self.session = self.session, None
        del session  # last reference: the destructor runs here

    # -- adding ------------------------------------------------------------
    def add_torrent_file(self, torrent_path: str, save_path: str,
                          file_priorities: Optional[Dict[int, int]] = None) -> str:
        info = lt.torrent_info(torrent_path)
        torrent_id = _info_hash_str(info)
        if torrent_id in self.handles:
            # Re-adding an existing torrent: libtorrent hands back the same
            # handle rather than erroring, which would silently duplicate
            # our bookkeeping. Surface it as "already present" instead.
            logger.info("[%s] Torrent already in session; ignoring duplicate add from file", torrent_id)
            return torrent_id
        params = {"ti": info, "save_path": save_path}
        handle = self.session.add_torrent(params)
        self.handles[torrent_id] = handle
        self.known_names[torrent_id] = info.name()
        if file_priorities:
            self._apply_file_priorities(handle, file_priorities)
        logger.info("[%s] Added torrent from file: %s -> %s", torrent_id, torrent_path, save_path)
        return torrent_id

    def add_magnet(self, magnet_uri: str, save_path: str) -> str:
        params = lt.parse_magnet_uri(magnet_uri)
        params.save_path = save_path
        torrent_id = _info_hash_str(params)
        if torrent_id in self.handles:
            logger.info("[%s] Torrent already in session; ignoring duplicate magnet add", torrent_id)
            return torrent_id
        handle = self.session.add_torrent(params)
        self.handles[torrent_id] = handle
        if params.name:
            self.known_names[torrent_id] = params.name
        logger.info("[%s] Added torrent from magnet -> %s", torrent_id, save_path)
        return torrent_id

    def restore_torrent(self, resume_data: bytes, torrent_file_path: Optional[str] = None) -> str:
        """Re-adds a torrent using a previously saved resume-data blob
        (see save_resume_data), falling back to a copy of the original
        .torrent file for pieces/file-layout info if the blob alone doesn't
        carry it (older libtorrent resume blobs sometimes don't)."""
        params = lt.read_resume_data(resume_data)
        if not getattr(params, "ti", None) and torrent_file_path and os.path.exists(torrent_file_path):
            params.ti = lt.torrent_info(torrent_file_path)
        handle = self.session.add_torrent(params)
        torrent_id = _info_hash_str(handle)
        self.handles[torrent_id] = handle
        logger.info("[%s] Restored torrent from resume data", torrent_id)
        return torrent_id

    @staticmethod
    def preview_torrent_file(torrent_path: str) -> List[TorrentFileEntry]:
        """Parses a .torrent file without adding it to the session -- used
        by the add-torrent dialog to show a file tree for selection first."""
        info = lt.torrent_info(torrent_path)
        storage = info.files()
        entries = []
        for i in range(storage.num_files()):
            if storage.file_flags(i) & lt.file_storage.flag_pad_file:
                continue  # alignment padding, not real content -- never shown/selectable
            entries.append(TorrentFileEntry(
                index=i, path=storage.file_path(i), size=storage.file_size(i),
            ))
        return entries

    # -- per-torrent controls -----------------------------------------------
    def _apply_file_priorities(self, handle, file_priorities: Dict[int, int]):
        if not file_priorities:
            return
        num_files = handle.torrent_file().num_files() if handle.torrent_file() else 0
        priorities = [4] * num_files  # lt default "normal" is 4
        for index, priority in file_priorities.items():
            if 0 <= index < num_files:
                priorities[index] = priority
        if priorities:
            handle.prioritize_files(priorities)

    def set_file_priorities(self, torrent_id: str, file_priorities: Dict[int, FilePriority]):
        handle = self.handles.get(torrent_id)
        if handle is None:
            return
        self._apply_file_priorities(handle, {i: p.value for i, p in file_priorities.items()})

    def get_file_list(self, torrent_id: str) -> List[TorrentFileEntry]:
        handle = self.handles.get(torrent_id)
        if handle is None or not handle.status().has_metadata:
            return []
        info = handle.torrent_file()
        storage = info.files()
        priorities = handle.get_file_priorities()
        progress = handle.file_progress()
        entries = []
        for i in range(storage.num_files()):
            if storage.file_flags(i) & lt.file_storage.flag_pad_file:
                continue  # alignment padding, not real content -- never shown/selectable
            entries.append(TorrentFileEntry(
                index=i, path=storage.file_path(i), size=storage.file_size(i),
                priority=FilePriority.from_lt_priority(priorities[i] if i < len(priorities) else 4),
                progress_bytes=progress[i] if i < len(progress) else 0,
            ))
        return entries

    def pause(self, torrent_id: str):
        handle = self.handles.get(torrent_id)
        if handle is not None:
            # Torrents are auto-managed by default, and libtorrent's
            # auto-manager treats a plain pause() on an auto-managed torrent
            # as "queue state noise" and silently resumes it on its next
            # tick (auto_manage_interval, up to 30s later). A pause the user
            # asked for must first take the torrent out of auto-management
            # so it actually sticks.
            handle.unset_flags(lt.torrent_flags.auto_managed)
            handle.pause()

    def resume(self, torrent_id: str):
        handle = self.handles.get(torrent_id)
        if handle is not None:
            # Mirror of pause(): hand the torrent back to the auto-manager
            # so normal queueing behaviour applies again, then resume.
            handle.set_flags(lt.torrent_flags.auto_managed)
            handle.resume()

    def force_recheck(self, torrent_id: str) -> bool:
        """Re-verify a torrent's data on disk against its piece hashes.

        libtorrent will not actually run a recheck on a *paused* torrent:
        force_recheck() queues the check but, with the torrent paused, it
        never processes, and the observable result is progress collapsing to
        0% and staying there until the torrent is resumed -- looking, to the
        user, exactly like "Force Recheck did nothing / broke my torrent".

        So if the torrent is paused we resume it first (re-arming
        auto-management, since our pause() deliberately unset it), then kick
        the recheck. A torrent that was already active is rechecked in place.
        Returns True if a handle was found and the recheck was issued.
        """
        handle = self.handles.get(torrent_id)
        if handle is None:
            return False
        if handle.status().paused:
            # Mirror resume(): hand it back to the auto-manager and resume so
            # the check can run. The recheck naturally leaves it active,
            # which is the sensible post-recheck state (the user can pause
            # again afterwards if they want).
            handle.set_flags(lt.torrent_flags.auto_managed)
            handle.resume()
        handle.force_recheck()
        # A recheck can leave the torrent needing to refetch pieces (data was
        # missing or failed verification). Re-announce to trackers/DHT so it
        # rebuilds its peer list promptly instead of sitting in DOWNLOADING
        # with no peers and a rate that decays to zero -- which shows up as an
        # ETA that climbs forever and a torrent that "never finishes".
        try:
            handle.force_reannounce()
        except Exception:
            # Best-effort: some handles (e.g. no trackers, magnet not yet
            # resolved) can't reannounce, and that must not fail the recheck.
            logger.debug("force_reannounce after recheck skipped for %s", torrent_id)
        return True

    def connect_peer(self, torrent_id: str, ip: str, port: int):
        """Directly connects to a known peer, bypassing tracker/DHT
        discovery. Useful for manually adding a known-good peer, and for
        tests that build a fully offline, deterministic local swarm."""
        handle = self.handles.get(torrent_id)
        if handle is not None:
            handle.connect_peer((ip, port))

    def set_speed_limits(self, torrent_id: str, download_bps: int = 0, upload_bps: int = 0):
        handle = self.handles.get(torrent_id)
        if handle is None:
            return
        # libtorrent's convention: 0 means unlimited, -1 is also accepted by
        # some versions for "no limit" -- 0 is the safe, version-stable choice.
        handle.set_download_limit(max(0, download_bps))
        handle.set_upload_limit(max(0, upload_bps))

    def remove(self, torrent_id: str, delete_files: bool = False):
        handle = self.handles.get(torrent_id)
        if handle is None:
            return
        flags = lt.session.delete_files if delete_files else 0
        self.session.remove_torrent(handle, flags)
        self.handles.pop(torrent_id, None)
        self.known_names.pop(torrent_id, None)

    # -- persistence ---------------------------------------------------
    def request_save_resume_data(self, torrent_id: str):
        """Asynchronously requests resume data; the result arrives via the
        alert queue on the next poll and is emitted as resume_data_saved
        (the caller listens for that + a side-channel to fetch the bytes,
        see TorrentSessionStore.save_all which drives this end to end)."""
        handle = self.handles.get(torrent_id)
        if handle is not None and handle.is_valid():
            handle.save_resume_data()

    def request_save_all_resume_data(self):
        for torrent_id in list(self.handles.keys()):
            self.request_save_resume_data(torrent_id)

    # -- polling / alerts ----------------------------------------------------
    def _poll(self):
        alerts = self.session.pop_alerts()
        for alert in alerts:
            self._handle_alert(alert)

        for torrent_id, handle in list(self.handles.items()):
            if not handle.is_valid():
                continue
            status = handle.status()
            self.progress_updated.emit(torrent_id, self._status_dict(status))

    def _handle_alert(self, alert):
        alert_type = type(alert).__name__

        if alert_type == "metadata_received_alert":
            handle = alert.handle
            torrent_id = _info_hash_str(handle)
            info = handle.torrent_file()
            if info is not None:
                self.known_names[torrent_id] = info.name()
                storage = info.files()
                files = [
                    {"index": i, "path": storage.file_path(i), "size": storage.file_size(i)}
                    for i in range(storage.num_files())
                    # Same rule as preview_torrent_file/get_file_list: pad
                    # files are alignment filler, never shown or selectable.
                    if not (storage.file_flags(i) & lt.file_storage.flag_pad_file)
                ]
                self.metadata_received.emit(torrent_id, info.name(), info.total_size(), files)
                logger.info("[%s] Metadata resolved: %s (%d files, %d bytes)",
                            torrent_id, info.name(), len(files), info.total_size())

        elif alert_type == "torrent_finished_alert":
            handle = alert.handle
            torrent_id = _info_hash_str(handle)
            name = self.known_names.get(torrent_id, torrent_id)
            logger.info("[%s] Torrent finished downloading: %s", torrent_id, name)
            self.torrent_finished.emit(torrent_id, name)

        elif alert_type in ("torrent_error_alert", "file_error_alert"):
            handle = getattr(alert, "handle", None)
            torrent_id = _info_hash_str(handle) if handle is not None else "unknown"
            message = alert.message()
            logger.error("[%s] Torrent error: %s", torrent_id, message)
            self.torrent_error.emit(torrent_id, message)

        elif alert_type == "state_changed_alert":
            if int(alert.state) in (2, 3):  # downloading_metadata, downloading
                self.download_started.emit(_info_hash_str(alert.handle))

        elif alert_type == "storage_moved_alert":
            torrent_id = _info_hash_str(alert.handle)
            new_path = alert.storage_path()
            logger.info("[%s] Storage moved to %s", torrent_id, new_path)
            self.storage_moved.emit(torrent_id, new_path)

        elif alert_type == "storage_moved_failed_alert":
            torrent_id = _info_hash_str(alert.handle)
            message = alert.message()
            logger.error("[%s] Storage move failed: %s", torrent_id, message)
            self.storage_move_failed.emit(torrent_id, message)

        elif alert_type == "save_resume_data_alert":
            handle = alert.handle
            torrent_id = _info_hash_str(handle)
            try:
                resume_bytes = lt.write_resume_data_buf(alert.params)
            except Exception as e:  # defensive: never let a serialization quirk crash the poll loop
                logger.error("[%s] Failed to serialize resume data: %s", torrent_id, e, exc_info=True)
                return
            self.resume_data_saved.emit(torrent_id, resume_bytes)

        elif alert_type == "save_resume_data_failed_alert":
            handle = getattr(alert, "handle", None)
            torrent_id = _info_hash_str(handle) if handle is not None else "unknown"
            logger.warning("[%s] Failed to save resume data: %s", torrent_id, alert.message())

    @staticmethod
    def _status_dict(status) -> dict:
        state = TorrentEngine.state_from_status(status)
        ratio = (status.all_time_upload / status.all_time_download) if status.all_time_download > 0 else 0.0
        return {
            "name": status.name,
            "state": state,
            "progress": status.progress,  # 0.0-1.0
            "total_wanted": status.total_wanted,
            "total_wanted_done": status.total_wanted_done,
            "download_rate": status.download_rate,
            "upload_rate": status.upload_rate,
            "num_peers": status.num_peers,
            "num_seeds": status.num_seeds,
            "all_time_upload": status.all_time_upload,
            "all_time_download": status.all_time_download,
            "ratio": ratio,
            "save_path": status.save_path,
            "is_seeding": status.is_seeding,
            "is_finished": status.is_finished,
            "has_metadata": status.has_metadata,
            "queued": TorrentEngine.is_queued(status),
            "error": status.error,
        }
