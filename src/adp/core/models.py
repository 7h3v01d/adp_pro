# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
"""Shared data types used across the core download engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Optional, Dict, List


class Status(Enum):
    PENDING = auto()
    QUEUED = auto()          # waiting on a schedule
    STARTING = auto()
    DOWNLOADING = auto()
    PAUSED = auto()
    STOPPED = auto()
    COMPLETED = auto()
    ERROR = auto()
    VERIFYING = auto()

    @property
    def is_terminal(self) -> bool:
        return self in (Status.COMPLETED, Status.ERROR, Status.STOPPED)

    @property
    def is_active(self) -> bool:
        return self in (Status.STARTING, Status.DOWNLOADING, Status.VERIFYING)


DEFAULT_CATEGORY = "General"

CATEGORY_RULES: Dict[str, List[str]] = {
    "Documents": [".pdf", ".doc", ".docx", ".txt", ".xls", ".xlsx", ".ppt", ".pptx", ".csv"],
    "Archives": [".zip", ".rar", ".7z", ".tar", ".gz", ".xz", ".bz2"],
    "Video": [".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv"],
    "Audio": [".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a"],
    "Images": [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".svg", ".webp"],
    "Software": [".exe", ".msi", ".dmg", ".pkg", ".deb", ".rpm", ".appimage"],
}


def category_for_filename(filename: str) -> str:
    """Best-effort auto-categorization based on file extension."""
    lower = filename.lower()
    for category, extensions in CATEGORY_RULES.items():
        if any(lower.endswith(ext) for ext in extensions):
            return category
    return DEFAULT_CATEGORY


@dataclass
class DownloadRecord:
    """Serializable snapshot of a download used for session persistence."""
    download_id: str
    url: str
    save_path: str
    checksum: Optional[str] = None
    num_threads: int = 4
    headers: Optional[Dict[str, str]] = None
    category: str = DEFAULT_CATEGORY
    speed_limit_bps: int = 0  # 0 == unlimited
    scheduled_time: Optional[str] = None  # ISO 8601, or None to start immediately
    status: str = Status.PENDING.name
    downloaded_size: int = 0
    total_size: int = 0
    # Per-download TLS verification decision. Persisted so a download the user
    # explicitly created with verify_tls=False (trusting a specific server)
    # doesn't silently flip to the global default across a restart, and vice
    # versa. Defaults True (verify) for records that predate this field.
    verify_tls: bool = True
    # Recovery-policy fields: runtime lifecycle decisions that must survive
    # process death so a restarted job knows the safe, legal operation to
    # perform when it runs again.
    #   destination_owned_by_adp -- ADP created/claimed the file, so a retry may
    #     legitimately reuse/overwrite it (a foreign file stays protected).
    #   restart_required -- the last failure was an integrity error (checksum/
    #     size mismatch), so retry must discard the bad bytes and restart.
    #   allow_overwrite -- an explicit user/API grant to replace a pre-existing
    #     file, persisted so a scheduled overwrite authorization survives a
    #     restart before the job runs.
    destination_owned_by_adp: bool = False
    restart_required: bool = False
    allow_overwrite: bool = False
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict:
        return {
            "download_id": self.download_id,
            "url": self.url,
            "save_path": self.save_path,
            "checksum": self.checksum,
            "num_threads": self.num_threads,
            "headers": self.headers,
            "category": self.category,
            "speed_limit_bps": self.speed_limit_bps,
            "scheduled_time": self.scheduled_time,
            "status": self.status,
            "downloaded_size": self.downloaded_size,
            "total_size": self.total_size,
            "verify_tls": self.verify_tls,
            "destination_owned_by_adp": self.destination_owned_by_adp,
            "restart_required": self.restart_required,
            "allow_overwrite": self.allow_overwrite,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DownloadRecord":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)
