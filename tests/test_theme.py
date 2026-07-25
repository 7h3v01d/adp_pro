# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
"""Theme system contracts: default resolution, palette completeness, and
that the QSS templates render without leftover placeholders."""
from adp.gui.theme import DARK, DARK_QSS, LIGHT, LIGHT_QSS, STATUS, stylesheet_for


def test_dark_is_the_default():
    # Anything that isn't explicitly "light" -- including unknown/legacy
    # values -- resolves to the dark-industrial theme.
    assert stylesheet_for("dark") == DARK_QSS
    assert stylesheet_for("light") == LIGHT_QSS
    assert stylesheet_for("") == DARK_QSS
    assert stylesheet_for("something-else") == DARK_QSS
    assert stylesheet_for(None) == DARK_QSS


def test_palettes_have_matching_keys():
    # The two themes must define the same tokens, or .format() on the shared
    # template would raise KeyError for a missing one.
    assert set(DARK) == set(LIGHT)


def test_all_tokens_substituted_into_qss():
    # Every palette color should be used somewhere -- either in the rendered
    # QSS (proving .format() filled it) or exposed via STATUS for the
    # custom-painted widgets (speed graph, progress chunks). A token that's
    # in neither is dead and should be removed.
    status_values = set(STATUS.values())
    for token_name, value in DARK.items():
        assert value in DARK_QSS or value in status_values, \
            f"dark token {token_name} ({value}) is unused"
    for token_name, value in LIGHT.items():
        # A handful of accents (e.g. amber) are palette colors consumed by
        # custom-painted widgets, not QSS selectors -- they only need to be
        # real, named colors kept in sync across themes.
        _PAINT_ONLY = {"amber"}
        if token_name in _PAINT_ONLY:
            assert value.startswith("#")
            continue
        assert value in LIGHT_QSS, f"light token {token_name} ({value}) not used in QSS"


def test_signature_tokens_present():
    # The house style: obsidian base + the four accents.
    assert DARK["obsidian"] == "#0b0f14"
    assert DARK["teal"] == "#2fd6c3"
    assert DARK["phosphor"] == "#4be08a"
    assert DARK["amber"] == "#ffb454"
    assert DARK["red"] == "#ff5c66"


def test_zero_radius_and_mono_font():
    assert "border-radius: 0px" in DARK_QSS
    assert "JetBrains Mono" in DARK_QSS


def test_groupbox_title_space_reserved():
    # Regression guard: a styled QGroupBox loses its native frame, so the
    # QSS must reserve title space (margin-top) and position the title, or
    # section headers collapse over the first content row.
    for qss in (DARK_QSS, LIGHT_QSS):
        assert "QGroupBox {" in qss
        assert "QGroupBox::title" in qss
        assert "margin-top" in qss


def test_status_colors_exposed_for_custom_widgets():
    for key in ("success", "error", "warning", "idle", "download", "upload"):
        assert STATUS[key].startswith("#")
