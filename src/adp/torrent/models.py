"""Data types for the torrent engine -- kept separate from the HTTP
downloader's models.py since torrents have a materially different lifecycle
(metadata resolution, seeding, ratio, per-file selection) even though both
eventually show up as "things in a queue" in the GUI.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Dict, List, Optional


class TorrentState(Enum):
    QUEUED = auto()
    CHECKING = auto()
    DOWNLOADING_METADATA = auto()  # magnet link, waiting on peers for the .torrent info
    DOWNLOADING = auto()
    FINISHED = auto()
    SEEDING = auto()
    PAUSED = auto()
    STOPPED = auto()
    ERROR = auto()

    @property
    def is_active(self) -> bool:
        return self in (
            TorrentState.CHECKING, TorrentState.DOWNLOADING_METADATA,
            TorrentState.DOWNLOADING, TorrentState.SEEDING,
        )

    @property
    def is_terminal_for_download(self) -> bool:
        """True once the download side is done, regardless of seeding."""
        return self in (TorrentState.FINISHED, TorrentState.SEEDING,
                        TorrentState.STOPPED, TorrentState.ERROR)


# libtorrent's own lt.torrent_status.states integer values, mapped to ours.
# Kept as plain ints (not importing libtorrent here) so this module stays
# import-light and testable without the libtorrent C-extension present.
LT_STATE_TO_TORRENT_STATE = {
    0: TorrentState.QUEUED,          # queued_for_checking
    1: TorrentState.CHECKING,        # checking_files
    2: TorrentState.DOWNLOADING_METADATA,  # downloading_metadata
    3: TorrentState.DOWNLOADING,     # downloading
    4: TorrentState.FINISHED,        # finished
    5: TorrentState.SEEDING,         # seeding
    6: TorrentState.CHECKING,        # allocating (legacy, rare)
    7: TorrentState.CHECKING,        # checking_resume_data
}


class FilePriority(Enum):
    SKIP = 0
    NORMAL = 4
    HIGH = 7

    @classmethod
    def from_lt_priority(cls, value: int) -> "FilePriority":
        if value <= 0:
            return cls.SKIP
        if value >= 6:
            return cls.HIGH
        return cls.NORMAL


@dataclass
class TorrentFileEntry:
    """One file inside a torrent, for the file-selection UI."""
    index: int
    path: str
    size: int
    priority: FilePriority = FilePriority.NORMAL
    progress_bytes: int = 0

    @property
    def selected(self) -> bool:
        return self.priority != FilePriority.SKIP


@dataclass
class TorrentRecord:
    """Serializable snapshot of a torrent used for session persistence.
    The heavy lifting of resuming actual piece state is delegated to
    libtorrent's own resume-data blob (stored alongside as a .fastresume
    file); this record is just enough for us to re-add it and rebuild the
    GUI row without needing libtorrent's resume data to already be present.
    """
    torrent_id: str  # info-hash hex string
    name: str
    save_path: str
    category: str = "Torrents"
    source_magnet: Optional[str] = None
    source_torrent_file: Optional[str] = None  # path to a copy of the .torrent we imported
    file_priorities: Dict[int, int] = field(default_factory=dict)  # index -> lt priority int
    upload_limit_bps: int = 0
    download_limit_bps: int = 0
    seed_ratio_limit: float = 0.0  # 0 == unlimited (seed forever / until user stops)
    added_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict:
        return {
            "torrent_id": self.torrent_id,
            "name": self.name,
            "save_path": self.save_path,
            "category": self.category,
            "source_magnet": self.source_magnet,
            "source_torrent_file": self.source_torrent_file,
            "file_priorities": {str(k): v for k, v in self.file_priorities.items()},
            "upload_limit_bps": self.upload_limit_bps,
            "download_limit_bps": self.download_limit_bps,
            "seed_ratio_limit": self.seed_ratio_limit,
            "added_at": self.added_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TorrentRecord":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known}
        if "file_priorities" in filtered and filtered["file_priorities"]:
            filtered["file_priorities"] = {int(k): v for k, v in filtered["file_priorities"].items()}
        return cls(**filtered)
