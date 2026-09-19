#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Verification of the application entry point (owner G): ``epub_reader.py``.

Every GUI check launches the REAL app in a child process
(``tests/app_driver.py`` -> ``epub_reader.main``) with APPDATA and LOCALAPPDATA
pointed at temporary folders, so the user's ``%APPDATA%\\Book Reader`` is never
read or written.  Windows appear briefly on screen and every child quits
through the app's own quit path (a watchdog kills a stuck one).  The user's
three books are opened read-only in place.  Screenshots go to ``tests\\out\\app``.

    C:\\Users\\mengz\\AppData\\Local\\Programs\\Python\\Python314\\python.exe tests\\test_app.py
    python tests\\test_app.py --only launch,restart        # a subset
    python -m unittest tests.test_app                        # under unittest

Set ``EPUB_READER_SKIP_GUI_TESTS=1`` to run only the non-GUI checks.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import site
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

OUT = os.path.join(HERE, "out", "app")
DRIVER = os.path.join(HERE, "app_driver.py")
ENTRY = os.path.join(ROOT, "epub_reader.py")
FIXTURES = os.path.join(HERE, "fixtures")
PIPE_PATH = r"\\.\pipe\book-reader-single-instance"
USER_BASE = os.environ.get("PYTHONUSERBASE") or site.getuserbase()
SKIP_GUI = os.environ.get("EPUB_READER_SKIP_GUI_TESTS", "") == "1"


class Sandbox:
    """A throwaway APPDATA + LOCALAPPDATA pair (the app's whole state root)."""

    def __init__(self, name: str) -> None:
        self.dir = tempfile.mkdtemp(prefix=f"er-app-{name}-")
        self.appdata = os.path.join(self.dir, "Roaming")
        self.local = os.path.join(self.dir, "Local")
        os.makedirs(self.appdata)
        os.makedirs(self.local)

    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update({
            "APPDATA": self.appdata,
            "LOCALAPPDATA": self.local,
            "PYTHONUSERBASE": USER_BASE,          # PySide6 lives in the real user site
            "PYTHONPATH": ROOT,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        })
        env.pop("EPUB_READER_DEBUG", None)
        return env

    @property
    def state_root(self) -> str:
        return os.path.join(self.appdata, "Book Reader")

    def log_text(self) -> str:
        path = os.path.join(self.state_root, "logs", "book-reader.log")
        try:
            return open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            return ""

    def cleanup(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


def pipe_busy() -> bool:
    """True when some Book Reader (possibly the user's own) is listening already."""
    try:
        return os.path.exists(PIPE_PATH)
    except OSError:
        return False


def run_scenario(name: str, box: Sandbox, app_args: list[str] | None = None, extra: list[str] | None = None,
                 timeout: float = 260.0) -> dict:
    """Run one driver scenario in a child; returns its result dict (+ rc, seconds, output)."""
    os.makedirs(OUT, exist_ok=True)
    result = os.path.join(box.dir, f"{name}.json")
    cmd = [sys.executable, DRIVER, name, result, OUT, *(extra or []), "--", *(app_args or [])]
    t0 = time.monotonic()
    try:
        proc = subprocess.run(cmd, env=box.env(), cwd=ROOT, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
        rc, out = proc.returncode, proc.stdout + proc.stderr
    except subprocess.TimeoutExpired as exc:
        rc, out = -999, f"TIMEOUT after {timeout}s\n{exc.stdout or ''}{exc.stderr or ''}"
    try:
        data = json.load(open(result, encoding="utf-8"))
    except (OSError, ValueError):
        data = {"scenario": name, "rows": [["result file written", False, out[-1500:]]], "data": {}}
    data["rc"] = rc
    data["seconds"] = round(time.monotonic() - t0, 1)
    data["output"] = out
    data["result_path"] = result
    return data


def log_problems(text: str, allow: tuple[str, ...] = ()) -> list[str]:
    bad = []
    for line in text.splitlines():
        if (" ERROR " in line or " CRITICAL " in line or "Traceback" in line) \
                and not any(a in line for a in allow):
            bad.append(line.strip()[:200])
    return bad


# ==========================================================================
# unittest
# ==========================================================================

class AppNoGui(unittest.TestCase):
    """Checks that need no window."""

    def test_help_prints_usage_and_writes_nothing(self) -> None:
        box = Sandbox("help")
        try:
            proc = subprocess.run([sys.executable, ENTRY, "--help"], env=box.env(), capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("epub_reader.py", proc.stdout)
            self.assertIn("--help", proc.stdout)
            self.assertFalse(os.path.exists(box.state_root), "--help must not create the state root")
        finally:
            box.cleanup()

    def test_parse_args(self) -> None:
        import epub_reader as er

        o = er.parse_args(["C:\\书\\a b.epub"])
        self.assertEqual((o.path, o.help), ("C:\\书\\a b.epub", False))
        self.assertTrue(er.parse_args(["--help"]).help)
        self.assertTrue(er.parse_args(["-h"]).help)
        self.assertEqual(er.parse_args(["--debug", "x.epub", "y"]).extra, ("y",))

    def test_parse_combo(self) -> None:
        import epub_reader as er
        from PySide6.QtCore import Qt

        def iv(x):
            return int(getattr(x, "value", x))

        C, S, A = iv(Qt.KeyboardModifier.ControlModifier), iv(Qt.KeyboardModifier.ShiftModifier), \
            iv(Qt.KeyboardModifier.AltModifier)
        self.assertEqual(er.parse_combo("Ctrl+→"), (iv(Qt.Key.Key_Right), C))
        self.assertEqual(er.parse_combo("N"), (iv(Qt.Key.Key_N), S))
        self.assertEqual(er.parse_combo("n"), (iv(Qt.Key.Key_N), 0))
        self.assertEqual(er.parse_combo("Shift+Space"), (iv(Qt.Key.Key_Space), S))
        self.assertEqual(er.parse_combo("Ctrl+-"), (iv(Qt.Key.Key_Minus), C))
        self.assertEqual(er.parse_combo("Ctrl+,"), (iv(Qt.Key.Key_Comma), C))
        self.assertEqual(er.parse_combo("Alt+←"), (iv(Qt.Key.Key_Left), A))
        self.assertEqual(er.parse_combo("Ctrl+Shift+C"), (iv(Qt.Key.Key_C), C | S))
        self.assertEqual(er.parse_combo("Esc"), (iv(Qt.Key.Key_Escape), 0))
        self.assertEqual(er._page_desc(*er.parse_combo("N")), "Shift+N")
        self.assertEqual(er._page_desc(*er.parse_combo("j")), "J")
        self.assertEqual(er._page_desc(*er.parse_combo("Shift+F3")), "Shift+F3")

    def test_key_map_is_complete_and_conflict_free(self) -> None:
        import epub_reader as er
        import reader_page
        from strings import KEYS

        # a stand-in action map with the real ids (the slots' identity is what matters)
        fake = {kid: [(lambda k=kid, i=i: (k, i)) for i in range(len(names))] if len(names) > 1
                else [(lambda k=kid: k)] for kid, names in reader_page.ACTION_SLOTS.items()}
        shell = {k: (lambda k=k: k) for k in er.GLOBAL_ACTIONS}
        bindings = er.build_bindings(fake, shell)
        self.assertEqual(er.audit_bindings(bindings), [])
        kids = {b.kid for b in bindings}
        for kid in KEYS:
            if kid in er.TOOLTIP_ONLY or kid in er.SHELL_OWNED_LIBRARY_IDS:
                continue
            self.assertIn(kid, kids)
        # the audit really catches a duplicate
        dup = list(bindings) + [er.Binding("x", 0, "Ctrl+T", *er.parse_combo("Ctrl+T"), "reader", "chord",
                                           "shell", lambda: None, True, "")]
        self.assertTrue(any("Ctrl+T" in p for p in er.audit_bindings(dup)))
        # gating classes
        kind = {(b.kid, b.label): b.kind for b in bindings}
        self.assertEqual(kind[("page_jk", "j")], "letter")
        self.assertEqual(kind[("find_prev", "N")], "letter")
        self.assertEqual(kind[("search", "/")], "letter")
        self.assertEqual(kind[("next_page", "Space")], "page")
        self.assertEqual(kind[("copy", "Ctrl+C")], "chord")
        self.assertEqual(kind[("escape", "Esc")], "escape")

    def test_single_instance_pipe_name(self) -> None:
        import store

        self.assertEqual(store.PIPE_NAME, "book-reader-single-instance")


@unittest.skipIf(SKIP_GUI, "EPUB_READER_SKIP_GUI_TESTS=1")
class AppGui(unittest.TestCase):
    """The real app, one child process per scenario."""

    def setUp(self) -> None:
        if pipe_busy():
            self.skipTest("another Book Reader is running (its pipe exists); not touching it")

    def assertRows(self, res: dict) -> None:  # noqa: N802
        failed = [f"{n}: {d}" for n, ok, d in res.get("rows", []) if not ok]
        self.assertTrue(res.get("rows"), res.get("output", "")[-2000:])
        self.assertFalse(failed, "\n".join(failed) + "\n" + res.get("output", "")[-1500:])

    def _run(self, name: str, box: Sandbox, **kw) -> dict:
        res = run_scenario(name, box, **kw)
        print(f"\n  --- {name}: rc={res['rc']} in {res['seconds']}s", flush=True)
        for n, ok, d in res.get("rows", []):
            print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f" -- {d[:220]}" if d else ""), flush=True)
        self.assertRows(res)
        self.assertEqual(res["rc"], 0, res["output"][-2000:])
        return res

    def test_a_launch(self) -> None:
        box = Sandbox("launch")
        try:
            self._run("launch", box)
            self.assertEqual(log_problems(box.log_text()), [])
            self.assertFalse(pipe_busy(), "the pipe must go away when the app exits")
        finally:
            box.cleanup()

    def test_b_fixtures(self) -> None:
        box = Sandbox("fixtures")
        try:
            self._run("fixtures", box)
            self.assertEqual(log_problems(box.log_text()), [])
        finally:
            box.cleanup()

    def test_c_real_books(self) -> None:
        box = Sandbox("real")
        try:
            self._run("real", box)
            self.assertEqual(log_problems(box.log_text()), [])
        finally:
            box.cleanup()

    def test_c2_other_formats(self) -> None:
        """MOBI / AZW3 (Project Gutenberg) and DjVu (Internet Archive) samples in the real app."""
        box = Sandbox("formats")
        try:
            self._run("formats", box)
            self.assertEqual(log_problems(box.log_text()), [])
        finally:
            box.cleanup()

    def test_d_features(self) -> None:
        box = Sandbox("features")
        try:
            self._run("features", box)
            self.assertEqual(log_problems(box.log_text()), [])
        finally:
            box.cleanup()

    def test_e_position_across_restart(self) -> None:
        box = Sandbox("restart")
        book = r"C:\Users\mengz\Desktop\文件\Econ Books\50人的二十年_樊纲 易纲等.epub"
        if not os.path.exists(book):
            self.skipTest("real book not present")
        try:
            a = self._run("restart_a", box, app_args=[book])
            self._run("restart_b", box, extra=[a["result_path"]])
            self.assertEqual(log_problems(box.log_text()), [])
        finally:
            box.cleanup()

    def test_f_language(self) -> None:
        box = Sandbox("lang")
        try:
            self._run("lang", box)
            self._run("lang_check", box)
            self.assertEqual(log_problems(box.log_text()), [])
        finally:
            box.cleanup()

    def test_g_single_instance(self) -> None:
        box = Sandbox("single")
        other = os.path.join(FIXTURES, "epub2_ncx.epub")
        marker = os.path.join(box.dir, "primary.ready")
        result = os.path.join(box.dir, "single_primary.json")
        cmd = [sys.executable, DRIVER, "single_primary", result, OUT, marker, other, "--"]
        primary = subprocess.Popen(cmd, env=box.env(), cwd=ROOT, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        try:
            end = time.monotonic() + 60
            while not os.path.exists(marker) and time.monotonic() < end and primary.poll() is None:
                time.sleep(0.1)
            self.assertTrue(os.path.exists(marker), "primary never became ready")
            t0 = time.monotonic()
            second = subprocess.run([sys.executable, ENTRY, other], env=box.env(), cwd=ROOT,
                                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                                    timeout=60)
            took = time.monotonic() - t0
            self.assertEqual(second.returncode, 0, second.stderr[-1500:])
            self.assertLess(took, 20, f"second launch took {took:.1f}s")
            out, _ = primary.communicate(timeout=120)
            res = json.load(open(result, encoding="utf-8"))
            res["output"] = out
            self.assertRows(res)
            self.assertEqual(primary.returncode, 0, out[-1500:])
            text = box.log_text()
            self.assertIn("handed over to the running instance", text)
            self.assertIn("second launch: OPEN", text)
            self.assertEqual(log_problems(text), [])
            print(f"  second launch exited rc=0 in {took:.2f}s", flush=True)
        finally:
            if primary.poll() is None:
                primary.kill()
            box.cleanup()

    def test_h_crash_card(self) -> None:
        box = Sandbox("crash")
        try:
            self._run("crash", box)
            text = box.log_text()
            self.assertIn("deliberate test failure (sc_crash)", text)
            self.assertIn("unhandled exception", text)
        finally:
            box.cleanup()

    def test_i_screenshots(self) -> None:
        box = Sandbox("shots")
        try:
            res = self._run("shots", box)
            d = res["data"]
            print("  chrome/page colours:", {k: v for k, v in d.items() if k.startswith(("chrome_", "page_"))},
                  flush=True)
            self.assertEqual(log_problems(box.log_text()), [])
        finally:
            box.cleanup()


# ==========================================================================
# command line: a PASS/FAIL table
# ==========================================================================

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default="", help="comma list of test name fragments (launch, fixtures, ...)")
    args = ap.parse_args(argv)
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in (AppNoGui, AppGui):
        for name in loader.getTestCaseNames(cls):
            if args.only and not any(f in name for f in args.only.split(",")):
                continue
            suite.addTest(cls(name))
    res = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if res.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
