# ADP Pro

A multi-threaded, resumable download manager with a PyQt6 "Pro" GUI:
categories, search/filter, per-download speed limits, scheduling,
drag-and-drop, clipboard monitoring, system tray notifications, and
Dark Industrial / High-Contrast Light themes -- backed by a fully
headless-testable core engine.

## Features

**Core download engine**
- Concurrent, chunked downloads (configurable connection count per file)
- Resume after a crash or restart (progress is persisted to a `.progress`
  sidecar file and verified against the server's ETag before trusting it)
- SHA-256 checksum verification
- Automatic single-thread fallback when a server doesn't support byte ranges
- Fail-safe recovery if resume state is ever corrupt/inconsistent
- Per-download and default bandwidth throttling (token-bucket limiter)

**API access** (REST + MCP, always on)
- A local REST API and an MCP server for AI tools/scripts to control the
  app: add/list/pause/resume/stop/retry/remove downloads and torrents,
  select which files in a torrent to grab, read stats
- Binds to 127.0.0.1 only, protected by a locally-generated API key (see
  "API Access" in the toolbar for the key and connection info)
- Both interfaces share one FastAPI/uvicorn process and one implementation
  of the actual logic (`AppController`) -- there's exactly one answer to
  "what does add_download mean" regardless of which interface calls it

**Torrent engine** (second tab, "Torrents")
- Built on `libtorrent`, fully self-contained -- no external torrent app needed
- Add via magnet link or `.torrent` file
- Per-file selection (choose which files in a multi-file torrent to grab)
- DHT (trackerless peer discovery), seeding after completion, force recheck
- Per-torrent download/upload speed limits and an optional seed ratio limit
  (once reached, the torrent auto-stops seeding)
- Session persistence across restarts using libtorrent's own resume-data,
  so a restart resumes quickly instead of doing a full recheck

Torrents keep seeding after they finish downloading by default (normal
torrent etiquette) -- right-click a finished torrent and choose **Stop
Seeding** to stop it manually, or set a seed ratio limit when adding it (or
a default one in Settings) to have it stop automatically once reached.

**Torrent search** (its own "Search" tab, plus REST + MCP)
- Searches multiple torrent indexers at once and merges the results:
  duplicates are collapsed by infohash, seeders are taken as the max across
  indexers (two sites reporting the same swarm are one swarm), and results
  are ranked -- log-scaled seeders dominate, recent releases get a boost,
  sub-16KiB junk "torrents" are penalized, and a result corroborated by
  multiple independent indexers scores higher
- One click (or double-click, or right-click) adds a result straight to the
  Torrents tab and starts downloading; right-click also offers Copy Magnet
- Providers ship for **torrents-csv** (public API, works out of the box)
  and **Jackett** (point it at your local Jackett instance and its API key
  in settings.json to search everything Jackett proxies); adding another
  indexer means one small adapter class in `adp/search/providers.py`
- A failing indexer is reported in the results line but never fails the
  search; searches run on a worker thread so the UI never blocks
- The same search is exposed as `POST /search` on the REST API and as a
  `search_torrents` MCP tool, so an AI assistant can search, pick, and
  `add_torrent` in one conversation -- same rule as everything else: one
  implementation (`SearchService`) behind every surface
- Only download content you have the right to download

**Stats dashboard** (fourth tab)
- A rolling speed graph (download/upload, last 5 minutes) drawn with a
  lightweight custom widget -- no extra charting library dependency
- Session totals (this run) and lifetime totals (persisted across restarts):
  bytes transferred, downloads/torrents completed
- Swarm health when torrent support is available: active torrents,
  connected peers/seeds, DHT node count

**Storage locations & disk-space safety**
- Download folder and torrent folder are both configurable in Settings (with
  a live free-space readout) -- keep multi-GB torrents off your system drive
  instead of the default app-data location. Changing the torrent folder
  applies to new torrents immediately, no restart; existing torrents keep the
  path they started with.
- Before a torrent starts writing, its size is checked against free space on
  the target drive (with a 512 MiB headroom margin). If it won't fit, it's
  paused with an explanatory message rather than dying mid-write with a
  disk-full error -- free some space or change the folder, then Resume
  (Resume is a deliberate override and isn't re-checked).
- Byte counts above 2 GiB are handled correctly throughout (large-file and
  large-torrent sizes are carried as 64-bit across Qt signals).

**Pro GUI**
- Category auto-detection (Documents/Archives/Video/Audio/Images/Software)
  with a filter dropdown, plus free-text search
- Add-download dialog with live file-size/range-support probing
- Scheduling: queue a download to start at a specific date/time
- Drag-and-drop: drop a URL (or a browser-dragged link) straight onto the
  window to add it
- Clipboard monitoring: optionally get prompted when you copy a
  downloadable-looking link
- System tray icon with completion notifications (for both downloads and
  torrents); minimize-to-tray on close
- Dark/light theme toggle
- Session persistence -- your queue survives an app restart

## Diagnosing a failed download

Every run writes a detailed, rotating log file so you don't have to
reproduce a problem with a debugger attached to see what happened:

- **Location**: an OS-appropriate per-user data directory --
  `%APPDATA%\AcceleratedDownloaderPro\logs\adp.log` on Windows,
  `~/Library/Application Support/AcceleratedDownloaderPro/logs/adp.log` on
  macOS, `~/.local/share/AcceleratedDownloaderPro/logs/adp.log` on Linux
  (or `$XDG_DATA_HOME` if set). It rotates at 5 MB, keeping 3 backups.
- **In the app**: the toolbar has a **View Logs** button that opens the
  log folder directly.
- **What's in it**: for every download -- the URL, save path, thread
  count, and any speed limit when it starts; the server's metadata
  (size, whether it supports byte ranges, ETag); each worker's HTTP
  status and Content-Range per chunk; warnings if a chunk finished with
  fewer bytes than expected (a sign the server ignored the Range header
  or closed the connection early); and on any failure, the full
  exception traceback plus the HTTP status code if one was returned.
  Uncaught exceptions anywhere in the app are also captured here.
- The console (when run from a terminal) shows a lighter INFO-level
  summary; the file always gets the full DEBUG-level detail.

If you're reporting a bug, the log file is the first thing to attach.

## Installation

**Windows (recommended):** run `setup.bat`. It creates the `.venv`, installs
dependencies, and does an editable install with the test tools
(`pip install -e ".[dev]"`) so `adp` is importable everywhere and the test
suite has its plugins -- then `run.bat`, `test.bat`, and a bare `pytest` all
work with no `PYTHONPATH` juggling. Companion scripts (`run.bat`, `test.bat`,
etc.) are written on first run.

**Any platform (manual):**

```bash
pip install -r requirements.txt
pip install -e .        # editable install: puts src/ on the path
```

The project uses a `src/` layout, so `adp` is only importable after that
editable install (or with `src/` on `PYTHONPATH`). This is deliberate -- it
means tests and `python -m adp.main` run against the same package that ships.

**Torrent support is optional.** The app depends on `libtorrent`, a native
C-extension with platform/Python-version-specific wheels. If it fails to
install or import, the app still launches fine with a fully working
Downloads tab -- the Torrents tab just shows a message explaining how to
enable it, instead of the app crashing on startup.

If `pip install libtorrent` doesn't work for your platform:
- Confirm your Python version has a wheel on
  [PyPI's libtorrent project page](https://pypi.org/project/libtorrent/#files) --
  e.g. Windows wheels are published for CPython 3.10/3.11/3.12 as
  `win_amd64`. If you're on a version without a wheel, use a supported
  Python version instead of building from source.
- Try `pip install --upgrade pip` first -- an older pip can fail to resolve
  a wheel that does exist.
- As a last resort, `conda install -c conda-forge libtorrent` works on
  platforms where the PyPI wheel doesn't fit your exact Python build.

## Configuration

Settings live in `settings.json` in the per-user app-data dir
(`%APPDATA%\AcceleratedDownloaderPro\` on Windows), created and managed by the
app -- edit them through the in-app Settings dialog rather than by hand. The
repo's `settings.example.json` documents every key and its default for
reference; the app does not read that file.

## Running

```bash
run.bat                 # Windows, after setup.bat
# or:
python -m adp.main
# or, after the editable install:
adp-downloader
```

## Controlling it via API (REST or MCP)

The app always runs a local REST API and an MCP server, both on the same
port (default `8765`, configurable in Settings), bound to `127.0.0.1`
only. Click **API Access** in the toolbar to get the base URL, MCP
endpoint, and API key -- every request needs the key in an `X-API-Key`
header, whichever interface you use.

### REST

```bash
curl -H "X-API-Key: <your key>" http://127.0.0.1:8765/downloads

curl -H "X-API-Key: <your key>" -H "Content-Type: application/json" \
     -d '{"url": "https://example.com/file.zip"}' \
     http://127.0.0.1:8765/downloads
```

Endpoints: `GET/POST /downloads`, `GET/POST/DELETE /downloads/{id}` plus
`/pause`, `/resume`, `/stop`, `/retry`; the same shape under `/torrents`
(swap `/stop`+`/retry` for `/force_recheck`+`/select_files`); and
`GET /stats`. Interactive docs (Swagger UI) are at `/docs` once the app is
running.

### MCP

Point any MCP-compatible client at `http://127.0.0.1:8765/mcp` (streamable
HTTP transport) with the API key as an `X-API-Key` header. Tools mirror the
REST endpoints one-to-one (`add_download`, `pause_torrent`, `get_stats`,
etc.) -- see `src/adp/api/mcp_tools.py` for the full list and descriptions
each tool exposes to the calling model.

### Security notes

- The API can add/pause/remove downloads and torrents -- treat the key
  like a password. It's stored locally at `<app data dir>/api_key.txt`;
  regenerate it from the API Access dialog if it's ever been exposed
  somewhere it shouldn't have been.
- It only ever binds to `127.0.0.1`; nothing on your network (or the
  internet) can reach it.
- No CORS headers are added anywhere, so a malicious web page can't
  silently drive the API through a browser even if it somehow knew the key.

## Project layout

```
src/adp/
  core/          GUI-independent engine: downloader, models, session,
                 settings, scheduler, speed limiter
  gui/           PyQt6 widgets: main window, dialogs, tray icon, theme
  utils/         Pure helper functions (formatting, URL heuristics)
  dev/           Optional dev-only tools (see below)
  main.py        Application entry point
tests/           pytest suite (see below)
```

## Development / dev tools

`src/adp/dev/test_rig.py` is an optional manual-testing tool: it embeds a
real browser (`QWebEngineView`) with the download panel docked beside it, so
you can click "download" links on real websites and watch them land in the
queue. It's not part of the shipped app or the automated test suite, and
needs an extra dependency:

```bash
pip install PyQt6-WebEngine
python -m adp.dev.test_rig
```

## Testing

```bash
pip install -e ".[dev]"     # pulls pytest-qt, pytest-timeout, pytest-mock
pytest
```

The `[dev]` extra is required: the GUI/API/torrent tests need `pytest-qt`
(for the `qtbot`/`qapp` fixtures) and `pytest-timeout`. If they're missing,
the suite stops immediately with a one-line message telling you to run the
install above, rather than emitting a wall of "fixture not found" errors.
(`pip install -r requirements-dev.txt` also works.)

This runs the full suite **except** the two tests marked `network`, which
hit a real external endpoint (httpbin.org) rather than the local mock
server used by everything else. Run those explicitly with:

```bash
pytest -m network
```

Tests marked `torrent` (the torrent engine and panel suites) run by
default -- they use a real local BitTorrent swarm on loopback, not the
internet, so they don't need the same opt-in treatment.

The suite is organized as:
- `test_downloader_engine.py` -- the core HTTP engine, against a real local
  HTTP server (`tests/mock_server.py`) that supports range requests and can
  simulate dropped connections, so resume/retry logic is exercised for real
  rather than mocked away.
- `test_torrent_engine.py` -- the torrent engine, against a real local
  BitTorrent swarm (`tests/torrent_swarm.py`): a seed session with real data
  and a leeching `TorrentEngine`, connected directly (no tracker/DHT needed)
  the same way the HTTP tests use a real local server instead of a mock.
  Covers adding via `.torrent` file and magnet link, metadata resolution,
  pause/resume, per-file selection, seeding after completion, and removal.
- `test_torrent_panel.py` -- pytest-qt tests driving the real `TorrentPanel`
  widget (add/category-filter/remove/session round-trip), plus the add-torrent
  dialog's validation.
- `test_stats_aggregator.py`, `test_stats_store.py` -- fast, fully offline
  unit tests for the dashboard's byte-counting logic and persistence.
- `test_stats_panel.py` -- pytest-qt tests driving the real `StatsPanel`
  widget against real downloads.
- `test_api_bridge.py`, `test_api_auth.py` -- fast, fully offline unit
  tests for the cross-thread GUI bridge and the API key store.
- `test_api_controller.py`, `test_api_controller_torrents.py` -- the
  `AppController` business logic, called from a real background thread
  (as the REST/MCP servers actually do) against real downloads/torrents.
- `test_api_rest_server.py` -- the REST API via FastAPI's TestClient,
  including auth rejection and full CRUD flows.
- `test_api_mcp.py` -- the MCP server's tools, plus one full end-to-end
  test using the official MCP client SDK against a real running server
  (real port, real HTTP, real JSON-RPC session handshake).
- `test_speed_limiter.py`, `test_scheduler.py`, `test_session.py`,
  `test_format_utils.py`, `test_url_utils.py` -- fast, fully offline unit
  tests for the supporting modules.
- `test_gui_smoke.py` -- pytest-qt tests that drive the real `DownloadPanel`
  widget (add/search/filter/pause/stop/schedule/settings/session round-trip).
- `test_network_smoke.py` -- opt-in tests against httpbin.org.

### Running headlessly

GUI tests need a Qt platform plugin. `offscreen` is the simplest and fastest
option and is what this suite is written for:

```bash
QT_QPA_PLATFORM=offscreen pytest
```

(`xvfb-run pytest` also works if you'd rather render to a virtual display,
but if your Qt install is missing `libxcb-cursor0` you'll need
`offscreen` instead.)

### A note on full-suite runtime

Because the engine tests intentionally exercise real blocking sockets
(`requests`/`urllib3`) inside `QThreadPool` workers, a worker that's
mid-retry against a connection the test has already torn down can't be
interrupted cooperatively -- it just has to finish its (now short, ~3s
worst case) retry backoff. A couple of tests deliberately leave one of
these in flight to test `stop()`/teardown behavior. This doesn't affect
whether tests pass, but left alone it can make the interpreter slow to
exit after the run; `tests/conftest.py` handles this by forcing a clean
exit once pytest has finished reporting results.

If you're running on a heavily loaded/CPU-constrained CI host and see an
occasional timing-related flake on `test_pause_then_resume`, that's a
resource-contention artifact of running many concurrent local HTTP servers
in one process, not a logic bug -- re-running it (or running
`tests/test_downloader_engine.py` on its own) will confirm.

## License

See `LICENSE`.
