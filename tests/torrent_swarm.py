"""A minimal local BitTorrent 'swarm' for offline testing: one seed session
holding real data, bound to loopback with tracker/DHT/LSD disabled, so
tests connect a leeching TorrentEngine directly to it (bypassing any real
network dependency) the same way tests/mock_server.py gives the HTTP engine
a real local server instead of a mock.
"""
from __future__ import annotations

import os
import socket

import libtorrent as lt


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class LocalSeed:
    """Creates a single-file torrent from real bytes on disk and seeds it
    from its own libtorrent session, bound to 127.0.0.1 with no tracker/DHT/
    LSD -- tests must connect_peer() to it directly."""

    def __init__(self, seed_dir: str):
        self.seed_dir = seed_dir
        self.port = _free_port()
        self.session = lt.session({
            "listen_interfaces": f"127.0.0.1:{self.port}",
            "enable_dht": False,
            "enable_lsd": False,
            "enable_upnp": False,
            "enable_natpmp": False,
        })
        self.torrent_info = None
        self.handle = None

    def seed_file(self, filename: str, content: bytes, piece_size: int = 16384):
        os.makedirs(self.seed_dir, exist_ok=True)
        file_path = os.path.join(self.seed_dir, filename)
        with open(file_path, 'wb') as f:
            f.write(content)

        fs = lt.file_storage()
        lt.add_files(fs, file_path)
        ct = lt.create_torrent(fs, piece_size=piece_size)
        ct.set_creator("adp-test-swarm")
        lt.set_piece_hashes(ct, self.seed_dir)
        torrent_dict = ct.generate()
        torrent_bytes = lt.bencode(torrent_dict)
        self.torrent_info = lt.torrent_info(torrent_bytes)

        self.handle = self.session.add_torrent({"ti": self.torrent_info, "save_path": self.seed_dir})
        return torrent_bytes

    def write_torrent_file(self, torrent_bytes: bytes, path: str):
        with open(path, 'wb') as f:
            f.write(torrent_bytes)

    def stop(self):
        if self.handle is not None:
            try:
                self.session.remove_torrent(self.handle)
            except RuntimeError:
                pass
