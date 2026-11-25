# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
"""Shorter searches to try when a search finds nothing (2.0.1).

Indexers such as torrents-csv match every word literally, so a specific search like
"Ubuntu 22.04 server ISO" or "ubuntu-22.04-server-lts" can return nothing while
"ubuntu server" finds plenty. fallback_queries() gives the shorter searches to try,
most specific first:

  1. without filler words (iso, download, torrent, the...) and with -/_ split into words
  2. ...also without version numbers (22.04, v1.2.3, 2025.11.01)
  3. ...only the first two words
  4. ...only the first word

Never more than MAX_FALLBACKS, never the original again, never an empty search.
"""
from __future__ import annotations

import re
from typing import List

MAX_FALLBACKS = 3

FILLER = {
    "iso", "img", "image", "download", "downloads", "torrent", "torrents", "magnet", "file", "files",
    "the", "a", "an", "of", "for", "latest", "newest", "official", "version", "release", "copy",
}
_VERSION = re.compile(r"^v?\d+(?:\.\d+)*[a-z]?$", re.IGNORECASE)


def _words(text: str) -> List[str]:
    return [w for w in re.split(r"[\s\-_]+", text or "") if w]


def fallback_queries(text: str) -> List[str]:
    original = " ".join(_words(text)).lower()
    words = [w for w in _words(text) if w.lower() not in FILLER]
    no_versions = [w for w in words if not _VERSION.match(w)]
    candidates = [words, no_versions, no_versions[:2], no_versions[:1]]
    out: List[str] = []
    for c in candidates:
        q = " ".join(c).strip()
        if q and q.lower() != original and q.lower() not in (x.lower() for x in out):
            out.append(q)
    return out[:MAX_FALLBACKS]
