"""JSON-backed persistence for the download queue/list across app restarts.

Kept free of any Qt widget dependency so it can be unit tested by simply
reading/writing DownloadRecord objects.
"""
from __future__ import annotations

import json
import logging
import os
from typing import List

from adp.core.models import DownloadRecord

logger = logging.getLogger(__name__)


class SessionStore:
    def __init__(self, session_file: str):
        self.session_file = session_file

    def save(self, records: List[DownloadRecord]) -> None:
        payload = [r.to_dict() for r in records]
        tmp_path = f"{self.session_file}.tmp"
        try:
            with open(tmp_path, 'w') as f:
                json.dump(payload, f, indent=4)
            os.replace(tmp_path, self.session_file)
        except OSError as e:
            logger.error(f"Failed to save session: {e}")

    def load(self) -> List[DownloadRecord]:
        if not os.path.exists(self.session_file):
            return []
        try:
            with open(self.session_file, 'r') as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.error(f"Failed to load session: {e}")
            return []

        records = []
        for entry in raw:
            try:
                records.append(DownloadRecord.from_dict(entry))
            except TypeError as e:
                logger.error(f"Skipping malformed session entry: {e}")
        return records
