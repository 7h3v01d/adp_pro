import importlib
import importlib.util
import sys

import pytest


def test_app_boots_without_libtorrent(qtbot, tmp_path, monkeypatch):
    """Regression test: MainWindow used to import TorrentPanel (and
    transitively libtorrent) unconditionally at module scope, so an
    environment without libtorrent installed couldn't launch the app at
    all -- not even for plain HTTP downloads. Confirm the app still boots
    with a working Downloads tab and a friendly placeholder instead of a
    crash, whether libtorrent is genuinely missing in this environment or
    (the common dev/CI case) actually installed and only simulated as
    missing here."""
    libtorrent_genuinely_available = importlib.util.find_spec("libtorrent") is not None

    if not libtorrent_genuinely_available:
        # Nothing to simulate -- the normal, unmodified import path already
        # exercises exactly the fallback we want to verify.
        main_window_module = importlib.import_module("adp.gui.main_window")
        assert main_window_module.TORRENT_SUPPORT_AVAILABLE is False
        win = main_window_module.MainWindow(state_dir=str(tmp_path))
        qtbot.addWidget(win)
        _assert_degraded_correctly(win)
        return

    real_import = __import__

    def blocking_import(name, *args, **kwargs):
        if name == "libtorrent" or name.startswith("libtorrent."):
            raise ModuleNotFoundError("No module named 'libtorrent'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", blocking_import)
    # torrent_panel/engine modules are already imported+cached from earlier
    # tests in this session; force them to re-evaluate under the blocked
    # import so main_window's fallback path actually gets exercised.
    for mod_name in ["adp.torrent.engine", "adp.gui.torrent_panel", "adp.gui.main_window"]:
        sys.modules.pop(mod_name, None)

    try:
        reloaded_main_window = importlib.import_module("adp.gui.main_window")
        assert reloaded_main_window.TORRENT_SUPPORT_AVAILABLE is False

        win = reloaded_main_window.MainWindow(state_dir=str(tmp_path))
        qtbot.addWidget(win)
        _assert_degraded_correctly(win)
    finally:
        monkeypatch.undo()
        # Restore real modules so later tests in the session get the real,
        # libtorrent-backed classes again rather than the blocked-import ones.
        for mod_name in ["adp.torrent.engine", "adp.gui.torrent_panel", "adp.gui.main_window"]:
            sys.modules.pop(mod_name, None)
        importlib.import_module("adp.gui.main_window")


def _assert_degraded_correctly(win):
    tab_labels = [win.tabs.tabText(i) for i in range(win.tabs.count())]
    assert "Downloads" in tab_labels
    assert any("unavailable" in label.lower() for label in tab_labels)
    assert win.torrent_panel is None
    # The Downloads panel itself must be fully intact and usable.
    assert win.download_panel is not None
    assert win.download_panel.download_list is not None
