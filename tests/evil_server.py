# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
"""An adversarial HTTP server for security/robustness testing.

Where mock_server.py models a *well-behaved* server, this one models the
hostile and broken ones the download engine must defend against: servers that
ignore Range headers, lie about Content-Range, over-send, or try to smuggle
compression into a byte-range transfer. The engine's job is to detect these
and fail the download rather than silently writing corrupt bytes.

Each endpoint corresponds to a specific attack/bug class; the tests in
test_adversarial_download.py assert the engine rejects each one.
"""
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


# A body big enough that the manager splits it into multiple ranged chunks.
EVIL_BODY = bytes((i % 251) for i in range(200_000))


class _EvilHandler(BaseHTTPRequestHandler):
    # Set per-server via the factory below.
    mode = "ignore_range"

    def log_message(self, *args):
        pass  # keep test output clean

    def _range(self):
        """Parse a Range header into (start, end) against EVIL_BODY, or None."""
        raw = self.headers.get("Range")
        if not raw or not raw.startswith("bytes="):
            return None
        try:
            s, _, e = raw[len("bytes="):].partition("-")
            start = int(s)
            end = int(e) if e else len(EVIL_BODY) - 1
            return start, end
        except ValueError:
            return None

    def do_HEAD(self):
        # Always advertise range support and a size, so the manager goes
        # multi-connection and actually exercises the ranged path.
        self.send_response(200)
        self.send_header("Content-Length", str(len(EVIL_BODY)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def do_GET(self):
        rng = self._range()

        if self.mode == "ignore_range":
            # Hostile: ignore the Range entirely, answer 200 with the WHOLE
            # file. Writing this at a chunk's offset corrupts the output.
            self.send_response(200)
            self.send_header("Content-Length", str(len(EVIL_BODY)))
            self.end_headers()
            self.wfile.write(EVIL_BODY)
            return

        if self.mode == "wrong_content_range":
            # 206, but Content-Range claims a different start than requested.
            start, end = rng if rng else (0, len(EVIL_BODY) - 1)
            lied_start = start + 1000
            self.send_response(206)
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Content-Range",
                             f"bytes {lied_start}-{end}/{len(EVIL_BODY)}")
            self.end_headers()
            self.wfile.write(EVIL_BODY[start:end + 1])
            return

        if self.mode == "wrong_content_range_end":
            # 206 with the right start but a wider end than requested, and a
            # Content-Length consistent with that wider slice -- a fully
            # self-consistent response for a slice we didn't ask for. Only the
            # end-matching check catches this (the Content-Length cross-check
            # can't, since the server made them agree).
            start, end = rng if rng else (0, len(EVIL_BODY) - 1)
            lied_end = min(end + 5000, len(EVIL_BODY) - 1)
            self.send_response(206)
            self.send_header("Content-Length", str(lied_end - start + 1))
            self.send_header("Content-Range",
                             f"bytes {start}-{lied_end}/{len(EVIL_BODY)}")
            self.end_headers()
            self.wfile.write(EVIL_BODY[start:lied_end + 1])
            return

        if self.mode == "wrong_content_range_total":
            # 206 whose reported total differs from what the download is based
            # on -- the resource changed size under us.
            start, end = rng if rng else (0, len(EVIL_BODY) - 1)
            self.send_response(206)
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Content-Range",
                             f"bytes {start}-{end}/{len(EVIL_BODY) + 999999}")
            self.end_headers()
            self.wfile.write(EVIL_BODY[start:end + 1])
            return

        if self.mode == "unknown_total":
            # 206 with an unknown total ("bytes start-end/*"). For a sized,
            # resumable, multi-part download we can't confirm the resource is
            # unchanged, so this must be rejected.
            start, end = rng if rng else (0, len(EVIL_BODY) - 1)
            self.send_response(206)
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Content-Range", f"bytes {start}-{end}/*")
            self.end_headers()
            self.wfile.write(EVIL_BODY[start:end + 1])
            return

        if self.mode == "no_content_range":
            # 206 status but omits Content-Range entirely.
            start, end = rng if rng else (0, len(EVIL_BODY) - 1)
            self.send_response(206)
            self.send_header("Content-Length", str(end - start + 1))
            self.end_headers()
            self.wfile.write(EVIL_BODY[start:end + 1])
            return

        if self.mode == "oversend":
            # Correct 206 headers, but stream MORE bytes than the range.
            start, end = rng if rng else (0, len(EVIL_BODY) - 1)
            self.send_response(206)
            self.send_header("Content-Range",
                             f"bytes {start}-{end}/{len(EVIL_BODY)}")
            # Deliberately don't set an honest Content-Length; push extra.
            self.end_headers()
            overflow = EVIL_BODY[start:end + 1] + b"\xff" * 5000
            try:
                self.wfile.write(overflow)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        # Fallback: behave correctly (used as a control).
        start, end = rng if rng else (0, len(EVIL_BODY) - 1)
        self.send_response(206 if rng else 200)
        if rng:
            self.send_header("Content-Range",
                             f"bytes {start}-{end}/{len(EVIL_BODY)}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        self.wfile.write(EVIL_BODY[start:end + 1])


class EvilServer:
    """Context-managed adversarial server. `mode` selects the misbehavior."""

    def __init__(self, mode: str):
        handler = type(f"EvilHandler_{mode}", (_EvilHandler,), {"mode": mode})
        self._httpd = HTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)

    @property
    def url(self) -> str:
        host, port = self._httpd.server_address
        return f"http://{host}:{port}/evil.bin"
