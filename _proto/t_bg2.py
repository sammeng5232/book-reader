"""(A) Prove/disprove QWebEnginePage.setBackgroundColor by grabbing pixels three
different ways from a RAISED window.
(B) Compare Paint/Resource Timing between text/html and application/xhtml+xml.
"""
import io
import json
import os
import sys

os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "")

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPoint, QTimer, QUrl, Qt
from PySide6.QtGui import QColor
from PySide6.QtWebEngineCore import (
    QWebEnginePage,
    QWebEngineProfile,
    QWebEngineScript,
    QWebEngineUrlScheme,
    QWebEngineUrlSchemeHandler,
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication

SCHEME = b"tst2"
s = QWebEngineUrlScheme(SCHEME)
s.setSyntax(QWebEngineUrlScheme.Syntax.Host)
F = QWebEngineUrlScheme.Flag
s.setFlags(F.SecureScheme | F.LocalScheme | F.LocalAccessAllowed | F.CorsEnabled | F.FetchApiAllowed)
QWebEngineUrlScheme.registerScheme(s)

HTML = (b"<!DOCTYPE html><html><head><meta charset='utf-8'><title>h</title>"
        b"<link rel='stylesheet' href='/s.css'/></head>"
        b"<body><p>html doc</p><img src='/img.png'/></body></html>")

XHTML = (b"<?xml version='1.0' encoding='utf-8'?>\n"
         b"<!DOCTYPE html>\n"
         b"<html xmlns='http://www.w3.org/1999/xhtml'><head><meta charset='utf-8'/>"
         b"<title>x</title><link rel='stylesheet' type='text/css' href='/s.css'/></head>"
         b"<body><p>xhtml doc</p><img src='/img.png'/></body></html>")

CSS = b"html,body{margin:0;padding:0;background:transparent} p{color:#888}"
PNG = b""


class H(QWebEngineUrlSchemeHandler):
    def requestStarted(self, job):
        p = job.requestUrl().path().lstrip("/")
        table = {
            "index.html": (HTML, b"text/html"),
            "index.xhtml": (XHTML, b"application/xhtml+xml"),
            "s.css": (CSS, b"text/css"),
            "img.png": (PNG, b"image/png"),
        }
        data, mime = table.get(p, (b"missing", b"text/plain"))
        b = QBuffer(job)
        b.setData(QByteArray(data))
        b.open(QIODevice.OpenModeFlag.ReadOnly)
        job.reply(mime, b)


TIMING_JS = (
    "JSON.stringify({"
    " ct: document.contentType,"
    " paint: performance.getEntriesByType('paint').map(e=>[e.name, Math.round(e.startTime)]),"
    " resource: performance.getEntriesByType('resource').map(e=>e.name),"
    " readyState: document.readyState })"
)


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

    page = QWebEnginePage(prof)
    view = QWebEngineView()
    view.setPage(page)
    view.setGeometry(60, 60, 520, 420)
    view.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
    view.show()
    view.raise_()
    view.activateWindow()

    R = {}
    steps = []

    def nxt():
        if steps:
            QTimer.singleShot(0, steps.pop(0))
        else:
            print(json.dumps(R, indent=2))
            app.quit()

    def sample(tag):
        out = {}
        # 1. widget grab
        pm = view.grab()
        out["view.grab"] = "NULL" if pm.isNull() else QColor(
            pm.toImage().pixel(pm.width() // 2, pm.height() - 30)).name()
        # 2. grabWindow(winId)
        pm2 = app.primaryScreen().grabWindow(int(view.winId()))
        out["grabWindow(winId)"] = "NULL" if pm2.isNull() else QColor(
            pm2.toImage().pixel(pm2.width() // 2, pm2.height() - 30)).name()
        # 3. whole-desktop grab, cropped to the view's global rect
        g = view.mapToGlobal(QPoint(0, 0))
        dpr = view.devicePixelRatioF()
        pm3 = app.primaryScreen().grabWindow(0)
        if not pm3.isNull():
            im = pm3.toImage()
            x = int((g.x() + view.width() // 2) * dpr)
            y = int((g.y() + view.height() - 30) * dpr)
            out["desktopGrab"] = (QColor(im.pixel(x, y)).name()
                                  if 0 <= x < im.width() and 0 <= y < im.height()
                                  else f"OOB {x},{y} of {im.width()}x{im.height()}")
        else:
            out["desktopGrab"] = "NULL"
        R[tag] = out

    def load(url, cb, delay=700):
        def on(ok):
            page.loadFinished.disconnect(on)
            QTimer.singleShot(delay, lambda: cb(ok))
        page.loadFinished.connect(on)
        page.load(QUrl(url))

    def s1():
        page.setBackgroundColor(QColor("#ff00ff"))
        R["expect1"] = "#ff00ff"
        load("tst2://h/index.html", lambda ok: (sample("A1.magenta"), nxt()))

    def s2():
        page.setBackgroundColor(QColor("#008080"))
        R["expect2"] = "#008080"
        QTimer.singleShot(900, lambda: (sample("A2.tealNoReload"), nxt()))

    def s3():
        load("tst2://h/index.html", lambda ok: (sample("A3.tealAfterReload"), nxt()))

    def s4():
        page.setBackgroundColor(QColor(Qt.GlobalColor.transparent))
        R["expect4"] = "transparent"
        load("tst2://h/index.html", lambda ok: (sample("A4.transparent"), nxt()))

    def b_html():
        page.setBackgroundColor(QColor("#101010"))
        def after(ok):
            page.runJavaScript(TIMING_JS, QWebEngineScript.ScriptWorldId.ApplicationWorld,
                               lambda v: (R.__setitem__("B.html.app", json.loads(v) if v else None), nxt()))
        load("tst2://h/index.html", after, delay=900)

    def b_xhtml():
        def after(ok):
            page.runJavaScript(TIMING_JS, QWebEngineScript.ScriptWorldId.ApplicationWorld,
                               lambda v: (R.__setitem__("B.xhtml.app", json.loads(v) if v else None), nxt()))
        load("tst2://h/index.xhtml", after, delay=900)

    def b_xhtml_main():
        page.runJavaScript(TIMING_JS, QWebEngineScript.ScriptWorldId.MainWorld,
                           lambda v: (R.__setitem__("B.xhtml.main", json.loads(v) if v else None), nxt()))

    steps.extend([s1, s2, s3, s4, b_html, b_xhtml, b_xhtml_main])
    QTimer.singleShot(1200, nxt)
    QTimer.singleShot(25000, lambda: (print(json.dumps(R, indent=2)), app.quit()))
    return app.exec()


sys.exit(main())
