#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for strings.py + the i18n package (owner I).

stdlib unittest only.  Run::

    C:\\Users\\mengz\\AppData\\Local\\Programs\\Python\\Python314\\python.exe ^
        -m unittest tests.test_strings -v

No Qt is imported here; the live-retranslation proof against real widgets is a
separate GUI check (see docs/api-strings.md).
"""

from __future__ import annotations

import copy
import datetime as dt
import gc
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import strings  # noqa: E402
from strings import S  # noqa: E402

RETIRED = "Ver" + "so"   # never spelled out in project files


class _StateGuard(unittest.TestCase):
    """Save and restore module state so tests cannot leak into each other."""

    def setUp(self) -> None:
        self._saved = (strings._current, strings._preference, strings._strict,
                       list(strings.language_changed._subs))
        strings.language_changed.clear()
        strings.set_strict(True)

    def tearDown(self) -> None:
        (strings._current, strings._preference, strings._strict,
         strings.language_changed._subs) = self._saved

    def use(self, lang: str) -> None:
        strings._current = lang


class TablesTest(_StateGuard):
    def test_real_tables_pass_the_checker(self) -> None:
        self.assertEqual(strings.check(), [])

    def test_four_languages_with_endonyms(self) -> None:
        self.assertEqual(strings.LANGUAGES, ("zh-Hans", "zh-Hant", "en", "ja"))
        self.assertEqual(strings.LANGUAGE_NAMES,
                         {"zh-Hans": "简体中文", "zh-Hant": "繁體中文", "en": "English", "ja": "日本語"})
        self.assertEqual(set(strings.TABLES), set(strings.LANGUAGES))
        self.assertEqual(strings.APP_DISPLAY_NAME, "EPUB Reader")

    def test_identical_key_sets(self) -> None:
        keys = [set(strings.TABLES[l]) for l in strings.LANGUAGES]
        for other in keys[1:]:
            self.assertEqual(keys[0], other)
        self.assertGreater(len(keys[0]), 400)

    def test_every_value_renders_in_every_language(self) -> None:
        for lang in strings.LANGUAGES:
            for key, value in strings.TABLES[lang].items():
                names = strings._placeholders(value)
                out = strings.translate(lang, key, **{n: "X" for n in names})
                self.assertTrue(out.strip(), (lang, key))
                for name in names:
                    self.assertNotIn("{" + name + "}", out, (lang, key))

    def test_retired_name_absent_everywhere(self) -> None:
        for lang, table in strings.TABLES.items():
            for key, value in table.items():
                self.assertNotIn(RETIRED.lower(), (key + value).lower(), (lang, key))
        for path in strings._source_files():
            with open(path, encoding="utf-8") as fh:
                self.assertNotIn(RETIRED.lower(), fh.read().lower(), path)

    def test_spec_copy_for_zh_hans(self) -> None:
        self.use("zh-Hans")
        self.assertEqual(S("title.book", title="从此岸到彼岸"), "从此岸到彼岸 — EPUB Reader")
        self.assertEqual(S("title.library"), "EPUB Reader")
        self.assertEqual(S("menu.about"), "关于 EPUB Reader")
        self.assertEqual(S("toc.synthetic"), "本书未提供目录，以下为章节文件")
        self.assertEqual(plural("search.count", 2371), "共 2371 处")
        self.assertEqual(S("search.capped", n=500), "仅显示前 500 处")
        self.assertEqual(S("lib.empty.title"), "书架是空的")
        self.assertEqual(plural("lib.found", 4, folder="Desktop\\文件"), "在 Desktop\\文件 中发现 4 本书")
        self.assertEqual(S("lib.found.add"), "添加到书架")
        self.assertEqual(S("lib.found.dismiss"), "不用了")
        self.assertEqual(S("err.drm.body", kind=S("err.drm.adept")),
                         "检测到 Adobe ADEPT 加密。EPUB Reader 不解密受保护的文件，请用购买它的官方应用打开。")
        self.assertEqual(S("status.left.chapter", time=strings.duration(12)), "本章剩余 12 分钟")
        self.assertEqual(S("status.left.book", time=strings.duration(200)), "全书剩余 3 小时 20 分钟")
        self.assertEqual(S("set.language"), "界面语言")
        self.assertEqual([S(k) for k in ("theme.light", "theme.sepia", "theme.dark", "theme.system")],
                         ["日", "纸", "夜", "跟随系统"])


def plural(key: str, n: int, **fmt: object) -> str:
    return strings.plural(key, n, **fmt)


class CheckerCatchesTest(unittest.TestCase):
    """Each rule is proven by breaking a copy of the tables."""

    def broken(self, lang: str, key: str, value: str | None) -> list[str]:
        tables = copy.deepcopy(strings.TABLES)
        if value is None:
            del tables[lang][key]
        else:
            tables[lang][key] = value
        return strings.check(tables, scan_files=False)

    def assertCaught(self, problems: list[str], needle: str) -> None:
        self.assertTrue(any(needle in p for p in problems), f"{needle!r} not in {problems}")

    def test_missing_key(self) -> None:
        self.assertCaught(self.broken("ja", "tb.toc", None), "ja: missing key 'tb.toc'")

    def test_placeholder_mismatch(self) -> None:
        self.assertCaught(self.broken("zh-Hant", "search.none", "[zh-Hant] no results"), "placeholders")

    def test_bad_placeholder_syntax(self) -> None:
        self.assertCaught(self.broken("en", "search.none", "No results for {q!r}"), "only {name}")

    def test_empty_value(self) -> None:
        self.assertCaught(self.broken("en", "tb.toc", "   "), "is empty")

    def test_retired_name(self) -> None:
        self.assertCaught(self.broken("en", "about.tagline", f"{RETIRED} reader"), "retired product name")

    def test_hard_coded_app_name(self) -> None:
        self.assertCaught(self.broken("en", "about.tagline", "EPUB Reader is quiet"), "use {app}")

    def test_exclamation_marks(self) -> None:
        self.assertCaught(self.broken("en", "status.copied", "Copied!"), "exclamation")
        self.assertCaught(self.broken("zh-Hans", "status.copied", "已复制！"), "exclamation")

    def test_ascii_ellipsis(self) -> None:
        self.assertCaught(self.broken("en", "menu.open", "Open Book..."), "ellipsis")

    def test_emoji(self) -> None:
        self.assertCaught(self.broken("en", "status.copied", "Copied \U0001F44D"), "emoji")

    def test_mojibake(self) -> None:
        self.assertCaught(self.broken("zh-Hans", "tb.toc", "é\x87\x91è\x9e\x8d\ufffd"), "mojibake")

    def test_plural_partner(self) -> None:
        tables = copy.deepcopy(strings.TABLES)
        for table in tables.values():
            del table["lib.count.other"]
        self.assertCaught(strings.check(tables, scan_files=False), "no '.other' partner")

    def test_scripts_and_regional_terms(self) -> None:
        self.assertCaught(self.broken("zh-Hans", "tb.toc", "目錄"), "Traditional glyphs")
        self.assertCaught(self.broken("zh-Hant", "tb.toc", "目录"), "Simplified glyphs")
        self.assertCaught(self.broken("zh-Hant", "tb.search", "搜索"), "mainland term 搜索")
        self.assertCaught(self.broken("zh-Hant", "lib.ctx.reveal", "在文件夾中顯示"), "mainland term 文件")
        self.assertCaught(self.broken("ja", "tb.toc", "目录"), "Simplified Chinese glyphs")
        self.assertCaught(self.broken("ja", "dock.bookmarks", "書籤"), "Japanese UI uses しおり")
        self.assertEqual(self.broken("ja", "tb.toc", "目次"), [])
        self.assertEqual(self.broken("ja", "tb.search", "検索"), [])
        self.assertEqual(self.broken("zh-Hant", "tb.search", "搜尋"), [])

    def test_halfwidth_punctuation_after_cjk(self) -> None:
        self.assertCaught(self.broken("zh-Hans", "status.theme", "主题:{theme}"), "half-width punctuation")

    def test_tone_particles(self) -> None:
        self.assertCaught(self.broken("zh-Hans", "status.copied", "已复制哦"), "particle")
        self.assertCaught(self.broken("zh-Hans", "common.loading", "请稍候"), "请稍候")

    def test_strict_translations_flags_stubs(self) -> None:
        tables = copy.deepcopy(strings.TABLES)
        tables["ja"]["tb.toc"] = "[ja] Contents"
        problems = strings.check(tables, strict_translations=True, scan_files=False)
        self.assertCaught(problems, "ja: 'tb.toc' is still an untranslated stub")
        self.assertEqual(strings.stub_counts(tables)["ja"] >= 1, True)


class LookupTest(_StateGuard):
    def test_app_placeholder_is_automatic_and_overridable(self) -> None:
        self.use("en")
        self.assertEqual(S("menu.about"), "About EPUB Reader")
        self.assertEqual(S("menu.about", app="X"), "About X")

    def test_missing_key_strict_raises(self) -> None:
        with self.assertRaises(KeyError):
            S("no.such.key")

    def test_missing_key_frozen_returns_key(self) -> None:
        strings.set_strict(False)
        self.assertEqual(S("no.such.key"), "no.such.key")

    def test_missing_placeholder(self) -> None:
        self.use("en")
        with self.assertRaises(KeyError):
            S("search.none")
        strings.set_strict(False)
        self.assertEqual(S("search.none"), "No results for “{q}”")

    def test_key_named_like_a_parameter(self) -> None:
        self.use("en")
        # positional-only parameters: a placeholder called "key" or "n" cannot collide
        self.assertEqual(strings.translate("en", "search.position", i=1, n=5), "1 of 5")

    def test_has(self) -> None:
        self.assertTrue(strings.has("tb.toc"))
        self.assertFalse(strings.has("tb.nope"))

    def test_plural(self) -> None:
        self.use("en")
        self.assertEqual(strings.plural("lib.count", 1), "1 book")
        self.assertEqual(strings.plural("lib.count", 0), "0 books")
        self.assertEqual(strings.plural("lib.count", 2), "2 books")
        self.assertEqual(strings.plural("search.count", 2371, n="2,371"), "2,371 matches")
        self.assertEqual(strings.plural_category(1.0, "en"), "other")
        self.use("zh-Hans")
        self.assertEqual(strings.plural("lib.count", 1), "1 本书")
        self.assertEqual(strings.plural_category(1, "ja"), "other")

    def test_duration_short_and_long(self) -> None:
        self.use("en")
        cases = {0: "under 1 min", 0.4: "under 1 min", -5: "under 1 min", None: "under 1 min",
                 1: "1 min", 59: "59 min", 60: "1 h", 61: "1 h 1 min", 200: "3 h 20 min"}
        for minutes, want in cases.items():
            self.assertEqual(strings.duration(minutes), want, minutes)  # type: ignore[arg-type]
        longs = {0: "less than a minute", 1: "1 minute", 2: "2 minutes", 60: "1 hour",
                 120: "2 hours", 61: "1 hour 1 minute", 125: "2 hours 5 minutes"}
        for minutes, want in longs.items():
            self.assertEqual(strings.duration(minutes, long=True), want, minutes)
        self.assertEqual(S("status.read", time=strings.duration(125)), "Read for 2 h 5 min")
        self.use("zh-Hans")
        self.assertEqual(strings.duration(0), "不到 1 分钟")
        self.assertEqual(strings.duration(125, long=True), "2 小时 5 分钟")
        self.assertEqual(strings.duration(float("nan")), "不到 1 分钟")

    def test_format_date(self) -> None:
        today = dt.date(2026, 9, 16)
        self.use("en")
        self.assertEqual(strings.format_date(today, today=today), "Today")
        self.assertEqual(strings.format_date(dt.date(2026, 9, 15), today=today), "Yesterday")
        self.assertEqual(strings.format_date(dt.date(2026, 3, 2), today=today), "Mar 2")
        self.assertEqual(strings.format_date(dt.date(2025, 12, 31), today=today), "Dec 31, 2025")
        self.assertEqual(strings.format_date(today, today=today, relative=False), "Sep 16, 2026")
        self.assertEqual(strings.format_date(None), "")
        self.assertEqual(strings.format_date("garbage"), "garbage")
        self.use("zh-Hans")
        self.assertEqual(strings.format_date(dt.date(2026, 3, 2), today=today), "3月2日")
        self.assertEqual(strings.format_date(dt.date(2025, 12, 31), today=today), "2025年12月31日")
        # ISO string as store.py writes it; aware -> converted to local time first
        iso = "2025-06-01T12:00:00+08:00"
        local = dt.datetime.fromisoformat(iso).astimezone().date()
        self.assertEqual(strings.format_date(iso, today=today),
                         f"{local.year}年{local.month}月{local.day}日")

    def test_tip_keys_cheatsheet(self) -> None:
        self.use("zh-Hans")
        self.assertEqual(strings.tip("tb.toc", "toc"), "目录 (Ctrl+T)")
        self.assertEqual(strings.tip("tb.search", "search"), "搜索 (Ctrl+F)")
        self.assertEqual(strings.tip("tb.more"), "更多")
        self.assertEqual(strings.keys_label("find_next"), "F3 / n")
        for lang in strings.LANGUAGES:
            self.use(lang)
            sheet = strings.cheatsheet()
            self.assertEqual(len(sheet), 5)
            rows = [row for _title, group in sheet for row in group]
            self.assertEqual(len(rows), sum(len(r) for _g, r in strings.CHEATSHEET_LAYOUT))
            for combos, desc in rows:
                self.assertTrue(combos and all(combos), lang)
                self.assertTrue(desc.strip(), lang)
        # every audited binding from product-spec §3b is on the sheet
        on_sheet = {c for _g, rows in strings.CHEATSHEET_LAYOUT for kid, _d in rows for c in strings.KEYS[kid]}
        for combo in ("Space", "PageDown", "j", "k", "Ctrl+PageDown", "Ctrl+Home", "Alt+←", "Ctrl+G", "F3", "n",
                      "Shift+F3", "N", "Ctrl+T", "Ctrl+B", "Ctrl+E", "Ctrl+F", "/", "Ctrl+,", "Esc", "F1",
                      "Ctrl+/", "Ctrl+D", "Ctrl+C", "Ctrl+Shift+C", "Ctrl+1", "Ctrl+4", "Delete", "Ctrl+M",
                      "Ctrl+=", "Ctrl+-", "Ctrl+0", "Ctrl+Shift+D", "F11", "Ctrl+Shift+F", "Ctrl+O",
                      "Ctrl+Shift+L", "Ctrl+W", "Ctrl+Q", "Enter", "F5"):
            self.assertIn(combo, on_sheet)
        self.assertNotIn("Ctrl+P", on_sheet)

    def test_theme_and_font_labels(self) -> None:
        self.use("zh-Hans")
        self.assertEqual(strings.theme_label("day"), "日")
        self.assertEqual(strings.theme_label("light"), "日")
        self.assertEqual(strings.theme_label("paper"), "纸")
        self.assertEqual(strings.theme_label("night"), "夜")
        self.assertEqual(strings.font_label("Microsoft YaHei"), "微软雅黑")
        self.assertEqual(strings.font_label("Georgia"), "Georgia")
        with self.assertRaises(KeyError):
            strings.theme_label("neon")


class LanguageSelectionTest(_StateGuard):
    def test_resolve_auto_contract_cases(self) -> None:
        cases = {
            "zh_CN": "zh-Hans", "zh_SG": "zh-Hans", "zh-CN": "zh-Hans", "zh": "zh-Hans",
            "zh-Hans-CN": "zh-Hans", "zh-Hans-HK": "zh-Hans",
            "zh_TW": "zh-Hant", "zh_HK": "zh-Hant", "zh_MO": "zh-Hant", "zh-Hant-TW": "zh-Hant",
            "zh-TW": "zh-Hant", "zh_TW.UTF-8": "zh-Hant",
            "Chinese (Simplified)_China": "zh-Hans", "Chinese (Traditional)_Taiwan": "zh-Hant",
            "ja": "ja", "ja_JP": "ja", "ja-JP": "ja", "Japanese_Japan": "ja",
            "en_US": "en", "fr_FR": "en", "ko_KR": "en", "": "en", "C": "en",
            "zh-Hans": "zh-Hans", "zh-Hant": "zh-Hant",
        }
        for name, want in cases.items():
            self.assertEqual(strings.resolve_auto(name), want, name)

    def test_set_language_emits_only_on_change(self) -> None:
        calls: list[str] = []
        strings.language_changed.subscribe(calls.append)
        strings._current = "en"
        strings.set_language("en")
        self.assertEqual(calls, [])
        strings.set_language("ja")
        strings.set_language("ja")
        strings.set_language("zh-Hant")
        self.assertEqual(calls, ["ja", "zh-Hant"])
        self.assertEqual(strings.current_language(), "zh-Hant")
        self.assertEqual(strings.language_preference(), "zh-Hant")

    def test_auto_and_legacy_values(self) -> None:
        original = strings.system_locale_name
        try:
            strings.system_locale_name = lambda: "ja-JP"  # type: ignore[assignment]
            strings.set_language("auto")
            self.assertEqual((strings.current_language(), strings.language_preference()), ("ja", "auto"))
            strings.set_language(None)
            self.assertEqual(strings.language_preference(), "auto")
            strings.set_language("zh")          # the old settings default
            self.assertEqual((strings.current_language(), strings.language_preference()),
                             ("zh-Hans", "zh-Hans"))
            strings.set_language("zh_TW")
            self.assertEqual(strings.current_language(), "zh-Hant")
            choices = strings.language_choices()
            self.assertEqual([c for c, _ in choices], ["auto", "zh-Hans", "zh-Hant", "en", "ja"])
            self.assertIn("日本語", choices[0][1])
            self.assertEqual(dict(choices[1:]), strings.LANGUAGE_NAMES)
        finally:
            strings.system_locale_name = original  # type: ignore[assignment]

    def test_lazy_first_resolution_does_not_emit(self) -> None:
        calls: list[str] = []
        strings.language_changed.subscribe(calls.append)
        strings._current = None
        self.assertIn(strings.current_language(), strings.LANGUAGES)
        self.assertEqual(calls, [])

    def test_system_locale_on_this_machine(self) -> None:
        name = strings.system_locale_name()
        self.assertTrue(name)
        self.assertIn(strings.resolve_auto(name), strings.LANGUAGES)


class RegistryTest(_StateGuard):
    def test_zero_and_one_argument_subscribers(self) -> None:
        got: list[object] = []

        class Widget:
            def retranslate_ui(self) -> None:
                got.append("noarg")

            def on_lang(self, lang: str) -> None:
                got.append(lang)

        w = Widget()
        strings.language_changed.subscribe(w.retranslate_ui)
        strings.language_changed.subscribe(w.on_lang)
        strings.language_changed.emit("en")
        self.assertEqual(got, ["noarg", "en"])

    def test_bound_methods_are_weak_functions_strong(self) -> None:
        got: list[str] = []

        class Widget:
            def retranslate_ui(self, lang: str) -> None:
                got.append("w:" + lang)

        w = Widget()
        strings.language_changed.subscribe(w.retranslate_ui)
        strings.language_changed.subscribe(lambda lang: got.append("lambda:" + lang))
        self.assertEqual(len(strings.language_changed), 2)
        del w
        gc.collect()
        strings.language_changed.emit("ja")
        self.assertEqual(got, ["lambda:ja"])
        self.assertEqual(len(strings.language_changed), 1)

    def test_subscribe_twice_and_unsubscribe(self) -> None:
        got: list[str] = []

        class Widget:
            def retranslate_ui(self, lang: str) -> None:
                got.append(lang)

        w = Widget()
        bound = w.retranslate_ui
        self.assertIs(strings.language_changed.subscribe(bound), bound)
        strings.language_changed.subscribe(w.retranslate_ui)   # a fresh bound-method object, same target
        self.assertEqual(len(strings.language_changed), 1)
        self.assertTrue(strings.language_changed.unsubscribe(w.retranslate_ui))
        self.assertFalse(strings.language_changed.unsubscribe(w.retranslate_ui))
        strings.language_changed.emit("en")
        self.assertEqual(got, [])

    def test_decorator_form(self) -> None:
        got: list[str] = []

        @strings.language_changed.subscribe
        def on_change(lang: str) -> None:
            got.append(lang)

        strings.language_changed.emit("zh-Hans")
        self.assertEqual(got, ["zh-Hans"])

    def test_failing_subscriber_does_not_stop_others(self) -> None:
        got: list[str] = []

        def bad(lang: str) -> None:
            raise ValueError("boom")

        strings.language_changed.subscribe(bad)
        strings.language_changed.subscribe(got.append)
        with self.assertLogs("epub_reader.strings", level="ERROR"):
            with self.assertRaises(ValueError):
                strings.language_changed.emit("en")
        self.assertEqual(got, ["en"])
        strings.set_strict(False)
        with self.assertLogs("epub_reader.strings", level="ERROR"):
            strings.language_changed.emit("ja")         # logged, not raised
        self.assertEqual(got, ["en", "ja"])

    def test_deleted_qt_object_is_dropped(self) -> None:
        def dead(lang: str) -> None:
            raise RuntimeError("libshiboken: Internal C++ object (PySide6.QtWidgets.QLabel) already deleted.")

        strings.language_changed.subscribe(dead)
        strings.language_changed.emit("en")            # no raise, even in strict mode
        self.assertEqual(len(strings.language_changed), 0)


class CommandLineTest(unittest.TestCase):
    def run_py(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        return subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True,
                              text=True, encoding="utf-8", env=env, timeout=120)

    def test_check_exits_zero(self) -> None:
        proc = self.run_py("strings.py", "--check")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("OK:", proc.stdout)
        self.assertIn("简体中文", proc.stdout)

    def test_strict_translations_tracks_stubs(self) -> None:
        proc = self.run_py("strings.py", "--check", "--strict-translations")
        pending = any(strings.stub_counts().values())
        self.assertEqual(proc.returncode, 1 if pending else 0, proc.stdout[-2000:])

    def test_show(self) -> None:
        proc = self.run_py("strings.py", "--show", "menu.about")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("关于 {app}", proc.stdout)

    def test_no_qt_import(self) -> None:
        code = ("import sys, strings; strings.set_language('auto'); strings.S('tb.toc'); "
                "strings.language_choices(); print(any(m.startswith('PySide6') for m in sys.modules))")
        proc = self.run_py("-c", code)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "False")

    def test_dev_mode_import_clean(self) -> None:
        proc = self.run_py("-X", "dev", "-W", "error::SyntaxWarning", "-W", "error::DeprecationWarning",
                           "-c", "import strings, i18n")
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
