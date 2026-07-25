# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
"""Packaging/licensing hygiene: every source file carries an SPDX header, and
pyproject declares the license, author, and classifiers. These guard the P2
release-readiness work so a new file or a metadata edit can't silently drop it.
"""
import pathlib
import tomllib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"


def _python_files():
    return sorted(SRC_ROOT.rglob("*.py"))


def test_every_source_file_has_spdx_header():
    missing = []
    for path in _python_files():
        head = path.read_text(encoding="utf-8")[:400]
        if "SPDX-License-Identifier: Apache-2.0" not in head:
            missing.append(str(path.relative_to(REPO_ROOT)))
    assert not missing, (
        "These source files are missing the SPDX license header:\n  "
        + "\n  ".join(missing)
    )


def test_every_source_file_has_copyright():
    missing = []
    for path in _python_files():
        head = path.read_text(encoding="utf-8")[:400]
        if "Copyright 2026 Leon Priest" not in head:
            missing.append(str(path.relative_to(REPO_ROOT)))
    assert not missing, (
        "These source files are missing the copyright line:\n  "
        + "\n  ".join(missing)
    )


def test_pyproject_declares_license_and_author():
    with open(REPO_ROOT / "pyproject.toml", "rb") as fh:
        project = tomllib.load(fh)["project"]
    assert project.get("license") == "Apache-2.0"
    authors = project.get("authors", [])
    assert any(a.get("name") == "Leon Priest" for a in authors)
    # A license classifier should be present for tooling that reads classifiers.
    assert any("Apache" in c for c in project.get("classifiers", []))


def test_license_file_present_and_apache():
    license_text = (REPO_ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Apache License" in license_text
    assert "Version 2.0" in license_text


def test_libtorrent_is_optional_not_core():
    with open(REPO_ROOT / "pyproject.toml", "rb") as fh:
        project = tomllib.load(fh)["project"]
    assert not any("libtorrent" in d for d in project["dependencies"]), \
        "libtorrent must stay in the [torrents] extra, not core dependencies"
    assert any("libtorrent" in d for d in project["optional-dependencies"]["torrents"])
