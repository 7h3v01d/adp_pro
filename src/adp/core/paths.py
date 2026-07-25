"""Resolves an OS-appropriate, per-user directory for the app's persistent
data: session queue, settings, and logs. Centralized here so all three
always live next to each other, which matters a lot when someone's trying
to find the log file to diagnose a problem.
"""
from __future__ import annotations

import os
import sys

APP_DIR_NAME = "AcceleratedDownloaderPro"


def default_app_data_dir() -> str:
    """Returns (and creates if needed) a per-user data directory:
    - Windows: %APPDATA%\\AcceleratedDownloaderPro
    - macOS:   ~/Library/Application Support/AcceleratedDownloaderPro
    - Linux:   $XDG_DATA_HOME/AcceleratedDownloaderPro or ~/.local/share/AcceleratedDownloaderPro
    """
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")

    path = os.path.join(base, APP_DIR_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def default_log_dir(app_data_dir: str = None) -> str:
    path = os.path.join(app_data_dir or default_app_data_dir(), "logs")
    os.makedirs(path, exist_ok=True)
    return path
