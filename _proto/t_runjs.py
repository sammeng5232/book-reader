"""Isolate: what does runJavaScript's result callback actually receive for each JS type,
in MainWorld vs ApplicationWorld, with and without an explicit worldId argument?"""
import sys
import time

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineScript
from PySide6.QtWidgets import QApplication

MAIN = QWebEngineScript.ScriptWorldId.MainWorld
APP = QWebEngineScript.ScriptWorldId.ApplicationWorld

CASES = [
    ("number", "42"),
    ("string", "'hi'"),
    ("bool", "true"),
    ("null", "null"),
    ("undefined", "undefined"),
    ("array", "[1, 'two', true]"),
    ("array_nums", "[1,2,3]"),
    ("array_str", "['a','b']"),
    ("object", "({a: 1, b: 'x'})"),
    ("nested", "({a: [1,2], b: {c: 3}})"),
    ("json_str", "JSON.stringify([1,'two',true])"),
    ("date", "new Date(0)"),
    ("domnode", "document.body"),
    ("func", "(function(){})"),
    ("promise", "Promise.resolve(7)"),
    ("throws", "(function(){ throw new Error('boom'); })()"),
]


def main():
    app = QApplication(sys.argv)
    prof = QWebEngineProfile()
    page = QWebEnginePage(prof)
    page.setHtml("<html><body>x</body></html>")

    results = []
    pending = []

    def run_all():
        for world_name, world in (("MAIN", MAIN), ("APP", APP)):
            for name, src in CASES:
                pending.append((world_name, name))

                def cb(v, wn=world_name, n=name):
                    results.append((wn, n, type(v).__name__, repr(v)[:70]))
                    try:
                        pending.remove((wn, n))
                    except ValueError:
                        pass

                page.runJavaScript(src, world, cb)

        # 2-arg overload (no worldId) -> implicitly MainWorld
        for name, src in [("array", "[1,'two',true]"), ("object", "({a:1})")]:
            pending.append(("2ARG", name))

            def cb2(v, n=name):
                results.append(("2ARG", n, type(v).__name__, repr(v)[:70]))
                try:
                    pending.remove(("2ARG", n))
                except ValueError:
                    pass

            page.runJavaScript(src, cb2)

        QTimer.singleShot(2500, report)

    def report():
        print(f"{'WORLD':<6} {'CASE':<12} {'PYTYPE':<12} VALUE")
        print("-" * 80)
        for w, n, t, v in results:
            print(f"{w:<6} {n:<12} {t:<12} {v}")
        if pending:
            print("\nCALLBACK NEVER FIRED FOR:", pending)
        app.quit()

    QTimer.singleShot(300, run_all)
    return app.exec()


sys.exit(main())
