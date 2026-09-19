#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for ``store.py`` (owner B).  stdlib ``unittest`` only.

Run::

    C:\\Users\\mengz\\AppData\\Local\\Programs\\Python\\Python314\\python.exe ^
        -m unittest tests.test_store -v

Every test uses a temporary root.  As a second guard, ``APPDATA`` and
``LOCALAPPDATA`` are pointed at a temporary directory for the duration of the
module, and the module asserts afterwards that the user's real
``%APPDATA%\\Book Reader`` was not created by this run.

Covers the six verified failure modes from docs/research/product-spec.md
(clean roundtrip, .bak creation, truncated primary recovered from .bak, both
files trashed -> quarantined and reset, schema 0->1 migration, future schema ->
read-only) plus: debounce coalescing, bounded latency, immediate highlight and
bookmark writes, blocking flush on quit, Chinese round-trip, book_id stability
and caching, ui.language / reader.theme normalization, the one-time legacy
directory migration, kill-during-write atomicity and concurrent mutation.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import store as S  # noqa: E402

REAL_APPDATA = os.environ.get("APPDATA", "")
REAL_LOCALAPPDATA = os.environ.get("LOCALAPPDATA", "")
_REAL_ROOT_EXISTED = os.path.exists(os.path.join(REAL_APPDATA, S.APP_DIR_NAME))
_REAL_CACHE_EXISTED = os.path.exists(os.path.join(REAL_LOCALAPPDATA, S.APP_DIR_NAME))

REAL_BOOKS = [
    r"C:\Users\mengz\Desktop\文件\Econ Books\50人的二十年_樊纲 易纲等.epub",
    r"C:\Users\mengz\Desktop\文件\Econ Books\Econ Books_China\从此岸到彼岸_人民币汇率如何实现清洁浮动_缪延亮.epub",
]

CJK_TEXT = "以外汇储备为代表的官方资本改变了全球资本流动和金融市场格局。"
ASTRAL = "\U00020000\U0002A6D6"          # CJK Ext-B, outside the BMP
NOTE = "对照第 5 章的三元悖论讨论 — 「引号」『二重』"
LEGACY_LOG = S.LEGACY_DIR_NAME.lower() + ".log"

_GUARD_DIR = ""
_SAVED_ENV: dict[str, str | None] = {}


def setUpModule() -> None:  # noqa: N802
    global _GUARD_DIR
    _GUARD_DIR = tempfile.mkdtemp(prefix="er_store_env_")
    for key in ("APPDATA", "LOCALAPPDATA"):
        _SAVED_ENV[key] = os.environ.get(key)
        os.environ[key] = os.path.join(_GUARD_DIR, key)


def tearDownModule() -> None:  # noqa: N802
    for key, value in _SAVED_ENV.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    shutil.rmtree(_GUARD_DIR, ignore_errors=True)
    if not _REAL_ROOT_EXISTED:
        assert not os.path.exists(os.path.join(REAL_APPDATA, S.APP_DIR_NAME)), \
            "test run created the REAL %APPDATA%\\Book Reader"
    if not _REAL_CACHE_EXISTED:
        assert not os.path.exists(os.path.join(REAL_LOCALAPPDATA, S.APP_DIR_NAME)), \
            "test run created the REAL %LOCALAPPDATA%\\Book Reader"


def sample_highlight() -> dict:
    loc = {"gpos": 4820, "path": [12], "offset": 133, "text": CJK_TEXT[:20],
           "before": "本节讨论的核心问题是"}
    return {"spine_index": 7, "spine_href": "ops/chapter3.xhtml",
            "start": loc, "end": dict(loc, gpos=4874, offset=187),
            "text": CJK_TEXT + ASTRAL, "note": NOTE, "color": "yellow",
            "style": "fill", "chapter_title": "第三章 汇率的锚", "book_progress": 0.3412}


def read_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def read_raw(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class TempRootCase(unittest.TestCase):
    """Each test gets a fresh root whose path contains CJK and a space."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="er_store_")
        self.root = os.path.join(self.tmp, "测试 根目录")
        self.stores: list[S.Store] = []

    def tearDown(self) -> None:
        for st in self.stores:
            st.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self, **kw) -> S.Store:
        st = S.Store(self.root, **kw)
        self.stores.append(st)
        return st


# ==========================================================================
# identity & locations
# ==========================================================================

class IdentityTests(unittest.TestCase):
    def test_constants(self) -> None:
        self.assertEqual(S.APP_DIR_NAME, "Book Reader")
        self.assertEqual(S.PROG_ID, "BookReader.Book.1")
        self.assertEqual(S.PIPE_NAME, "book-reader-single-instance")
        self.assertEqual(S.LOG_FILE_NAME, "book-reader.log")
        self.assertNotIn(" ", S.PROG_ID)

    def test_locations_come_from_environment(self) -> None:
        self.assertEqual(S.app_dir(), os.path.join(os.environ["APPDATA"], "Book Reader"))
        self.assertEqual(S.cache_dir(),
                         os.path.join(os.environ["LOCALAPPDATA"], "Book Reader", "cache"))
        self.assertEqual(S.log_file(r"X:\r"), os.path.join(r"X:\r", "logs", "book-reader.log"))

    def test_no_retired_name_outside_migration_constant(self) -> None:
        """Retired names (EPUB Reader, Verso) may appear in code only as LEGACY_* constants."""
        import ast
        with open(os.path.join(ROOT, "store.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        retired = [n.lower() for n in S.LEGACY_DIR_NAMES] + [n.lower() for n in S.LEGACY_LOG_NAMES]
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if any(r in node.value.lower() for r in retired):
                    offenders.append(node)
        allowed = set()
        for node in ast.walk(tree):                     # the LEGACY_* definitions themselves
            if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "").startswith("LEGACY_"):
                allowed |= {id(c) for c in ast.walk(node)}
        docstrings = set()
        for node in ast.walk(tree):                     # docstrings describe the migration
            body = getattr(node, "body", None)
            if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                    and isinstance(getattr(body[0], "value", None), ast.Constant):
                docstrings.add(id(body[0].value))
        bad = [n.value[:60] for n in offenders if id(n) not in allowed and id(n) not in docstrings]
        self.assertEqual(bad, [])
        self.assertEqual(S.LEGACY_DIR_NAMES, ("EPUB Reader", "Verso"))


# ==========================================================================
# the six failure modes
# ==========================================================================

class FailureModeTests(TempRootCase):
    bid = "3f8a1c0b9d2e4f6a7b8c9d0e1f2a3b4c"

    def test_1_clean_roundtrip(self) -> None:
        st = self.make()
        hl = st.add_highlight(self.bid, sample_highlight())
        st.save_position(self.bid, {"spine_index": 7, "locator": {"gpos": 1, "text": CJK_TEXT}},
                         force=True)
        st.close()
        st2 = self.make()
        state = st2.book_state(self.bid)
        self.assertEqual(state["highlights"][0]["id"], hl["id"])
        self.assertEqual(state["highlights"][0]["note"], NOTE)
        self.assertEqual(state["highlights"][0]["text"], CJK_TEXT + ASTRAL)
        self.assertEqual(state["position"]["locator"]["text"], CJK_TEXT)
        self.assertEqual(st2.load_notes, [])
        leftovers = [n for n in os.listdir(st2.books_dir) if n.startswith(".tmp_")]
        self.assertEqual(leftovers, [])

    def test_2_bak_created(self) -> None:
        st = self.make()
        path = st.book_state_path(self.bid)
        st.add_bookmark(self.bid, {"spine_index": 1, "label": "第一"})
        self.assertFalse(os.path.exists(path + ".bak"))
        st.add_bookmark(self.bid, {"spine_index": 2, "label": "第二"})
        self.assertTrue(os.path.exists(path + ".bak"))
        self.assertEqual(len(read_raw(path + ".bak")["bookmarks"]), 1)   # previous generation
        self.assertEqual(len(read_raw(path)["bookmarks"]), 2)

    def test_3_truncated_primary_recovered_from_bak(self) -> None:
        st = self.make()
        path = st.book_state_path(self.bid)
        st.add_highlight(self.bid, sample_highlight())
        st.add_bookmark(self.bid, {"spine_index": 3, "label": "书签"})
        st.close()
        raw = read_bytes(path).decode("utf-8")
        with open(path, "w", encoding="utf-8") as f:       # simulate a kill mid-write
            f.write(raw[: len(raw) // 2])
        st2 = self.make()
        state = st2.book_state(self.bid)
        self.assertEqual(len(state["highlights"]), 1)         # the .bak generation
        self.assertEqual(state["highlights"][0]["note"], NOTE)
        self.assertTrue(any("backup+healed" in n for n in st2.load_notes), st2.load_notes)
        corrupt = [n for n in os.listdir(st2.books_dir) if ".corrupt-" in n]
        self.assertEqual(len(corrupt), 1)                     # wreckage preserved
        self.assertEqual(read_raw(path)["highlights"][0]["note"], NOTE)   # primary healed
        self.assertEqual(read_raw(path + ".bak")["highlights"][0]["note"], NOTE)

    def test_4_both_trashed_quarantined_and_reset(self) -> None:
        st = self.make()
        path = st.book_state_path(self.bid)
        st.add_highlight(self.bid, sample_highlight())
        st.add_highlight(self.bid, sample_highlight())
        st.close()
        with open(path, "w", encoding="utf-8") as f:
            f.write("{{{")
        with open(path + ".bak", "w", encoding="utf-8") as f:
            f.write("nope")
        st2 = self.make()
        state = st2.book_state(self.bid)
        self.assertEqual(state["highlights"], [])
        self.assertEqual(state["book_id"], self.bid)
        self.assertIn(f"books/{self.bid}.json: reset:CORRUPT_QUARANTINED", st2.load_notes)
        corrupt = [n for n in os.listdir(st2.books_dir) if ".corrupt-" in n]
        self.assertEqual(len(corrupt), 1)
        with open(os.path.join(st2.books_dir, corrupt[0]), encoding="utf-8") as f:
            self.assertEqual(f.read(), "{{{")                 # nothing deleted

    def test_4b_repeated_quarantine_never_overwrites(self) -> None:
        os.makedirs(self.root, exist_ok=True)
        p = os.path.join(self.root, "library.json")
        for i in range(3):
            with open(p, "w", encoding="utf-8") as f:
                f.write(f"garbage {i}")
            S.read_json(p, S.default_library)
        corrupt = sorted(n for n in os.listdir(self.root) if ".corrupt-" in n)
        self.assertEqual(len(corrupt), 3, corrupt)

    def test_5_schema_0_to_1_migration(self) -> None:
        os.makedirs(self.root, exist_ok=True)
        path = os.path.join(self.root, "settings.json")
        legacy = {"app": S.LEGACY_DIR_NAME, "ui": {"language": "zh"},
                  "reader": {"theme": "night", "font_size_px": 25},
                  "themes": {"day": {"bg": "#FFFFFF"}}}           # no "schema" key = v0
        with open(path, "w", encoding="utf-8") as f:
            json.dump(legacy, f, ensure_ascii=False)
        st = self.make()
        self.assertEqual(st.get("reader.font_size_px"), 25)       # user value kept
        self.assertEqual(st.get("reader.line_height"), 1.9)       # default filled in
        self.assertEqual(st.get("ui.language"), "zh-Hans")
        self.assertEqual(st.get("reader.theme"), "dark")
        self.assertIsNone(st.get("themes"))
        self.assertTrue(any("MIGRATED(0->1)" in n for n in st.load_notes), st.load_notes)
        on_disk = read_raw(path)                                   # written back
        self.assertEqual(on_disk["schema"], 1)
        self.assertEqual(on_disk["app"], "Book Reader")
        self.assertEqual(on_disk["ui"]["language"], "zh-Hans")
        self.assertEqual(read_raw(path + ".bak"), legacy)          # original kept

    def test_5b_unmigratable_preserves_original(self) -> None:
        os.makedirs(self.root, exist_ok=True)
        path = os.path.join(self.root, "library.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"schema": -3, "books": [{"id": "x"}]}, f)
        st = self.make()
        self.assertEqual(st.library(), [])
        self.assertTrue(any("UNMIGRATABLE(-3)" in n for n in st.load_notes))
        kept = [n for n in os.listdir(self.root) if ".unmigratable-" in n]
        self.assertEqual(len(kept), 1)

    def test_6_future_schema_is_read_only(self) -> None:
        os.makedirs(self.root, exist_ok=True)
        path = os.path.join(self.root, "settings.json")
        future = {"schema": 99, "ui": {"language": "zh"}, "reader": {"theme": "night"}}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(future, f)
        before = read_bytes(path)
        st = self.make()
        self.assertTrue(st.read_only)
        self.assertIn("READ_ONLY_FUTURE_SCHEMA(99)", st.read_only_reason)
        self.assertTrue(st.is_read_only(st.settings_path))
        st.set("reader.font_size_px", 30)
        st.settings.window.dock_width = 400
        self.assertTrue(st.flush())
        self.assertEqual(read_bytes(path), before)        # untouched, not normalized
        self.assertFalse(os.path.exists(path + ".bak"))
        # other files keep saving: a future settings file must not cost a highlight
        st.add_highlight(self.bid, sample_highlight())
        self.assertEqual(read_raw(st.book_state_path(self.bid))["highlights"][0]["note"], NOTE)


# ==========================================================================
# write policy
# ==========================================================================

class WritePolicyTests(TempRootCase):
    bid = "0123456789abcdef0123456789abcdef"

    def writes_to(self, st: S.Store, path: str) -> int:
        return st._writer.write_log.count(path)

    def test_debounce_coalesces_bursts(self) -> None:
        st = self.make()
        t0 = time.monotonic()
        for i in range(300):
            st.set("reader.font_size_px", 14 + i % 19)
            st.settings.window.dock_width = 200 + i
        burst = time.monotonic() - t0
        self.assertLess(burst, 0.4, "burst itself must be fast (no I/O on the caller)")
        self.assertFalse(os.path.exists(st.settings_path))          # nothing yet
        time.sleep(0.25)
        self.assertEqual(self.writes_to(st, st.settings_path), 0)   # still inside 500 ms
        time.sleep(0.6)
        self.assertEqual(self.writes_to(st, st.settings_path), 1)   # 600 calls -> 1 write
        self.assertEqual(read_raw(st.settings_path)["window"]["dock_width"], 499)
        self.assertGreaterEqual(st.stats["requests"], 600)

    def test_bounded_latency_under_continuous_change(self) -> None:
        st = self.make()
        path = st.book_state_path(self.bid)
        first_seen = None
        t0 = time.monotonic()
        i = 0
        while time.monotonic() - t0 < 3.0:
            st.save_position(self.bid, {"spine_index": 0, "locator": {"gpos": i}})
            i += 1
            if first_seen is None and os.path.exists(path):
                first_seen = time.monotonic() - t0
            time.sleep(0.05)
        self.assertIsNotNone(first_seen, "a continuously scrolling reader was never saved")
        self.assertLess(first_seen, 2.6)
        self.assertGreaterEqual(first_seen, 1.9)

    def test_highlights_and_bookmarks_hit_disk_immediately(self) -> None:
        st = self.make()
        path = st.book_state_path(self.bid)
        st.save_position(self.bid, {"spine_index": 4, "locator": {"gpos": 77}})  # debounced
        self.assertFalse(os.path.exists(path))
        hl = st.add_highlight(self.bid, sample_highlight())
        on_disk = read_raw(path)                                  # no flush() call
        self.assertEqual([h["id"] for h in on_disk["highlights"]], [hl["id"]])
        self.assertEqual(on_disk["position"]["locator"]["gpos"], 77)   # rides along
        bm = st.add_bookmark(self.bid, {"spine_index": 4, "label": "重要"})
        self.assertEqual(read_raw(path)["bookmarks"][0]["id"], bm["id"])
        self.assertTrue(st.update_highlight(self.bid, hl["id"], note="改过的笔记"))
        self.assertEqual(read_raw(path)["highlights"][0]["note"], "改过的笔记")
        self.assertTrue(st.remove_bookmark(self.bid, bm["id"]))
        self.assertEqual(read_raw(path)["bookmarks"], [])
        # an immediate book write must not force the unrelated debounced settings
        st.set("reader.line_height", 2.1)
        st.add_highlight(self.bid, sample_highlight())
        self.assertIn(st.settings_path, st.stats["pending"])

    def test_flush_on_quit_is_blocking_and_complete(self) -> None:
        st = self.make()
        st.set("ui.language", "ja")                                   # 500 ms
        st.save_position(self.bid, {"spine_index": 9, "locator": {"gpos": 123}})  # 2000 ms
        st.library_upsert({"id": self.bid, "title": "从此岸到彼岸", "path": r"C:\书\a.epub"})
        st.library_update(self.bid, progress=0.5)                     # 60 s
        self.assertEqual(len(st.stats["pending"]), 3)
        t0 = time.monotonic()
        self.assertTrue(st.flush())
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 1.0, "flush must not wait out the debounce windows")
        self.assertEqual(st.stats["pending"], [])
        self.assertEqual(read_raw(st.settings_path)["ui"]["language"], "ja")
        self.assertEqual(read_raw(st.book_state_path(self.bid))["position"]["locator"]["gpos"], 123)
        lib = read_raw(st.library_path)
        self.assertEqual(lib["books"][0]["progress"], 0.5)
        self.assertEqual(lib["books"][0]["title"], "从此岸到彼岸")
        st.close()
        st2 = self.make()
        self.assertEqual(st2.get("ui.language"), "ja")
        self.assertEqual(st2.library_get(self.bid)["path"], r"C:\书\a.epub")

    def test_flush_without_writer_thread_writes_inline(self) -> None:
        st = self.make(start_writer=False)
        st.set("reader.font_size_px", 26)
        self.assertTrue(st.flush())
        self.assertEqual(read_raw(st.settings_path)["reader"]["font_size_px"], 26)

    def test_close_then_save_is_ignored_not_crashing(self) -> None:
        st = self.make()
        st.close()
        st.set("reader.font_size_px", 30)
        st.save_book_state(self.bid, S.default_book_state(self.bid), immediate=True)
        self.assertFalse(os.path.exists(st.book_state_path(self.bid)))

    def test_chinese_round_trip_is_real_utf8(self) -> None:
        st = self.make()
        st.add_highlight(self.bid, sample_highlight())
        st.library_upsert({"id": self.bid, "title": "五十人的二十年", "authors": ["樊纲", "易纲"],
                           "path": r"C:\Users\mengz\Desktop\文件\Econ Books\x.epub"}, immediate=True)
        raw = read_bytes(st.book_state_path(self.bid))
        self.assertIn(CJK_TEXT.encode("utf-8"), raw)
        self.assertIn(ASTRAL.encode("utf-8"), raw)
        self.assertNotIn(b"\\u", raw)                                # ensure_ascii=False
        raw_lib = read_bytes(st.library_path)
        self.assertIn("樊纲".encode("utf-8"), raw_lib)
        self.assertNotIn(b"\r\n", raw)                               # newline="\n"
        st.close()
        st2 = self.make()
        self.assertEqual(st2.book_state(self.bid)["highlights"][0]["text"], CJK_TEXT + ASTRAL)
        self.assertEqual(st2.library_get(self.bid)["authors"], ["樊纲", "易纲"])

    def test_concurrent_unlocked_mutation_loses_nothing(self) -> None:
        st = self.make()
        state = st.book_state(self.bid)
        state["history"] = [{"gpos": i, "at": "x" * 50} for i in range(4000)]
        stop = threading.Event()

        def mutate() -> None:          # a careless caller: mutates without the lock
            n = 0
            while not stop.is_set():
                state["stats"][f"k{n % 50}"] = n
                if n % 50 == 49:
                    for j in range(50):
                        state["stats"].pop(f"k{j}", None)
                n += 1

        t = threading.Thread(target=mutate)
        t.start()
        try:
            for _ in range(8):
                st.save_book_state(self.bid, state, immediate=True)
        finally:
            stop.set()
            t.join()
        st.save_book_state(self.bid, state, immediate=True)
        self.assertEqual(st.stats["errors"], [])
        self.assertEqual(len(read_raw(st.book_state_path(self.bid))["history"]), 4000)

    def test_serialization_race_is_retried(self) -> None:
        st = self.make()
        real = S.json.dumps
        failures = [2]

        def flaky(*a, **k):
            if failures[0]:
                failures[0] -= 1
                raise RuntimeError("dictionary changed size during iteration")
            return real(*a, **k)

        S.json.dumps = flaky
        try:
            st.add_highlight(self.bid, sample_highlight())
        finally:
            S.json.dumps = real
        self.assertEqual(failures[0], 0)
        self.assertEqual(st.stats["errors"], [])
        self.assertEqual(read_raw(st.book_state_path(self.bid))["highlights"][0]["note"], NOTE)

    def test_transient_replace_failure_is_retried(self) -> None:
        st = self.make()
        real = S.os.replace
        failures = [3]

        def locked(src, dst):          # e.g. an AV scanner holding the target open
            if dst.endswith(".json") and failures[0]:
                failures[0] -= 1
                raise PermissionError(13, "The process cannot access the file")
            return real(src, dst)

        S.os.replace = locked
        try:
            t0 = time.monotonic()
            st.add_bookmark(self.bid, {"spine_index": 1, "label": "锁定"})
            elapsed = time.monotonic() - t0
        finally:
            S.os.replace = real
        self.assertEqual(failures[0], 0)
        self.assertEqual(st.stats["errors"], [])
        self.assertEqual(read_raw(st.book_state_path(self.bid))["bookmarks"][0]["label"], "锁定")
        self.assertLess(elapsed, 3.0)
        leftovers = [n for n in os.listdir(st.books_dir) if n.startswith(".tmp_")]
        self.assertEqual(leftovers, [])


# ==========================================================================
# settings semantics
# ==========================================================================

class SettingsTests(TempRootCase):
    def test_defaults(self) -> None:
        st = self.make()
        self.assertEqual(st.get("ui.language"), "auto")
        self.assertEqual(st.get("reader.theme"), "system")
        self.assertEqual(st.settings.reader.font_size_px, 21)
        self.assertIsNone(st.get("themes"))
        self.assertIsNone(st.get("highlight_colors"))
        self.assertEqual(st.get("app"), "Book Reader")

    def test_language_values(self) -> None:
        self.assertEqual(S.normalize_language("zh"), "zh-Hans")
        self.assertEqual(S.normalize_language("en"), "en")
        self.assertEqual(S.normalize_language("zh_TW"), "zh-Hant")
        self.assertEqual(S.normalize_language("zh-HK"), "zh-Hant")
        self.assertEqual(S.normalize_language("zh_CN"), "zh-Hans")
        self.assertEqual(S.normalize_language("ja-JP"), "ja")
        self.assertEqual(S.normalize_language("en_GB"), "en")
        self.assertEqual(S.normalize_language("fr"), "auto")
        self.assertEqual(S.normalize_language(None), "auto")
        for v in S.UI_LANGUAGES:
            self.assertEqual(S.normalize_language(v), v)
        st = self.make()
        st.set("ui.language", "zh")
        self.assertEqual(st.get("ui.language"), "zh-Hans")
        st.settings.ui.language = "zh-Hant"
        self.assertTrue(st.flush())
        self.assertEqual(read_raw(st.settings_path)["ui"]["language"], "zh-Hant")

    def test_legacy_en_value_in_existing_file(self) -> None:
        os.makedirs(self.root, exist_ok=True)
        with open(os.path.join(self.root, "settings.json"), "w", encoding="utf-8") as f:
            json.dump({"schema": 1, "app": "Book Reader", "ui": {"language": "en"},
                       "reader": {"theme": "paper"}}, f)
        st = self.make()
        self.assertEqual(st.get("ui.language"), "en")
        self.assertEqual(st.get("reader.theme"), "sepia")

    def test_theme_values(self) -> None:
        for old, new in (("day", "light"), ("paper", "sepia"), ("night", "dark"),
                         ("DARK", "dark"), ("bogus", "system"), (None, "system")):
            self.assertEqual(S.normalize_theme_choice(old), new)
        st = self.make()
        st.set("reader.theme", "night")
        self.assertEqual(st.reader_settings(None)["theme"], "dark")

    def test_attribute_access(self) -> None:
        st = self.make()
        st.settings.reader.font_size_px = 23
        self.assertEqual(st.get("reader.font_size_px"), 23)
        self.assertEqual(st.settings["reader.font_size_px"], 23)
        self.assertIn("reader.theme", st.settings)
        with self.assertRaises(AttributeError):
            _ = st.settings.reader.no_such_key
        d = st.settings.reader.to_dict()
        d["font_size_px"] = 99
        self.assertEqual(st.get("reader.font_size_px"), 23)        # copy, not live

    def test_reader_settings_merge_overrides(self) -> None:
        st = self.make()
        bid = "ab" * 16
        st.set("reader.font_size_px", 22)
        st.set_override(bid, "font_size_px", 26)
        st.set_override(bid, "line_height", 2.2)
        merged = st.reader_settings(bid)
        self.assertEqual(merged["font_size_px"], 26)
        self.assertEqual(merged["line_height"], 2.2)
        self.assertEqual(st.reader_settings(None)["font_size_px"], 22)
        merged["font_size_px"] = 1
        self.assertEqual(st.book_state(bid)["overrides"]["font_size_px"], 26)
        st.clear_overrides(bid)
        self.assertEqual(st.reader_settings(bid)["font_size_px"], 22)

    def test_reset_keeps_window_and_language(self) -> None:
        st = self.make()
        st.set("ui.language", "ja")
        st.set("window.dock_width", 333)
        st.set("reader.font_size_px", 30)
        st.reset_settings()
        self.assertEqual(st.get("ui.language"), "ja")
        self.assertEqual(st.get("window.dock_width"), 333)
        self.assertEqual(st.get("reader.font_size_px"), 21)


# ==========================================================================
# library, covers, identity
# ==========================================================================

class LibraryTests(TempRootCase):
    def test_upsert_merge_and_remove_keeps_book_state(self) -> None:
        st = self.make()
        bid = "cd" * 16
        st.library_upsert({"id": bid, "title": "旧书名", "path": r"C:\a.epub"})
        st.library_upsert({"id": bid, "title": "新书名"})
        entry = st.library_get(bid)
        self.assertEqual((entry["title"], entry["path"]), ("新书名", r"C:\a.epub"))
        self.assertEqual(entry["hash_algo"], "blake2b-128")
        st.add_highlight(bid, sample_highlight())
        st.library_remove(bid)
        self.assertTrue(st.flush())
        self.assertIsNone(st.library_get(bid))
        self.assertEqual(read_raw(st.library_path)["books"], [])
        self.assertTrue(os.path.exists(st.book_state_path(bid)))   # annotations survive
        with self.assertRaises(ValueError):
            st.library_upsert({"title": "no id"})

    def test_cover_path_under_store_cache_root(self) -> None:
        st = self.make()
        p = st.cover_path("ef" * 16)
        self.assertTrue(p.startswith(os.path.join(self.root, "cache", "covers")))
        self.assertTrue(p.endswith(".jpg"))
        self.assertTrue(os.path.isdir(os.path.dirname(p)))
        st2 = S.Store(os.path.join(self.tmp, "r2"), cache_root=os.path.join(self.tmp, "本地缓存"))
        self.stores.append(st2)
        self.assertTrue(st2.cover_path("x").startswith(os.path.join(self.tmp, "本地缓存", "covers")))

    def test_book_id_stable_and_cached(self) -> None:
        st = self.make()
        path = os.path.join(self.tmp, "书 样本.epub")
        with open(path, "wb") as f:
            f.write(os.urandom(3 * 1024 * 1024 + 17))
        calls = []
        real = S.book_id

        def counting(p):
            calls.append(p)
            return real(p)

        S.book_id = counting
        try:
            a = st.book_id_for(path)
            b = st.book_id_for(path)
            self.assertEqual(a, b)
            self.assertEqual(len(a), 32)
            self.assertEqual(len(calls), 1, "second call must hit the (path,size,mtime_ns) cache")
            # different spelling of the same path is the same cache key
            st.book_id_for(os.path.join(self.tmp, ".", "书 样本.epub").upper())
            self.assertEqual(len(calls), 1)
            # a changed file is rehashed
            stat = os.stat(path)
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
            self.assertEqual(st.book_id_for(path), a)     # same bytes -> same id
            self.assertEqual(len(calls), 2)
            with open(path, "ab") as f:
                f.write(b"x")
            c = st.book_id_for(path)
            self.assertNotEqual(c, a)
            self.assertEqual(len(calls), 3)
            # a fresh store is seeded from library.json and does not rehash
            stat = os.stat(path)
            st.library_upsert({"id": c, "path": path, "size": stat.st_size,
                               "mtime_ns": stat.st_mtime_ns}, immediate=True)
            st.close()
            st2 = self.make()
            self.assertEqual(st2.book_id_for(path), c)
            self.assertEqual(len(calls), 3)
        finally:
            S.book_id = real

    def test_book_id_on_real_books_is_stable_and_read_only(self) -> None:
        present = [p for p in REAL_BOOKS if os.path.exists(p)]
        if not present:
            self.skipTest("user's real books not present")
        st = self.make()
        for p in present:
            before = os.stat(p)
            a = st.book_id_for(p)
            b = S.book_id(p)
            after = os.stat(p)
            self.assertEqual(a, b)
            self.assertRegex(a, r"^[0-9a-f]{32}$")
            self.assertEqual((before.st_size, before.st_mtime_ns),
                             (after.st_size, after.st_mtime_ns))


# ==========================================================================
# one-time legacy directory migration
# ==========================================================================

class LegacyMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="er_migrate_")
        self.appdata = os.path.join(self.tmp, "Roaming")
        self.local = os.path.join(self.tmp, "Local")
        self.old = os.path.join(self.appdata, S.LEGACY_DIR_NAME)
        self.new = os.path.join(self.appdata, S.APP_DIR_NAME)
        os.makedirs(os.path.join(self.old, "books"))
        os.makedirs(os.path.join(self.old, "logs"))
        os.makedirs(os.path.join(self.local, S.LEGACY_DIR_NAME, "cache", "covers"))
        with open(os.path.join(self.old, "settings.json"), "w", encoding="utf-8") as f:
            json.dump({"schema": 1, "app": S.LEGACY_DIR_NAME, "ui": {"language": "zh"},
                       "reader": {"theme": "paper", "font_size_px": 24}}, f)
        with open(os.path.join(self.old, "books", "aa.json"), "w", encoding="utf-8") as f:
            json.dump({"schema": 1, "book_id": "aa", "highlights": [{"id": "h1", "note": NOTE}]},
                      f, ensure_ascii=False)
        for name in (LEGACY_LOG, LEGACY_LOG + ".1"):
            with open(os.path.join(self.old, "logs", name), "w", encoding="utf-8") as f:
                f.write("日志")
        with open(os.path.join(self.local, S.LEGACY_DIR_NAME, "cache", "covers", "aa.jpg"), "wb") as f:
            f.write(b"\xff\xd8jpeg")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_moves_once_then_never_looks_again(self) -> None:
        notes = S.migrate_legacy_dirs(self.appdata, self.local)
        self.assertEqual(len(notes), 2, notes)
        self.assertFalse(os.path.exists(self.old))
        self.assertFalse(os.path.exists(os.path.join(self.local, S.LEGACY_DIR_NAME)))
        self.assertTrue(os.path.exists(os.path.join(self.local, "Book Reader", "cache", "covers", "aa.jpg")))
        self.assertTrue(os.path.exists(os.path.join(self.new, "logs", "book-reader.log")))
        self.assertTrue(os.path.exists(os.path.join(self.new, "logs", "book-reader.log.1")))
        self.assertFalse(any(S.LEGACY_DIR_NAME.lower() in n.lower() for n in os.listdir(os.path.join(self.new, "logs"))))
        # a legacy dir that reappears later is ignored: the new root exists
        os.makedirs(self.old)
        self.assertEqual(S.migrate_legacy_dirs(self.appdata, self.local), [])
        self.assertTrue(os.path.isdir(self.old))
        # the migrated state opens normally and is normalized
        with S.Store(self.new) as st:
            self.assertEqual(st.get("app"), "Book Reader")
            self.assertEqual(st.get("ui.language"), "zh-Hans")
            self.assertEqual(st.get("reader.theme"), "sepia")
            self.assertEqual(st.get("reader.font_size_px"), 24)
            self.assertEqual(st.book_state("aa")["highlights"][0]["note"], NOTE)

    def test_v1_epub_reader_state_migrates_to_book_reader(self) -> None:
        """v1.0 stored everything under "EPUB Reader"; v1.1 renamed the app."""
        shutil.rmtree(self.old)                                  # only the v1.0 layout this time
        v1 = os.path.join(self.appdata, "EPUB Reader")
        os.makedirs(os.path.join(v1, "books"))
        os.makedirs(os.path.join(v1, "logs"))
        os.makedirs(os.path.join(self.local, "EPUB Reader", "cache", "converted"))
        with open(os.path.join(v1, "books", "bb.json"), "w", encoding="utf-8") as f:
            json.dump({"schema": 1, "book_id": "bb", "highlights": [{"id": "h2", "note": NOTE}]},
                      f, ensure_ascii=False)
        with open(os.path.join(v1, "logs", "epub-reader.log"), "w", encoding="utf-8") as f:
            f.write("v1 log")
        notes = S.migrate_legacy_dirs(self.appdata, self.local)
        self.assertTrue(notes)
        self.assertFalse(os.path.exists(v1))
        with open(os.path.join(self.new, "books", "bb.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["highlights"][0]["note"], NOTE)     # nothing lost
        self.assertTrue(os.path.exists(os.path.join(self.new, "logs", "book-reader.log")))
        self.assertTrue(os.path.isdir(os.path.join(self.local, "Book Reader", "cache", "converted")))
        # the older development directory is left alone once a newer one was migrated
        self.assertEqual(S.migrate_legacy_dirs(self.appdata, self.local), [])

    def test_newest_legacy_name_wins(self) -> None:
        os.makedirs(os.path.join(self.appdata, "EPUB Reader", "books"))
        S.migrate_legacy_dirs(self.appdata, self.local)
        self.assertTrue(os.path.isdir(self.old))                 # Verso untouched
        self.assertTrue(os.path.isdir(os.path.join(self.new, "books")))

    def test_existing_new_root_is_never_overwritten(self) -> None:
        os.makedirs(self.new)
        self.assertEqual(S.migrate_legacy_dirs(self.appdata, self.local), [])
        self.assertTrue(os.path.isdir(self.old))
        self.assertEqual(os.listdir(self.new), [])

    def test_nothing_to_migrate(self) -> None:
        shutil.rmtree(self.old)
        self.assertEqual(S.migrate_legacy_dirs(self.appdata, self.local), [])
        self.assertFalse(os.path.exists(self.new))

    def test_locked_file_falls_back_to_copy(self) -> None:
        handle = open(os.path.join(self.old, "logs", LEGACY_LOG), "a", encoding="utf-8")
        try:
            notes = S.migrate_legacy_dirs(self.appdata, self.local)
        finally:
            handle.close()
        self.assertTrue(any("copied" in n for n in notes), notes)
        self.assertTrue(os.path.isdir(self.old))                     # original left alone
        self.assertEqual(read_raw(os.path.join(self.new, "books", "aa.json"))["highlights"][0]["note"],
                         NOTE)
        self.assertFalse(os.path.exists(self.new + ".migrating"))

    def test_store_default_root_runs_migration(self) -> None:
        saved = os.environ["APPDATA"], os.environ["LOCALAPPDATA"]
        os.environ["APPDATA"], os.environ["LOCALAPPDATA"] = self.appdata, self.local
        try:
            with S.Store() as st:
                self.assertEqual(st.root, self.new)
                self.assertEqual(st.cache_root, os.path.join(self.local, "Book Reader", "cache"))
                self.assertTrue(st.migration_notes)
                self.assertEqual(st.get("ui.language"), "zh-Hans")
        finally:
            os.environ["APPDATA"], os.environ["LOCALAPPDATA"] = saved


# ==========================================================================
# atomicity under a hard kill
# ==========================================================================

_HAMMER = r"""
import sys, os
sys.path.insert(0, sys.argv[1])
import store
path = sys.argv[2]
doc = {"book_id": "k", "highlights": [{"id": "h%d" % i, "text": "汇率" * 40, "note": "笔记" * 20}
                                      for i in range(1500)]}
print("ready", flush=True)
n = 0
while True:
    doc["n"] = n
    store.write_json(path, doc)
    n += 1
"""


class KillDuringWriteTests(unittest.TestCase):
    def test_terminate_mid_write_never_loses_the_file(self) -> None:
        tmp = tempfile.mkdtemp(prefix="er_kill_")
        try:
            script = os.path.join(tmp, "hammer.py")
            with open(script, "w", encoding="utf-8") as f:
                f.write(_HAMMER)
            target = os.path.join(tmp, "书", "k.json")
            os.makedirs(os.path.dirname(target))
            outcomes = []
            for i in range(12):
                proc = subprocess.Popen([sys.executable, script, ROOT, target],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.assertEqual(proc.stdout.readline().strip(), b"ready")
                time.sleep(0.15 + (i * 0.037) % 0.3)
                proc.kill()                                            # TerminateProcess
                proc.wait(10)
                proc.stdout.close()
                proc.stderr.close()
                obj, tag = S.read_json(target, lambda: {"reset": True})
                self.assertIn(tag, ("primary", "backup"), f"kill #{i}: {tag}")
                self.assertEqual(len(obj["highlights"]), 1500)
                outcomes.append((tag, obj.get("n")))
            ns = [n for _, n in outcomes]
            self.assertTrue(any(n and n > 0 for n in ns), outcomes)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
