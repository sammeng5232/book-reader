"""Generic Qt WebEngine probe harness.

Usage: python harness.py <html-file> <js-file> [width] [height]
Loads the HTML in a real QWebEngineView of the given size, waits for load,
then evaluates the JS file (last expression / an explicit JSON string is
printed).  The JS should end with an expression that is JSON-serialisable.
"""
import sys, json, os
from PySide6.QtCore import QUrl, QTimer, Qt
from PySide6.QtWidgets import QApplication
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings

html_path = os.path.abspath(sys.argv[1])
js_path = os.path.abspath(sys.argv[2])
W = int(sys.argv[3]) if len(sys.argv) > 3 else 900
H = int(sys.argv[4]) if len(sys.argv) > 4 else 700

app = QApplication(sys.argv)


class Page(QWebEnginePage):
    def javaScriptConsoleMessage(self, level, msg, line, src):
        print(f"[console:{level}] {msg} ({line})", file=sys.stderr)


view = QWebEngineView()
page = Page(view)
view.setPage(page)
s = page.settings()
s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
s.setAttribute(QWebEngineSettings.WebAttribute.ShowScrollBars, False)
view.resize(W, H)
view.show()

js = open(js_path, "r", encoding="utf-8").read()
done = {"n": 0}


def emit(res):
    print("===RESULT===")
    if isinstance(res, str):
        try:
            res = json.loads(res)
        except Exception:
            pass
    try:
        print(json.dumps(res, ensure_ascii=False, indent=1))
    except Exception:
        print(repr(res))
    app.quit()


def on_load(ok):
    if not ok:
        emit({"error": "load failed"})
        return
    # give layout + fonts a beat
    QTimer.singleShot(400, lambda: page.runJavaScript(js, 0, emit))


page.loadFinished.connect(on_load)
page.load(QUrl.fromLocalFile(html_path))
QTimer.singleShot(20000, lambda: emit({"error": "timeout"}))
sys.exit(app.exec())
