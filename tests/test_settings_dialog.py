"""SettingsDialog round-trip for the new storage-location fields."""
import pytest

pytestmark = pytest.mark.gui

from adp.gui.dialogs import SettingsDialog


def test_storage_dirs_round_trip(qtbot, tmp_path):
    dl = str(tmp_path / "downloads")
    tor = str(tmp_path / "torrents")
    dialog = SettingsDialog(current_settings={
        "download_dir": dl, "torrent_download_dir": tor, "theme": "dark",
    })
    qtbot.addWidget(dialog)
    out = dialog.get_settings()
    assert out["download_dir"] == dl
    assert out["torrent_download_dir"] == tor
    assert out["theme"] == "dark"


def test_blank_dirs_stay_blank(qtbot):
    dialog = SettingsDialog(current_settings={})
    qtbot.addWidget(dialog)
    out = dialog.get_settings()
    assert out["download_dir"] == ""
    assert out["torrent_download_dir"] == ""


def test_free_space_label_populated_for_real_dir(qtbot, tmp_path):
    dialog = SettingsDialog(current_settings={"download_dir": str(tmp_path)})
    qtbot.addWidget(dialog)
    # Constructor calls _update_free_space_label once.
    assert "free" in dialog.free_space_label.text().lower()


def test_existing_settings_preserved_through_dialog(qtbot):
    """The dialog must not drop unrelated keys it does own -- a regression
    guard so adding storage fields didn't disturb the rest."""
    dialog = SettingsDialog(current_settings={
        "theme": "dark", "verify_tls": False, "torrent_listen_port": 7000,
        "torrent_enable_dht": False,
    })
    qtbot.addWidget(dialog)
    out = dialog.get_settings()
    assert out["verify_tls"] is False
    assert out["torrent_listen_port"] == 7000
    assert out["torrent_enable_dht"] is False
