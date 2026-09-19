# -*- coding: utf-8 -*-
"""Book Reader UI string tables, one module per language.

Nothing here is looked up directly by UI code; go through :mod:`strings`
(``from strings import S``).  This package only gathers the four tables:

    zh_Hans.py  简体中文
    zh_Hant.py  繁體中文
    en.py       English   (the reference the stubs were generated from)
    ja.py       日本語

Each module exports ``TABLE: dict[str, str]``.  All four must have identical
key sets and identical ``{placeholder}`` sets per key; run
``python strings.py --check`` after editing any of them.
"""

from __future__ import annotations

import os

from . import en, ja, zh_Hans, zh_Hant

__all__ = ["TABLES", "MODULE_FILES"]

#: Language code -> table.  Codes match ``strings.LANGUAGES``.
TABLES: dict[str, dict[str, str]] = {
    "zh-Hans": zh_Hans.TABLE,
    "zh-Hant": zh_Hant.TABLE,
    "en": en.TABLE,
    "ja": ja.TABLE,
}

#: Language code -> source file, for the checker's text scans.
MODULE_FILES: dict[str, str] = {
    "zh-Hans": os.path.abspath(zh_Hans.__file__),
    "zh-Hant": os.path.abspath(zh_Hant.__file__),
    "en": os.path.abspath(en.__file__),
    "ja": os.path.abspath(ja.__file__),
}
