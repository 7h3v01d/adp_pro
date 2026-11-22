# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
"""Pure, GUI-independent helpers for recognizing downloadable-looking URLs
(used by clipboard monitoring and drag-and-drop handling) and for deriving
safe local filenames from server responses."""

from __future__ import annotations

import os
import re
from urllib.parse import urlparse, unquote, urlsplit, urlunsplit, parse_qsl

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


# -- redaction for logs ------------------------------------------------------------
REDACTED = "REDACTED"
_URL_IN_TEXT = re.compile(r"""\b(?:https?|ftp)://[^\s'"<>]+""", re.IGNORECASE)
_MAGNET_IN_TEXT = re.compile(r"""magnet:\?[^\s'"<>]+""", re.IGNORECASE)
# Magnet params that identify the content and carry no credentials.
_MAGNET_SAFE_PARAMS = {"xt", "dn", "xl"}
# HTTP request lines as logged by urllib3: "GET /path?token=... HTTP/1.1"
_REQUEST_LINE = re.compile(r"\b(GET|HEAD|POST|PUT|PATCH|DELETE|OPTIONS) (/\S*) (HTTP/\d(?:\.\d)?)")


def redact_url(url: str) -> str:
    """A URL safe to write to a log: scheme, host and path are kept (enough to
    diagnose which server/file failed); credentials are not. Signed and
    tokenised download links put bearer secrets in the query string
    (?token=, X-Amz-Signature=, sig=, key=...) and some put user:password in
    the authority, so every query *value* and any userinfo is replaced --
    parameter names are kept for diagnosis -- and the fragment is dropped."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return REDACTED
    netloc = parts.netloc
    if "@" in netloc:
        netloc = f"{REDACTED}@{netloc.rsplit('@', 1)[1]}"
    query = parts.query
    if query:
        pairs = parse_qsl(query, keep_blank_values=True)
        query = "&".join(f"{name}={REDACTED}" for name, _ in pairs) if pairs else REDACTED
    return urlunsplit((parts.scheme, netloc, parts.path, query, ""))


def _redact_magnet(magnet: str) -> str:
    try:
        pairs = parse_qsl(magnet.split("?", 1)[1], keep_blank_values=True)
    except (IndexError, ValueError):
        return "magnet:?" + REDACTED
    # Trackers (tr=) on private sites embed the user's passkey.
    return "magnet:?" + "&".join(
        f"{k}={v if k.lower() in _MAGNET_SAFE_PARAMS else REDACTED}" for k, v in pairs)


def redact_urls_in_text(text: str) -> str:
    """Redacts every http(s)/ftp URL and magnet link found in free text --
    log messages, and exception text (requests embeds the full URL in its
    errors: "404 Client Error: Not Found for url: https://...?token=...")."""
    if not text or ("://" not in text and "magnet:" not in text.lower() and " HTTP/" not in text):
        return text
    text = _REQUEST_LINE.sub(lambda m: f"{m.group(1)} {redact_url(m.group(2))} {m.group(3)}", text)
    def _url(m):
        url = m.group(0)
        stripped = url.rstrip(".,;:!?)]}")  # sentence punctuation isn't part of the URL
        return redact_url(stripped) + url[len(stripped):]
    text = _URL_IN_TEXT.sub(_url, text)
    return _MAGNET_IN_TEXT.sub(lambda m: _redact_magnet(m.group(0)), text)
