# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
import os
import sys
import importlib.util

import pytest
from PyQt6.QtCore import QThreadPool

# Fail fast, and legibly, if the test-only plugins aren't installed. Without
# this, a venv set up with only the runtime deps produces a wall of ~100
# "fixture 'qtbot' not found" errors plus an easily-missed "Unknown config
# option: qt_api" warning -- confusing, and it looks like the code is broken
# when the real cause is a missing `pip install -e ".[dev]"`. One clear line
# beats decoding the wall.
_REQUIRED_PLUGINS = {
    "pytestqt": "pytest-qt (provides the qtbot/qapp fixtures)",
    "pytest_timeout": "pytest-timeout (provides @pytest.mark.timeout)",
}
_missing = [desc for mod, desc in _REQUIRED_PLUGINS.items()
            if importlib.util.find_spec(mod) is None]
if _missing:
    raise pytest.UsageError(
        "Missing test dependencies:\n  - "
        + "\n  - ".join(_missing)
        + "\n\nInstall the dev extras into your venv, then re-run:\n"
        "    pip install -e \".[dev]\"\n"
        "(setup.bat does this automatically.)"
    )

# Prefer the editable-installed package (setup.bat runs `pip install -e .`),
# so the suite tests what actually ships. Only fall back to putting src/ on
# the path if `adp` isn't importable -- e.g. running against a fresh checkout
# before setup.bat has been run.
try:
    import adp  # noqa: F401
except ModuleNotFoundError:
    SRC_ROOT = os.path.join(os.path.dirname(__file__), "..", "src")
    sys.path.insert(0, os.path.abspath(SRC_ROOT))

from mock_server import MockDownloadServer  # noqa: E402

try:
    from torrent_swarm import LocalSeed
    _TORRENT_IMPORT_ERROR = None
except ImportError as e:
    LocalSeed = None
    _TORRENT_IMPORT_ERROR = e


@pytest.fixture
def mock_server():
    server = MockDownloadServer().start()
    yield server
    server.stop()


@pytest.fixture
def tls_mock_server():
    """HTTPS variant with a deliberately untrusted self-signed certificate,
    for exercising the verify_tls behaviour end to end."""
    server = MockDownloadServer(tls=True).start()
    yield server
    server.stop()


@pytest.fixture
def thread_pool():
    pool = QThreadPool()
    pool.setMaxThreadCount(8)
    yield pool
    pool.clear()
    pool.waitForDone(8000)


@pytest.fixture
def download_dir(tmp_path):
    d = tmp_path / "downloads"
    d.mkdir()
    return str(d)


@pytest.fixture
def local_seed(tmp_path):
    if LocalSeed is None:
        pytest.skip(f"libtorrent not installed ({_TORRENT_IMPORT_ERROR}); "
                    f"run `pip install libtorrent` to enable torrent tests")
    seed = LocalSeed(str(tmp_path / "seed_data"))
    yield seed
    seed.stop()


@pytest.fixture
def leech_dir(tmp_path):
    d = tmp_path / "leech_data"
    d.mkdir()
    return str(d)


@pytest.fixture
def leech_engine():
    if LocalSeed is None:
        pytest.skip(f"libtorrent not installed ({_TORRENT_IMPORT_ERROR}); "
                    f"run `pip install libtorrent` to enable torrent tests")
    from adp.torrent.engine import TorrentEngine
    from torrent_swarm import _free_port

    engine = TorrentEngine(
        listen_port=_free_port(), enable_dht=False, bind_address="127.0.0.1",
        enable_lsd=False, enable_upnp=False, enable_natpmp=False,
        # Run the auto-manager every second instead of every ~30s so the
        # pause-stickiness regression test can observe several ticks: an
        # auto-managed torrent that is merely handle.pause()d gets silently
        # resumed by the next tick, which real users would only see long
        # after clicking Pause.
        extra_settings={"auto_manage_interval": 1},
    )
    yield engine
    engine.stop()


_exit_status = {"code": 0}


def pytest_sessionfinish(session, exitstatus):
    _exit_status["code"] = int(exitstatus)


def pytest_unconfigure(config):
    """Ensure the process exits promptly once results are reported.

    Downloads intentionally exercise real blocking sockets (requests/urllib3)
    inside QThreadPool workers. A worker that's mid-retry against a closed
    connection can't be interrupted cooperatively, and a handful of tests
    deliberately leave one in flight to test stop()/teardown behavior. That
    thread has no bearing on whether the tests passed, but it can otherwise
    keep the interpreter from exiting on its own. Flushing output and forcing
    the exit here (after pytest's own summary has printed) keeps CI/runs from
    hanging on cleanup that doesn't matter.
    """
    import sys as _sys
    _sys.stdout.flush()
    _sys.stderr.flush()
    os._exit(_exit_status["code"])
