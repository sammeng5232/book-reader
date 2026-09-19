"""Bisect: why does performance.getEntriesByType('resource') come back empty in
proto.py but not in the minimal tests? Toggle one factor at a time."""
import json
import os
import sys

# choose flags BEFORE importing proto (which uses setdefault)
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = os.environ.get("FLAGS_OVERRIDE", "")

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtWebEngineCore import (
    QWebEnginePage,
    QWebEngineProfile,
    QWebEngineScript,
    QWebEngineSettings,
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication

import proto as P  # registers the epub:// scheme at import time

URL = f"epub://{P.HOST}/OEBPS/text/ch1.xhtml"
JS = (
    "JSON.stringify({"
    " res: performance.getEntriesByType('resource').map(e=>e.name.split('/').pop()),"
    " allTypes: performance.getEntries().map(e=>e.entryType),"
    " bufSize: (performance.setResourceTimingBufferSize ? 'has-api' : 'no-api'),"
    " paint: performance.getEntriesByType('paint').length,"
    " ct: document.contentType })"
)


def build(case, book):
    prof = QWebEngineProfile()
    h = P.ZipSchemeHandler(book)
    h.setParent(prof)
    prof.installUrlSchemeHandler(P.SCHEME, h)

    if case in ("scripts", "full"):
        boot = P.qwebchannel_js() + "\n" + P.BOOT_JS.replace(
            "__THEME_CSS__", json.dumps(P.THEME_CSS))
        prof.scripts().insert(P.make_script(
            "b", boot, QWebEngineScript.InjectionPoint.DocumentCreation))
        prof.scripts().insert(P.make_script(
            "p", P.PROBE_JS, QWebEngineScript.InjectionPoint.DocumentReady))

    if case == "full":
        A = QWebEngineSettings.WebAttribute
        st = prof.settings()
        st.setAttribute(A.JavascriptEnabled, True)
        st.setAttribute(A.LocalContentCanAccessFileUrls, False)
        st.setAttribute(A.LocalContentCanAccessRemoteUrls, False)
        st.setAttribute(A.LocalStorageEnabled, True)
        st.setAttribute(A.PdfViewerEnabled, False)
        st.setAttribute(A.PluginsEnabled, False)
        st.setAttribute(A.ShowScrollBars, False)
        st.setAttribute(A.FocusOnNavigationEnabled, True)
        st.setAttribute(A.ErrorPageEnabled, False)
        st.setAttribute(A.PlaybackRequiresUserGesture, True)
        st.setAttribute(A.JavascriptCanOpenWindows, False)
        st.setAttribute(A.LinksIncludedInFocusChain, True)
        st.setAttribute(A.ScrollAnimatorEnabled, False)
        st.setAttribute(A.WebGLEnabled, False)
        st.setAttribute(A.PrintElementBackgrounds, True)

    page = QWebEnginePage(prof)
    return prof, h, page


def main():
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "proto_book.epub"), "rb") as fh:
        book = fh.read()

    app = QApplication(sys.argv)
    view = QWebEngineView()
    view.setGeometry(70, 70, 500, 400)
    view.show()
    view.raise_()

    keep = []
    R = {}
    cases = ["bare", "scripts", "full", "bare-headers-off"]

    def nxt():
        if not cases:
            print(json.dumps(R, indent=2))
            app.quit()
            return
        case = cases.pop(0)
        if case == "bare-headers-off":
            P.ZipSchemeHandler.SKIP_HEADERS = True
            case_build = "bare"
        else:
            P.ZipSchemeHandler.SKIP_HEADERS = False
            case_build = case
        prof, h, page = build(case_build, book)
        keep.append((prof, h, page))
        view.setPage(page)

        loads = [0]

        def on(ok):
            loads[0] += 1
            if loads[0] < 3:                      # load 3x: cold, warm, warm
                QTimer.singleShot(150, lambda: page.load(QUrl(URL)))
                return
            page.loadFinished.disconnect(on)
            QTimer.singleShot(600, lambda: page.runJavaScript(
                JS, QWebEngineScript.ScriptWorldId.MainWorld,
                lambda v: (R.__setitem__(case, json.loads(v) if v else None), nxt())))

        page.loadFinished.connect(on)
        page.load(QUrl(URL))

    QTimer.singleShot(400, nxt)
    QTimer.singleShot(40000, lambda: (print(json.dumps(R, indent=2)), app.quit()))
    return app.exec()


sys.exit(main())
