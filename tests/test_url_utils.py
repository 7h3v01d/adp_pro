import pytest

from adp.utils.url_utils import is_probably_url, looks_like_download_url, extract_urls_from_mime_text


@pytest.mark.parametrize("text,expected", [
    ("https://example.com/file.zip", True),
    ("http://example.com", True),
    ("not a url", False),
    ("ftp://example.com/file.zip", False),
    ("", False),
    ("hello world http://example.com", False),
])
def test_is_probably_url(text, expected):
    assert is_probably_url(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("https://example.com/movie.mp4", True),
    ("https://example.com/archive.tar.gz", True),
    ("https://example.com/", False),
    ("https://example.com/page.html", False),
    ("not a url", False),
])
def test_looks_like_download_url(text, expected):
    assert looks_like_download_url(text) == expected


def test_extract_urls_from_mime_text_dedupes_and_filters():
    text = "https://a.com/one.zip\nnot a url\nhttps://a.com/one.zip\nhttps://b.com/two.iso"
    assert extract_urls_from_mime_text(text) == [
        "https://a.com/one.zip",
        "https://b.com/two.iso",
    ]


def test_extract_urls_from_mime_text_empty():
    assert extract_urls_from_mime_text("") == []
    assert extract_urls_from_mime_text(None) == []


# -- filename derivation ----------------------------------------------------

import pytest

from adp.utils.url_utils import (
    filename_from_content_disposition, filename_from_url, sanitize_filename,
)


@pytest.mark.parametrize("cd,expected", [
    ('attachment; filename="report.zip"', "report.zip"),
    ("attachment; filename=report.zip", "report.zip"),
    ("attachment; filename= spaced.zip ", "spaced.zip"),
    # RFC 5987 extended form, the reason this parser exists
    ("attachment; filename*=UTF-8''na%C3%AFve%20r%C3%A9sum%C3%A9.pdf", "naïve résumé.pdf"),
    ("attachment; filename*=iso-8859-1''caf%E9.txt", "café.txt"),
    # RFC 6266: filename* wins over filename when both are present
    ('attachment; filename="fallback.bin"; filename*=UTF-8\'\'real%20name.bin', "real name.bin"),
    # quoted-pair escapes inside a quoted string
    ('attachment; filename="we\\"ird.zip"', 'we"ird.zip'),
    # unknown charset label falls back to utf-8 percent-decoding
    ("attachment; filename*=x-nonsense''plain%20name.txt", "plain name.txt"),
    ("inline", None),
    ("", None),
    (None, None),
])
def test_filename_from_content_disposition(cd, expected):
    assert filename_from_content_disposition(cd) == expected


@pytest.mark.parametrize("raw,expected", [
    ("report.zip", "report.zip"),
    # traversal via either separator style is reduced to the basename
    ("../../../evil.exe", "evil.exe"),
    ("..\\..\\evil.exe", "evil.exe"),
    ("/etc/passwd", "passwd"),
    # Windows-illegal and control characters are stripped
    ('a<b>:c|d?e*f.zip', "abcdef.zip"),
    ("bad\nname\t.txt", "badname.txt"),
    # trailing dots/spaces vanish on Windows; make that explicit
    ("name.txt...", "name.txt"),
    # reserved device names are defused, including with extensions
    ("CON.txt", "_CON.txt"),
    ("aux", "_aux"),
    # degenerate inputs fall back
    ("", "download"),
    ("   ", "download"),
    ("...", "download"),
    (None, "download"),
])
def test_sanitize_filename(raw, expected):
    assert sanitize_filename(raw) == expected


def test_filename_from_url_decodes_percent_encoding():
    assert filename_from_url("https://x.test/files/My%20Report%20%282026%29.pdf") == "My Report (2026).pdf"
    assert filename_from_url("https://x.test/downloads/") == ""
