"""Storage locations and free-disk-space checks.

Motivated by a real incident: torrents defaulted to a folder under the
app-data dir on C:, C: filled up completely, every active torrent died with
file_write errors, and the log handler itself started failing. Two lessons
applied here:

1. Where big files land must be the user's choice, not an app-data detail
   (see `torrent_download_dir` / `download_dir` in settings).
2. A download manager should notice "this won't fit" *before* the disk is
   full, not when the write fails -- and it should always leave headroom,
   because a 100%-full system drive breaks far more than the download.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import Optional

from adp.utils.format import format_size

# Never fill a disk to the last byte: even when a download "fits", refuse to
# start it unless this much space would remain afterwards. A full system
# drive takes down logging, settings saves, and everything else on the OS.
DEFAULT_HEADROOM_BYTES = 512 * 1024 * 1024  # 512 MiB


def resolve_dir(configured: Optional[str], fallback: str) -> str:
    """The directory to actually use: the user-configured one when set (with
    ~ expanded), else the fallback. Creates it if possible; if the configured
    location can't be created (drive unplugged, permissions), falls back
    rather than failing -- with the reasoning that a download landing in the
    default folder beats a download that can't start at all."""
    candidate = (configured or "").strip()
    if candidate:
        candidate = os.path.expanduser(candidate)
        try:
            os.makedirs(candidate, exist_ok=True)
            return candidate
        except OSError:
            pass
    os.makedirs(fallback, exist_ok=True)
    return fallback


def free_space_bytes(path: str) -> Optional[int]:
    """Free bytes on the volume holding `path`. Walks up to the nearest
    existing ancestor so it works for not-yet-created target folders.
    Returns None when it can't be determined (never raises) -- callers must
    treat None as "unknown", not as "plenty"."""
    probe = os.path.abspath(path or ".")
    for _ in range(64):
        if os.path.exists(probe):
            try:
                return shutil.disk_usage(probe).free
            except OSError:
                return None
        parent = os.path.dirname(probe)
        if parent == probe:
            return None
        probe = parent
    return None


@dataclass(frozen=True)
class SpaceCheck:
    fits: bool
    free_bytes: Optional[int]        # None = couldn't determine
    needed_bytes: int
    headroom_bytes: int

    @property
    def message(self) -> str:
        free_text = format_size(self.free_bytes) if self.free_bytes is not None else "unknown"
        return (
            f"needs {format_size(self.needed_bytes)} "
            f"(+{format_size(self.headroom_bytes)} headroom) "
            f"but only {free_text} is free"
        )


def check_space(path: str, needed_bytes: int,
                 headroom_bytes: int = DEFAULT_HEADROOM_BYTES) -> SpaceCheck:
    """Would writing `needed_bytes` under `path` still leave `headroom_bytes`
    free? Unknown free space counts as fitting: refusing every download on a
    volume we can't stat would be the wrong failure mode, and the engine's
    own write errors remain the backstop."""
    free = free_space_bytes(path)
    fits = True if free is None else (needed_bytes + headroom_bytes) <= free
    return SpaceCheck(fits=fits, free_bytes=free,
                       needed_bytes=max(int(needed_bytes), 0),
                       headroom_bytes=headroom_bytes)
