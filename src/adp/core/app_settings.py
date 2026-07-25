"""Small JSON-backed store for app-wide (non-download) preferences."""
from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

DEFAULT_SETTINGS = {
    "theme": "dark",   # dark-industrial is the house default
    "default_speed_limit_bps": 0,
    "verify_tls": True,
    "minimize_to_tray": True,
    "notifications_enabled": True,
    "clipboard_monitor_enabled": False,
    "torrent_listen_port": 6881,
    "torrent_enable_dht": True,
    "torrent_default_seed_ratio_limit": 0.0,
    "api_port": 8765,
    # Storage locations. Empty string = the built-in default (a folder under
    # the per-user app-data dir). Set these to keep multi-GB downloads off
    # the system drive -- the exact failure that motivated them.
    "download_dir": "",           # starting folder for the Add Download dialog
    "torrent_download_dir": "",   # where torrents are saved by default
    # Torrent search providers (Search tab / POST /search). Deny-first like
    # everything else, except torrents_csv: it's a read-only public API that
    # needs no credentials, and a Search tab that's empty-by-default in an
    # all-in-one app is worse than querying it. The capability that matters
    # -- actually adding a torrent -- always remains an explicit action.
    "search_providers": {
        "torrents_csv": {"enabled": True},
        "jackett": {
            "enabled": False,
            "base_url": "http://127.0.0.1:9117",
            "api_key": "",
            "indexer": "all",
        },
    },
}


class AppSettingsStore:
    def __init__(self, settings_file: str):
        self.settings_file = settings_file

    def load(self) -> dict:
        settings = dict(DEFAULT_SETTINGS)
        if os.path.exists(self.settings_file):
            try:
                with open(self.settings_file, 'r') as f:
                    settings.update(json.load(f))
            except (OSError, json.JSONDecodeError) as e:
                logger.error(f"Failed to load settings, using defaults: {e}")
        return settings

    def save(self, settings: dict) -> None:
        try:
            with open(self.settings_file, 'w') as f:
                json.dump(settings, f, indent=4)
        except OSError as e:
            logger.error(f"Failed to save settings: {e}")
