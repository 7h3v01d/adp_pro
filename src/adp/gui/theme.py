# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
"""Themes for Accelerated Downloader Pro.

Dark-industrial design system (the default), plus a High-Contrast Light
companion. Signature language: obsidian base, teal/phosphor/amber/red
accents, steel hairlines, JetBrains Mono, flat zero-radius controls.

Colors are defined once as named tokens and formatted into the QSS, so a
palette change happens in exactly one place. The two themes share the same
structural QSS template and differ only in their token set.

Copyright 2026 Leon Priest (7h3v01d). Apache License 2.0.
"""

from __future__ import annotations

# --- Dark-industrial tokens ------------------------------------------------
DARK = {
    "obsidian":  "#0b0f14",   # window base
    "panel":     "#10161d",   # raised surfaces (lists, tables, inputs)
    "panel_alt": "#0d1319",   # alternating rows / header strips
    "steel":     "#232b35",   # hairline borders
    "steel_hi":  "#2f3a47",   # hover/active borders
    "text":      "#c9d4de",   # primary text
    "dim":       "#5c6b7a",   # secondary text, captions
    "teal":      "#2fd6c3",   # primary accent
    "teal_dim":  "#1c8479",   # pressed/darker teal
    "phosphor":  "#4be08a",   # success / seeders / go
    "amber":     "#ffb454",   # secondary action / warning
    "red":       "#ff5c66",   # danger / errors
    "on_accent": "#0b0f14",   # text on accent fills (obsidian)
    "sel_bg":    "#123832",   # selected row (teal-tinted)
    "sel_text":  "#eafffb",
}

# --- High-contrast light companion -----------------------------------------
LIGHT = {
    "obsidian":  "#eef1f4",
    "panel":     "#ffffff",
    "panel_alt": "#f4f6f8",
    "steel":     "#c2ccd6",
    "steel_hi":  "#9aa7b4",
    "text":      "#141a20",
    "dim":       "#5c6b7a",
    "teal":      "#0f9b8e",
    "teal_dim":  "#0b7469",
    "phosphor":  "#1f9d57",
    "amber":     "#b9761a",
    "red":       "#c0303a",
    "on_accent": "#ffffff",
    "sel_bg":    "#cdeee9",
    "sel_text":  "#08201d",
}

# --- Structural QSS (tokens filled per theme) ------------------------------
# Zero border-radius everywhere, JetBrains Mono, flat fills, hairline steel
# borders that brighten on hover -- the house style.
_TEMPLATE = """
* {{
    font-family: "JetBrains Mono", "Cascadia Mono", "Consolas", monospace;
    font-size: 13px;
    border-radius: 0px;
}}
QWidget {{ background-color: {obsidian}; color: {text}; }}
QMainWindow, QDialog {{ background-color: {obsidian}; }}
QToolTip {{
    background-color: {panel}; color: {text};
    border: 1px solid {steel}; padding: 4px 6px;
}}

/* -- lists & tables -- */
QListWidget, QTableWidget, QTreeWidget {{
    background-color: {panel}; border: 1px solid {steel};
    alternate-background-color: {panel_alt};
    outline: none;
}}
QListWidget::item {{ border-bottom: 1px solid {steel}; padding: 2px; }}
QListWidget::item:selected, QTableWidget::item:selected {{
    background-color: {sel_bg}; color: {sel_text};
}}
QListWidget::item:hover, QTableWidget::item:hover {{ background-color: {panel_alt}; }}
QTableWidget {{ gridline-color: {steel}; }}
QHeaderView::section {{
    background-color: {panel_alt}; color: {dim};
    border: none; border-bottom: 1px solid {steel}; border-right: 1px solid {steel};
    padding: 5px 8px; font-size: 11px; letter-spacing: 1px; text-transform: uppercase;
}}
QTableCornerButton::section {{ background-color: {panel_alt}; border: none; }}

/* -- inputs -- */
QLineEdit, QSpinBox, QComboBox, QDateTimeEdit, QPlainTextEdit, QTextEdit {{
    background-color: {panel}; color: {text};
    border: 1px solid {steel}; padding: 5px 7px; selection-background-color: {sel_bg};
    selection-color: {sel_text};
}}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QDateTimeEdit:focus,
QPlainTextEdit:focus, QTextEdit:focus {{ border: 1px solid {teal}; }}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{ color: {dim}; }}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{
    background-color: {panel}; border: 1px solid {steel};
    selection-background-color: {sel_bg}; selection-color: {sel_text};
}}
QLineEdit::placeholder {{ color: {dim}; }}

/* -- buttons -- */
QPushButton {{
    background-color: {teal}; color: {on_accent};
    border: 1px solid {teal}; padding: 6px 16px; font-weight: 600; letter-spacing: 1px;
}}
QPushButton:hover {{ background-color: {obsidian}; color: {teal}; }}
QPushButton:pressed {{ background-color: {teal_dim}; color: {on_accent}; border-color: {teal_dim}; }}
QPushButton:disabled {{ background-color: {panel}; color: {dim}; border-color: {steel}; }}
/* secondary = outlined steel, teal on hover */
QPushButton#secondary {{
    background-color: transparent; color: {text}; border: 1px solid {steel};
    font-weight: 400;
}}
QPushButton#secondary:hover {{ border-color: {teal}; color: {teal}; }}
QPushButton#secondary:pressed {{ border-color: {teal_dim}; color: {teal_dim}; }}
/* danger = outlined red */
QPushButton#danger {{
    background-color: transparent; color: {red}; border: 1px solid {red}; font-weight: 400;
}}
QPushButton#danger:hover {{ background-color: {red}; color: {on_accent}; }}

/* -- progress -- */
QProgressBar {{
    border: 1px solid {steel}; background-color: {panel}; color: {text};
    text-align: center; height: 16px; font-size: 11px;
}}
QProgressBar::chunk {{ background-color: {phosphor}; }}

/* -- tabs -- */
QTabWidget::pane {{ border: 1px solid {steel}; top: -1px; }}
QTabBar::tab {{
    background-color: {panel_alt}; color: {dim};
    border: 1px solid {steel}; border-bottom: none;
    padding: 7px 18px; margin-right: 2px; letter-spacing: 1px;
}}
QTabBar::tab:selected {{ background-color: {obsidian}; color: {teal}; border-bottom: 2px solid {teal}; }}
QTabBar::tab:hover:!selected {{ color: {text}; }}

/* -- checkboxes -- */
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{
    width: 15px; height: 15px; border: 1px solid {steel}; background-color: {panel};
}}
QCheckBox::indicator:hover {{ border-color: {teal}; }}
QCheckBox::indicator:checked {{ background-color: {teal}; border-color: {teal}; }}

/* -- menus -- */
QMenu {{ background-color: {panel}; border: 1px solid {steel}; padding: 4px; }}
QMenu::item {{ padding: 5px 22px; }}
QMenu::item:selected {{ background-color: {sel_bg}; color: {sel_text}; }}
QMenu::separator {{ height: 1px; background-color: {steel}; margin: 4px 2px; }}

/* -- toolbar & status -- */
QToolBar {{ background-color: {panel_alt}; border: none; border-bottom: 1px solid {steel}; spacing: 6px; padding: 5px; }}
QToolBar QToolButton {{ background-color: transparent; color: {text}; padding: 5px 10px; border: 1px solid transparent; }}
QToolBar QToolButton:hover {{ border: 1px solid {steel}; color: {teal}; }}
QStatusBar {{ background-color: {panel_alt}; color: {dim}; border-top: 1px solid {steel}; }}
QStatusBar::item {{ border: none; }}

/* -- scrollbars -- */
QScrollBar:vertical {{ background: {obsidian}; width: 12px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {steel}; min-height: 24px; }}
QScrollBar::handle:vertical:hover {{ background: {steel_hi}; }}
QScrollBar:horizontal {{ background: {obsidian}; height: 12px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: {steel}; min-width: 24px; }}
QScrollBar::handle:horizontal:hover {{ background: {steel_hi}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

/* -- category badge (pill kept subtle, still zero-radius house style) -- */
QLabel#categoryBadge {{
    background-color: {panel_alt}; color: {teal};
    border: 1px solid {steel}; padding: 1px 8px; font-size: 11px; letter-spacing: 1px;
}}

/* -- group boxes -- */
/* A styled QWidget makes Qt stop drawing the native groupbox frame/title,
   so we must position them ourselves: a top margin opens room for the
   title, and the title is lifted into that gap and given the teal accent.
   Without this the title collapses over the first content row. */
QGroupBox {{
    border: 1px solid {steel}; margin-top: 14px; padding: 12px 10px 8px 10px;
    background-color: {panel};
}}
QGroupBox::title {{
    subcontrol-origin: margin; subcontrol-position: top left;
    left: 10px; padding: 0 6px;
    color: {teal}; font-size: 11px; font-weight: 600; letter-spacing: 2px;
    text-transform: uppercase;
}}

/* -- splitters -- */
QSplitter::handle {{ background-color: {steel}; }}
"""


def _build(tokens: dict) -> str:
    return _TEMPLATE.format(**tokens)


DARK_QSS = _build(DARK)
LIGHT_QSS = _build(LIGHT)


def stylesheet_for(theme_name: str) -> str:
    """Returns the QSS for the named theme. Anything other than 'light'
    resolves to the dark-industrial default -- including the historical
    stored value and any unrecognized name."""
    return LIGHT_QSS if theme_name == "light" else DARK_QSS


# --- Semantic status colors ------------------------------------------------
# For custom-painted widgets (progress-bar chunks, the speed graph) that can't
# be reached by QSS selectors. Kept here so every color in the app traces back
# to this one file. Values are the dark-industrial accents; they read well on
# the light theme too, so they're intentionally theme-independent.
STATUS = {
    "success":   DARK["phosphor"],   # completed / seeding
    "error":     DARK["red"],        # error
    "warning":   DARK["amber"],      # stopped
    "idle":      DARK["dim"],        # paused / queued
    "download":  DARK["teal"],       # download speed trace
    "upload":    DARK["amber"],      # upload speed trace
    "active":    DARK["phosphor"],   # in-progress chunk
}
