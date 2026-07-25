# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
"""Storage-location and disk-space-guard tests."""
import os

import pytest

from adp.core.storage import (
    DEFAULT_HEADROOM_BYTES, ConfiguredPathUnavailableError, check_space,
    free_space_bytes, resolve_dir,
)


class TestResolveDir:
    def test_uses_configured_when_creatable(self, tmp_path):
        configured = str(tmp_path / "chosen")
        fallback = str(tmp_path / "fallback")
        assert resolve_dir(configured, fallback) == configured
        assert os.path.isdir(configured)

    def test_falls_back_when_blank(self, tmp_path):
        fallback = str(tmp_path / "fallback")
        assert resolve_dir("", fallback) == fallback
        assert resolve_dir(None, fallback) == fallback
        assert os.path.isdir(fallback)

    def test_expands_user(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        result = resolve_dir("~/adp_downloads", str(tmp_path / "fb"))
        # normpath so the comparison is separator-agnostic: on Windows,
        # expanduser expands ~ but leaves the forward slash from the input,
        # yielding mixed separators (C:\...\home/adp_downloads) that are
        # perfectly valid but wouldn't string-match os.path.join's backslash.
        assert os.path.normpath(result) == os.path.normpath(
            os.path.join(str(tmp_path), "adp_downloads"))

    def test_raises_when_configured_uncreatable(self, tmp_path):
        # An explicitly-configured path that can't be created must fail
        # closed -- silently redirecting to the default is the exact incident
        # (fill the system drive) the config exists to prevent.
        blocker = tmp_path / "not_a_dir"
        blocker.write_text("x")  # a file where a dir parent is needed
        fallback = str(tmp_path / "fallback")
        with pytest.raises(ConfiguredPathUnavailableError):
            resolve_dir(str(blocker / "sub"), fallback)
        # And it must NOT have created/used the fallback.
        assert not os.path.exists(fallback)


class TestFreeSpace:
    def test_returns_positive_for_real_dir(self, tmp_path):
        free = free_space_bytes(str(tmp_path))
        assert free is not None and free > 0

    def test_walks_up_to_existing_ancestor(self, tmp_path):
        # Target folder doesn't exist yet; should still resolve via parent.
        target = str(tmp_path / "does" / "not" / "exist" / "yet")
        assert free_space_bytes(target) is not None


class TestCheckSpace:
    def test_fits_when_plenty_free(self, tmp_path):
        result = check_space(str(tmp_path), needed_bytes=1024, headroom_bytes=0)
        assert result.fits is True

    def test_does_not_fit_when_over_free(self, tmp_path):
        free = free_space_bytes(str(tmp_path)) or 0
        result = check_space(str(tmp_path), needed_bytes=free + 10 * 1024**3)
        assert result.fits is False
        assert "free" in result.message

    def test_headroom_makes_a_tight_fit_fail(self, tmp_path):
        free = free_space_bytes(str(tmp_path)) or 0
        # Needs almost everything; headroom pushes it over.
        needed = max(free - (DEFAULT_HEADROOM_BYTES // 2), 0)
        result = check_space(str(tmp_path), needed_bytes=needed)
        assert result.fits is False

    def test_unknown_free_space_counts_as_fitting(self, monkeypatch, tmp_path):
        monkeypatch.setattr("adp.core.storage.free_space_bytes", lambda _p: None)
        result = check_space(str(tmp_path), needed_bytes=10 * 1024**4)
        assert result.fits is True
        assert result.free_bytes is None
        assert "unknown" in result.message

    def test_message_is_human_readable(self, tmp_path):
        result = check_space(str(tmp_path), needed_bytes=2 * 1024**3, headroom_bytes=512 * 1024**2)
        assert "GB" in result.message or "GiB" in result.message or "MB" in result.message
