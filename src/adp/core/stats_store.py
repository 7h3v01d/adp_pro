"""JSON-backed storage for lifetime (cross-restart) statistics: total bytes
ever downloaded/uploaded and total completed transfers. Kept separate from
AppSettingsStore since these are counters, not preferences.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)

DEFAULT_STATS = {
    "lifetime_downloaded_bytes": 0,
    "lifetime_uploaded_bytes": 0,
    "lifetime_completed_downloads": 0,
    "lifetime_completed_torrents": 0,
    "first_used_at": None,  # set on first load
}


class StatsStore:
    def __init__(self, state_dir: str):
        self.stats_file = os.path.join(state_dir, "stats.json")

    def load(self) -> dict:
        stats = dict(DEFAULT_STATS)
        if os.path.exists(self.stats_file):
            try:
                with open(self.stats_file, 'r') as f:
                    stats.update(json.load(f))
            except (OSError, json.JSONDecodeError) as e:
                logger.error(f"Failed to load stats, starting fresh: {e}")
        if not stats.get("first_used_at"):
            stats["first_used_at"] = datetime.now().isoformat()
        return stats

    def save(self, stats: dict) -> None:
        tmp_path = f"{self.stats_file}.tmp"
        try:
            with open(tmp_path, 'w') as f:
                json.dump(stats, f, indent=4)
            os.replace(tmp_path, self.stats_file)
        except OSError as e:
            logger.error(f"Failed to save stats: {e}")
