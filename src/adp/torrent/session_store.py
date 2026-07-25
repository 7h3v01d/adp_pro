"""JSON + binary-blob persistence for the torrent queue.

Two things need to survive a restart:
1. Our own lightweight metadata (TorrentRecord) -- category, save path,
   how it was added (magnet vs .torrent file), speed/ratio limits -- so the
   GUI can rebuild its rows without needing libtorrent to have resumed yet.
2. libtorrent's own resume-data blob per torrent -- piece state, priorities,
   peer cache -- which is what actually makes a restart resume quickly
   instead of doing a full recheck.
"""
from __future__ import annotations

import json
import logging
import os
from typing import List, Optional

from adp.torrent.models import TorrentRecord

logger = logging.getLogger(__name__)

RESUME_SUFFIX = ".fastresume"


class TorrentSessionStore:
    def __init__(self, state_dir: str):
        self.state_dir = state_dir
        self.records_file = os.path.join(state_dir, "torrents_session.json")
        self.resume_dir = os.path.join(state_dir, "torrents_resume")
        self.torrent_files_dir = os.path.join(state_dir, "torrents_imported")
        os.makedirs(self.resume_dir, exist_ok=True)
        os.makedirs(self.torrent_files_dir, exist_ok=True)

    # -- records (our metadata) -------------------------------------------
    def save_records(self, records: List[TorrentRecord]) -> None:
        payload = [r.to_dict() for r in records]
        tmp_path = f"{self.records_file}.tmp"
        try:
            with open(tmp_path, 'w') as f:
                json.dump(payload, f, indent=4)
            os.replace(tmp_path, self.records_file)
        except OSError as e:
            logger.error(f"Failed to save torrent session: {e}")

    def load_records(self) -> List[TorrentRecord]:
        if not os.path.exists(self.records_file):
            return []
        try:
            with open(self.records_file, 'r') as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.error(f"Failed to load torrent session: {e}")
            return []

        records = []
        for entry in raw:
            try:
                records.append(TorrentRecord.from_dict(entry))
            except TypeError as e:
                logger.error(f"Skipping malformed torrent session entry: {e}")
        return records

    # -- resume data blobs --------------------------------------------
    def save_resume_data(self, torrent_id: str, data: bytes) -> None:
        path = self._resume_path(torrent_id)
        try:
            with open(path, 'wb') as f:
                f.write(data)
        except OSError as e:
            logger.error(f"[{torrent_id}] Failed to write resume data: {e}")

    def load_resume_data(self, torrent_id: str) -> Optional[bytes]:
        path = self._resume_path(torrent_id)
        if not os.path.exists(path):
            return None
        try:
            with open(path, 'rb') as f:
                return f.read()
        except OSError as e:
            logger.error(f"[{torrent_id}] Failed to read resume data: {e}")
            return None

    def delete_resume_data(self, torrent_id: str) -> None:
        path = self._resume_path(torrent_id)
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError as e:
            logger.error(f"[{torrent_id}] Failed to delete resume data: {e}")

    def _resume_path(self, torrent_id: str) -> str:
        return os.path.join(self.resume_dir, f"{torrent_id}{RESUME_SUFFIX}")

    # -- imported .torrent file copies -------------------------------------
    def store_torrent_file_copy(self, torrent_id: str, source_path: str) -> str:
        """Copies an imported .torrent file into our own directory so it
        survives even if the user moves/deletes the original they picked in
        the file dialog. Returns the path to the stored copy."""
        import shutil
        dest = os.path.join(self.torrent_files_dir, f"{torrent_id}.torrent")
        try:
            shutil.copyfile(source_path, dest)
            return dest
        except OSError as e:
            logger.error(f"[{torrent_id}] Failed to store a copy of the .torrent file: {e}")
            return source_path
