"""Isolate two questions:
  (A) Where must setBackgroundColor be called for it to actually paint?
  (B) Are Paint Timing / Resource Timing entries available on a custom scheme,
      and does the script world matter?
"""
import io
import json
import os
import sys
import time
import zipfile

os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--js-flags=--expose-gc")

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QTimer, QUrl
from PySide6.QtGui import QColor
from PySide6.QtWebEngineCore import (
    QWebEnginePage,
    QWebEngineProfile,
    QWebEngineScript,
    QWebEngineUrlRequestJob,
    QWebEngineUrlScheme,
    QWebEngineUrlSchemeHandler,
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication

SCHEME = b"tst"
s = QWebEngineUrlScheme(SCHEME)
s.setSyntax(QWebEngineUrlScheme.Syntax.Host)
F = QWebEngineUrlScheme.Flag
s.setFlags(F.SecureScheme | F.LocalScheme | F.LocalAccessAllowed | F.CorsEnabled | F.FetchApiAllowed)
QWebEngineUrlScheme.registerScheme(s)

PAGE = (
    b"<!DOCTYPE html><html><head><meta charset='utf-8'><title>t</title>"
    b"<style>html,body{margin:0;padding:0;background:transparent}</style></head>"
    b"<body><p style='color:#888'>content</p>"
    b"<img src='/img.png'/></body></html>"
)
PNG = None


class H(QWebEngineUrlSchemeHandler):
    def requestStarted(self, job):
        p = job.requestUrl().path().lstrip("/")
        data = PNG if p == "img.png" else PAGE
        mime = b"image/png" if p == "img.png" else b"text/html"
        b = QBuffer(job)
        b.setData(QByteArray(data))
        b.open(QIODevice.OpenModeFlag.ReadOnly)
        job.reply(mime, b)


def main():
    global PNG
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (40, 40), (0, 255, 0)).save(buf, format="PNG")
    PNG = buf.getvalue()

    app = QApplication(sys.argv)
    prof = QWebEngineProfile()
    h = H()
    prof.installUrlSchemeHandler(SCHEME, h)

    MAGENTA = "#ff00ff"
    results = {}

    page = QWebEnginePage(prof)
    page.setBackgroundColor(QColor(MAGENTA))     # (1) set BEFORE the view exists
    view = QWebEngineView()
    view.resize(500, 400)
    view.setPage(page)                            # does setPage clobber it?
    results["afterSetPage.page.backgroundColor"] = page.backgroundColor().name()
    view.show()

    def grab(tag):
        pm = app.primaryScreen().grabWindow(int(view.winId()))
        if pm.isNull():
            return "NULL"
        img = pm.toImage()
        return QColor(img.pixel(img.width() // 2, img.height() - 30)).name()

    steps = []

    def nxt():
        if steps:
            QTimer.singleShot(0, steps.pop(0))
        else:
            print(json.dumps(results, indent=2))
            app.quit()

    def load_then(cb):
        def on(ok):
            page.loadFinished.disconnect(on)
            QTimer.singleShot(500, lambda: cb(ok))
        page.loadFinished.connect(on)
        page.load(QUrl("tst://h/index.html"))

    # -- A1: background set before view existed -------------------------
    def a1():
        def after(ok):
            results["A1.beforeView.pixel"] = grab("a1")
            nxt()
        load_then(after)

    # -- A2: re-set after load, then wait and grab -----------------------
    def a2():
        page.setBackgroundColor(QColor(MAGENTA))
        QTimer.singleShot(600, lambda: (results.__setitem__("A2.reSetAfterLoad.pixel", grab("a2")), nxt()))

    # -- A3: re-set + force a reload -------------------------------------
    def a3():
        page.setBackgroundColor(QColor("#008080"))
        def after(ok):
            results["A3.setThenReload.pixel"] = grab("a3")
            results["A3.expected"] = "#008080"
            nxt()
        load_then(after)

    # -- A4: what does the view's own palette do? ------------------------
    def a4():
        results["view.hasSetBackgroundColor"] = hasattr(view, "setBackgroundColor")
        results["page.backgroundColor.now"] = page.backgroundColor().name()
        nxt()

    # -- B: timing APIs, both worlds --------------------------------------
    def b():
        pend = {"n": 0}
        def ask(world, label):
            pend["n"] += 1
            def cb(v):
                results[label] = json.loads(v) if v else None
                pend["n"] -= 1
                if pend["n"] == 0:
                    nxt()
            page.runJavaScript(
                "JSON.stringify({"
                " paint: performance.getEntriesByType('paint').map(e=>[e.name, Math.round(e.startTime)]),"
                " resource: performance.getEntriesByType('resource').map(e=>e.name),"
                " nav: (performance.getEntriesByType('navigation')[0]||{}).loadEventEnd,"
                " hasObs: typeof PerformanceObserver,"
                " supported: (PerformanceObserver.supportedEntryTypes||[]).slice(0),"
                " isSecure: window.isSecureContext,"
                " origin: location.origin })",
                world, cb)
        ask(QWebEngineScript.ScriptWorldId.MainWorld, "B.mainWorld")
        ask(QWebEngineScript.ScriptWorldId.ApplicationWorld, "B.appWorld")

    steps.extend([a1, a2, a3, a4, b])
    QTimer.singleShot(400, nxt)
    QTimer.singleShot(14000, lambda: (print(json.dumps(results, indent=2)), app.quit()))
    return app.exec()


sys.exit(main())
