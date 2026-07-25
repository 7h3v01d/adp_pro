# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
"""Pure, GUI-independent helpers for recognizing downloadable-looking URLs
(used by clipboard monitoring and drag-and-drop handling) and for deriving
safe local filenames from server responses."""

from __future__ import annotations

import os
import re
from urllib.parse import urlparse, unquote

DOWNLOADABLE_EXTENSIONS = {
    ".zip", ".rar", ".7z", ".tar", ".gz", ".xz", ".bz2",
    ".exe", ".msi", ".dmg", ".pkg", ".deb", ".rpm", ".appimage",
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv",
    ".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".iso", ".img", ".apk",
}


def is_probably_url(text: str) -> bool:
    text = (text or "").strip()
    if not text or " " in text or "\n" in text:
        return False
    try:
        parsed = urlparse(text)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def looks_like_download_url(text: str) -> bool:
    """Heuristic: is this clipboard/dropped text a URL pointing at something
    that's plausibly a file to download (as opposed to a regular webpage)?"""
    if not is_probably_url(text):
        return False
    path = urlparse(text).path.lower()
    return any(path.endswith(ext) for ext in DOWNLOADABLE_EXTENSIONS)


def extract_urls_from_mime_text(text: str) -> list[str]:
    """Splits multi-line dropped text (e.g. from a browser drag) into
    candidate URLs, preserving order and dropping blanks/duplicates."""
    seen = set()
    urls = []
    for line in (text or "").splitlines():
        candidate = line.strip()
        if candidate and is_probably_url(candidate) and candidate not in seen:
            seen.add(candidate)
            urls.append(candidate)
    return urls


# -- filename derivation ----------------------------------------------------

_WINDOWS_ILLEGAL_CHARS = set('<>:"/\\|?*')
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_filename(name: str | None, fallback: str = "download") -> str:
    """Reduces a server- or URL-supplied name to something safe to create on
    disk (Windows rules, the strictest of our targets): strips any path
    components (defeating `filename="..\\..\\evil.exe"` traversal), removes
    illegal/control characters, and sidesteps reserved device names."""
    name = (name or "").strip().strip('"').strip()
    # A filename must never navigate: keep only the last path segment,
    # treating both separator styles as separators regardless of host OS.
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c for c in name if c not in _WINDOWS_ILLEGAL_CHARS and ord(c) >= 32)
    # Windows silently drops trailing dots/spaces; do it explicitly so the
    # name we display matches the name that lands on disk.
    name = name.rstrip(". ").strip()
    if not name or set(name) == {"."}:
        return fallback
    stem = name.split(".", 1)[0].upper()
    if stem in _WINDOWS_RESERVED_NAMES:
        return f"_{name}"
    return name


def filename_from_content_disposition(cd: str | None) -> str | None:
    """Extracts the filename from a Content-Disposition header, or None.

    Per RFC 6266, the RFC 5987 `filename*=charset'lang'percent-encoded`
    form takes precedence over the plain `filename=` form when both are
    present. The result is NOT sanitized -- callers should pass it through
    sanitize_filename() before touching the filesystem."""
    if not cd:
        return None

    # 1) filename*  (RFC 5987 ext-value: charset'language'value)
    m = re.search(r"filename\*\s*=\s*([^;]+)", cd, re.IGNORECASE)
    if m:
        ext_value = m.group(1).strip().strip('"')
        parts = ext_value.split("'", 2)
        if len(parts) == 3:
            charset, _language, value = parts
            try:
                return unquote(value, encoding=charset or "utf-8", errors="replace")
            except LookupError:  # unknown charset label -- fall back to utf-8
                return unquote(value)
        return unquote(ext_value)

    # 2) filename="quoted string" (with \-escapes per RFC 2616 quoted-pair)
    m = re.search(r'filename\s*=\s*"((?:\\.|[^"\\])*)"', cd, re.IGNORECASE)
    if m:
        # Only unescape \" and \\ -- a literal RFC quoted-pair reading would
        # also collapse `..\..\evil.exe` into a separator-free string,
        # hiding the traversal attempt from sanitize_filename. Real servers
        # that put backslashes in filenames mean paths, not escapes.
        return re.sub(r'\\(["\\])', r"\1", m.group(1))

    # 3) filename=bare-token
    m = re.search(r"filename\s*=\s*([^;]+)", cd, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return None


def filename_from_url(url: str) -> str:
    """Best-effort filename from the final path segment of a URL,
    percent-decoded. Empty string if the URL has no usable path segment."""
    return unquote(os.path.basename(urlparse(url).path))
