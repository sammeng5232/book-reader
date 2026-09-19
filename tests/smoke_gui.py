#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GUI smoke test: launch the reader on a fixture, let it render, screenshot it.

This exists so GUI regressions can be eyeballed in seconds instead of by
clicking around. It does NOT assert what the page looks like - it proves the
app starts, opens a book, paints something non-blank, and exits.

Run it with the 3.14 interpreter::

    C:\\Users\\mengz\\AppData\\Local\\Programs\\Python\\Python314\\python.exe ^
        C:\\Users\\mengz\\book-reader\\tests\\smoke_gui.py --all

Screenshots land in tests\\out\\smoke_<fixture>.png.

Exit codes
    0  screenshot(s) taken, all non-blank
    1  the app raised while opening a book
    2  the app does not exist yet (or could not be located)
    3  a screenshot came out blank / uniform

The app does not exist yet. Until it does this script exits 2 with a message
explaining exactly what it looked for, rather than an ImportError traceback.

Notes for whoever builds the app
--------------------------------
Screenshotting is done with PySide6 only: `QWidget.grab()`, which on Qt 6.11 /
Windows DOES capture QWebEngineView content (verified). No OS screen-capture
APIs, no third-party tooling.
"""

from __future__ import annotations

import argparse
import importlib
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
for p in (str(PROJECT_ROOT), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

OUT_DIR = HERE / "out"
FIXTURES = HERE / "fixtures"

# Fixtures worth a screenshot, and why.
SMOKE_SET = [
    ("epub2_ncx.epub", "baseline EPUB 2 + NCX tree in the TOC dock"),
    ("epub3_nav.epub", "EPUB 3 nested nav"),
    ("cjk_vertical.epub", "CJK glyphs, and a vertical-rl chapter that injected CSS must not flatten"),
    ("images_fonts.epub", "images and the embedded font must load through the custom URL scheme"),
    ("odd_paths.epub", "percent-encoded and mixed-case hrefs must still resolve"),
    ("fixed_layout.epub", "pre-paginated pages must be scaled, not reflowed"),
    ("big_book.epub", "150-chapter TOC must not freeze the UI"),
    ("broken_xml.epub", "malformed chapters must still render something"),
]

# Where the app might live, in the order we try.
MODULE_CANDIDATES = [
    "main", "app", "reader",
    "epub_reader", "epubreader",
    "epub_reader.main", "epub_reader.app", "epub_reader.ui",
    "src.main", "src.app",
]
WINDOW_CLASS_NAMES = [
    "MainWindow", "ReaderWindow", "EpubReaderWindow", "EpubReaderMainWindow",
    "EpubReader", "ReaderMainWindow", "Window",
]
FACTORY_NAMES = ["create_window", "create_main_window", "build_window", "make_window"]
OPEN_METHOD_NAMES = [
    "open_book", "open_epub", "open_file", "load_book", "load_epub",
    "set_book", "open_path", "load", "open",
]

GUARD_MESSAGE = """\
smoke_gui.py: the reader application was not found, so there is nothing to
screenshot yet. This is expected until the app lands.

Looked for a QWidget subclass named any of:
    {classes}
or a factory function named any of:
    {factories}
in any of these modules (relative to {root}):
    {modules}

{detail}
Once the app exists, either name its window class one of the above or run:
    smoke_gui.py --entry <module>:<ClassOrFactory>
"""


def log(msg=""):
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# locating the app
# ---------------------------------------------------------------------------


def _import(name):
    try:
        return importlib.import_module(name), None
    except BaseException as exc:  # noqa: BLE001
        return None, exc


def locate_entry(explicit=None):
    """-> (target, 'module:Name', module) or (None, diagnostic, None).

    IMPORTANT: this runs BEFORE any QApplication exists, because
    QWebEngineUrlScheme.registerScheme() must be called before QtWebEngine is
    initialised. Importing QtWidgets is fine; instantiating QApplication is not.
    """
    from PySide6.QtWidgets import QWidget

    if explicit:
        if ":" not in explicit:
            return None, "--entry must look like module:Name, got %r" % explicit, None
        mod_name, attr = explicit.split(":", 1)
        mod, exc = _import(mod_name)
        if mod is None:
            return None, "could not import %r: %r" % (mod_name, exc), None
        target = getattr(mod, attr, None)
        if target is None:
            return None, "%s has no attribute %r" % (mod_name, attr), None
        return target, "%s:%s" % (mod_name, attr), mod

    tried = []
    for mod_name in MODULE_CANDIDATES:
        mod, exc = _import(mod_name)
        if mod is None:
            if not isinstance(exc, (ImportError, ModuleNotFoundError)):
                tried.append("  %-22s import raised %r" % (mod_name, exc))
            continue
        for cls_name in WINDOW_CLASS_NAMES:
            obj = getattr(mod, cls_name, None)
            if isinstance(obj, type) and issubclass(obj, QWidget):
                return obj, "%s:%s" % (mod_name, cls_name), mod
        for fn_name in FACTORY_NAMES:
            obj = getattr(mod, fn_name, None)
            if callable(obj):
                return obj, "%s:%s" % (mod_name, fn_name), mod
        tried.append("  %-22s imported, but exports no known window class" % mod_name)

    detail = ("What happened:\n" + "\n".join(tried) + "\n") if tried else ""
    return None, detail, None


def build_window(target, epub_path):
    """Try the plausible construction shapes, most specific first."""
    errors = []
    # 1. Window(path)
    try:
        return target(str(epub_path)), "%s(path)" % getattr(target, "__name__", target)
    except TypeError as exc:
        errors.append("  %s(path) -> TypeError: %s" % (getattr(target, "__name__", target), exc))
    # 2. Window() then .open_book(path) / etc.
    win = target()
    for name in OPEN_METHOD_NAMES:
        method = getattr(win, name, None)
        if callable(method):
            try:
                method(str(epub_path))
                return win, "%s().%s(path)" % (getattr(target, "__name__", target), name)
            except TypeError as exc:
                errors.append("  .%s(path) -> TypeError: %s" % (name, exc))
    raise RuntimeError(
        "constructed the window but could not hand it a book.\n"
        "Tried:\n" + "\n".join(errors) +
        "\nExpose one of: " + ", ".join(OPEN_METHOD_NAMES)
    )


# ---------------------------------------------------------------------------
# screenshot
# ---------------------------------------------------------------------------


def image_stats(image):
    """Coarse blankness check: how many distinct colours across a sampled grid."""
    w, h = image.width(), image.height()
    if w == 0 or h == 0:
        return 0, 0.0
    colours = set()
    total = 0
    count = 0
    step_x = max(1, w // 60)
    step_y = max(1, h // 60)
    for y in range(0, h, step_y):
        for x in range(0, w, step_x):
            rgb = image.pixel(x, y) & 0xFFFFFF
            colours.add(rgb)
            total += ((rgb >> 16) & 0xFF) + ((rgb >> 8) & 0xFF) + (rgb & 0xFF)
            count += 1
    return len(colours), (total / (3.0 * count)) if count else 0.0


def shoot(window, out_path):
    pixmap = window.grab()
    image = pixmap.toImage()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ok = pixmap.save(str(out_path), "PNG")
    colours, mean = image_stats(image)
    return ok, pixmap.width(), pixmap.height(), colours, mean


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------


def run(fixtures, entry, width, height, timeout_s, settle_ms):
    from PySide6.QtCore import QEventLoop, QTimer

    # Locate (and therefore import) the app FIRST: custom URL schemes have to be
    # registered before QtWebEngine spins up, i.e. before QApplication exists.
    target, info, mod = locate_entry(entry)
    if target is None:
        log(GUARD_MESSAGE.format(
            classes=", ".join(WINDOW_CLASS_NAMES),
            factories=", ".join(FACTORY_NAMES),
            modules=", ".join(MODULE_CANDIDATES),
            root=PROJECT_ROOT,
            detail=info or "",
        ))
        return 2

    for hook in ("register_url_schemes", "register_schemes"):
        fn = getattr(mod, hook, None)
        if callable(fn):
            log("calling %s.%s() before QApplication" % (mod.__name__, hook))
            fn()
            break

    from PySide6.QtWidgets import QApplication

    try:
        from PySide6.QtWebEngineWidgets import QWebEngineView
    except ImportError:
        QWebEngineView = None

    app = QApplication.instance() or QApplication(sys.argv[:1])

    log("app entry point: %s" % info)
    worst = 0
    for name in fixtures:
        path = FIXTURES / name
        if not path.exists():
            log("  SKIP %s (fixture missing - run make_fixtures.py)" % name)
            continue

        log("")
        log("--- %s ---" % name)
        try:
            window, how = build_window(target, path)
        except Exception:
            log(traceback.format_exc())
            worst = max(worst, 1)
            continue
        log("  constructed via %s" % how)

        window.resize(width, height)
        window.show()

        # Wait for every QWebEngineView the window owns to finish loading,
        # then let it settle so fonts and images are painted.
        loop = QEventLoop()
        pending = {"n": 0, "seen_load": False}
        views = list(window.findChildren(QWebEngineView)) if QWebEngineView else []
        if QWebEngineView is not None and isinstance(window, QWebEngineView):
            views.insert(0, window)

        def finish():
            if loop.isRunning():
                loop.quit()

        def on_load(ok):
            pending["seen_load"] = True
            QTimer.singleShot(settle_ms, finish)

        for v in views:
            v.loadFinished.connect(on_load)

        if not views:
            log("  (no QWebEngineView found yet; waiting %sms then grabbing)" % settle_ms)
            QTimer.singleShot(settle_ms, finish)
        QTimer.singleShot(int(timeout_s * 1000), finish)
        loop.exec()

        if views and not pending["seen_load"]:
            log("  WARNING: no loadFinished within %.1fs - grabbing anyway" % timeout_s)

        out = OUT_DIR / ("smoke_%s.png" % Path(name).stem)
        ok, w, h, colours, mean = shoot(window, out)
        if not ok:
            log("  ERROR: could not write %s" % out)
            worst = max(worst, 1)
        else:
            log("  wrote %s  (%dx%d, %d distinct colours, mean luminance %.0f)"
                % (out.name, w, h, colours, mean))
            if colours < 3:
                log("  BLANK: the window painted a flat image. Something did not render.")
                worst = max(worst, 3)

        window.close()
        window.deleteLater()
        app.processEvents()

    log("")
    log("screenshots in %s" % OUT_DIR)
    return worst


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fixture", action="append", default=[],
                    help="fixture filename (repeatable); default: epub3_nav.epub")
    ap.add_argument("--all", action="store_true", help="every fixture in the smoke set")
    ap.add_argument("--entry", default=None,
                    help="override app detection, e.g. main:MainWindow")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=900)
    ap.add_argument("--timeout", type=float, default=20.0,
                    help="seconds to wait for the page to load")
    ap.add_argument("--settle", type=int, default=1200,
                    help="ms to wait after load before grabbing")
    ap.add_argument("--list", action="store_true", help="list the smoke set and exit")
    args = ap.parse_args(argv)

    if args.list:
        for name, why in SMOKE_SET:
            marker = "ok " if (FIXTURES / name).exists() else "MISSING"
            print("  %-7s %-22s %s" % (marker, name, why))
        return 0

    if args.all:
        fixtures = [n for n, _ in SMOKE_SET]
    elif args.fixture:
        fixtures = args.fixture
    else:
        fixtures = ["epub3_nav.epub"]

    try:
        import PySide6  # noqa: F401
    except ImportError as exc:
        log("smoke_gui.py needs PySide6 and the Python 3.14 interpreter.\n"
            "  import PySide6 failed: %r\n"
            "  Use: %%LOCALAPPDATA%%\\Programs\\Python\\Python314\\python.exe" % exc)
        return 2

    return run(fixtures, args.entry, args.width, args.height, args.timeout, args.settle)


if __name__ == "__main__":
    raise SystemExit(main())
