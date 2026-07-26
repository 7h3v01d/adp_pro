# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
"""TorrentItemWidget display logic -- specifically the ETA handling that made
a post-recheck torrent look like it was "never finishing" with a climbing ETA.
"""
import pytest

from adp.gui.torrent_widgets import TorrentItemWidget
from adp.torrent.models import TorrentState

pytestmark = pytest.mark.gui


def _status(state, downloaded=0, total=1_000_000, download_rate=0, upload_rate=0, **extra):
    d = {
        "state": state,
        "progress": (downloaded / total) if total else 0.0,
        "total_wanted_done": downloaded,
        "total_wanted": total,
        "download_rate": download_rate,
        "upload_rate": upload_rate,
        "num_peers": 0,
        "num_seeds": 0,
        "ratio": 0.0,
    }
    d.update(extra)
    return d


def _widget(qtbot):
    w = TorrentItemWidget("tid", "test.torrent")
    qtbot.addWidget(w)
    return w


def test_stalled_download_shows_stalled_not_climbing_eta(qtbot):
    """A DOWNLOADING torrent whose rate has decayed to near-zero must show
    'stalled', not an ever-growing ETA. This is the reported recheck symptom:
    after a recheck the peer connection dies and the rate sits at zero."""
    w = _widget(qtbot)
    # A sustained dead connection: several zero-rate samples in a row, which
    # is what a stalled post-recheck torrent actually looks like.
    for _ in range(10):
        w.update_status(_status(TorrentState.DOWNLOADING, downloaded=500_000,
                                 total=1_000_000, download_rate=0))
    text = w.info_label.text()
    assert "ETA: stalled" in text


def test_healthy_download_shows_numeric_eta(qtbot):
    w = _widget(qtbot)
    # Sustained healthy rate -> a real ETA, not 'stalled'.
    for _ in range(5):
        w.update_status(_status(TorrentState.DOWNLOADING, downloaded=500_000,
                                 total=1_000_000, download_rate=100_000))
    text = w.info_label.text()
    assert "stalled" not in text
    assert "ETA:" in text
    # 500KB remaining at ~100KB/s -> a few seconds, definitely not blank.
    assert "ETA: --" not in text


def test_eta_is_smoothed_not_instantaneous(qtbot):
    """A single rate spike/dip shouldn't swing the ETA wildly, because the
    rate is smoothed. After a steady rate, one near-zero sample must not
    immediately flip to 'stalled'."""
    w = _widget(qtbot)
    for _ in range(5):
        w.update_status(_status(TorrentState.DOWNLOADING, downloaded=500_000,
                                 total=1_000_000, download_rate=100_000))
    # One dip: smoothing keeps the effective rate well above the stall floor.
    w.update_status(_status(TorrentState.DOWNLOADING, downloaded=500_000,
                            total=1_000_000, download_rate=0))
    assert "stalled" not in w.info_label.text()


def test_checking_shows_verifying_message(qtbot):
    """During a recheck, progress reads 0; show a 'verifying' message rather
    than a 0-byte figure that looks like the data was lost."""
    w = _widget(qtbot)
    w.update_status(_status(TorrentState.CHECKING, downloaded=0, total=1_000_000))
    assert "verifying" in w.info_label.text().lower()


def test_checking_resets_rate_smoother(qtbot):
    """A recheck between downloads must reset the smoother so the post-recheck
    download doesn't inherit a stale rate."""
    w = _widget(qtbot)
    for _ in range(5):
        w.update_status(_status(TorrentState.DOWNLOADING, downloaded=100,
                                total=1_000_000, download_rate=100_000))
    assert w._smoothed_rate > 0
    w.update_status(_status(TorrentState.CHECKING, total=1_000_000))
    assert w._smoothed_rate == 0.0
