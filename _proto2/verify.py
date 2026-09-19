"""End-to-end verification of reader.js + screenshots."""
import sys, json, os
from PySide6.QtCore import QUrl, QTimer
from PySide6.QtWidgets import QApplication
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings

HERE = os.path.dirname(os.path.abspath(__file__))
html = os.path.join(HERE, sys.argv[1] if len(sys.argv) > 1 else "book.html")
READER = open(os.path.join(HERE, "reader.js"), encoding="utf-8").read()
app = QApplication(sys.argv)

class Page(QWebEnginePage):
    def javaScriptConsoleMessage(self, l, m, line, s):
        print(f"[console] {m} ({line})", file=sys.stderr)

view = QWebEngineView(); page = Page(view); view.setPage(page)
page.settings().setAttribute(QWebEngineSettings.WebAttribute.ShowScrollBars, False)
view.resize(900, 700); view.show()
out = {}
steps = []

def shot(name):
    def go(cb):
        def after(v):
            img = view.grab()
            p2 = os.path.join(HERE, "shot_%s.png" % name)
            img.save(p2)
            out.setdefault("shots", []).append({"f": p2, "state": v})
            cb()
        page.runJavaScript("JSON.stringify({sl:document.documentElement.scrollLeft,"
                           "page:RDR.page(),pages:RDR.pages(),sw:document.documentElement.scrollWidth})", 0, after)
        return
    return go

def js(code, key=None):
    def go(cb):
        def done(v):
            if key:
                try: v = json.loads(v) if isinstance(v, str) and v[:1] in '[{"' else v
                except Exception: pass
                out[key] = v
            cb()
        page.runJavaScript(code, 0, done)
    return go

def wait(ms):
    def go(cb): QTimer.singleShot(ms, cb)
    return go

def run(i=0):
    if i >= len(steps):
        print("===RESULT==="); print(json.dumps(out, ensure_ascii=False, indent=1)); app.quit(); return
    steps[i](lambda: QTimer.singleShot(60, lambda: run(i + 1)))

def on_load(ok):
    if not ok: print("load failed"); app.quit(); return
    steps.extend([
        wait(400),
        js(READER),
        js("JSON.stringify(RDR.init({theme:{name:'light',font:'serif',fontSize:19,lineHeight:1.78,gap:72,maxWidth:0}}))", "init"),
        wait(200), shot("light_p0"),
        js("JSON.stringify(RDR.goto(2))", "goto2"), wait(150), shot("light_p2"),
        js("JSON.stringify(RDR.capture())", "cap"),
        js("JSON.stringify(RDR.setTheme({name:'dark',font:'serif',fontSize:19}))", "dark"),
        wait(250), shot("dark"),
        js("JSON.stringify({bodyColor:getComputedStyle(document.body).color,"
           "htmlBg:getComputedStyle(document.documentElement).backgroundColor,"
           "preBg:getComputedStyle(document.getElementById('pre1')).backgroundColor,"
           "italic:getComputedStyle(document.getElementById('poem1')).fontStyle,"
           "sc:getComputedStyle(document.getElementById('sc1')).fontVariantCaps,"
           "indent:getComputedStyle(document.getElementById('p1')).textIndent,"
           "pages:RDR.pages()})", "darkCheck"),
        js("JSON.stringify(RDR.setTheme({name:'sepia',font:'songti',fontSize:22,lineHeight:1.9}))", "sepia"),
        wait(250), shot("sepia_cjk"),
        js("JSON.stringify({found:RDR.find('江月'), step:RDR.findStep(1)})", "find"),
        wait(200), shot("find"),
        js("RDR.clearFind(); JSON.stringify(RDR.setTheme({name:'light',font:'serif',fontSize:19,lineHeight:1.78}))", "back"),
        js("(function(){var a=document.getElementById('nr1');"
           "var ev=new PointerEvent('pointerdown',{clientX:10,clientY:10,pointerId:1,bubbles:true});"
           "return JSON.stringify(RDR.capture());})()", "cap2"),
        js("RDR.goto(4); 1"), wait(150),
        js("(function(){const a=document.getElementById('nr1');"
           "const f=window.__showNoteTest;"
           "const ev=new MouseEvent('click');"
           "/* drive the internal path via a synthetic pointerup */"
           "const r=a.getBoundingClientRect();"
           "document.dispatchEvent(new PointerEvent('pointerdown',{pointerId:9,clientX:r.left+2,clientY:r.top+2,bubbles:true}));"
           "a.dispatchEvent(new PointerEvent('pointerup',{pointerId:9,clientX:r.left+2,clientY:r.top+2,bubbles:true}));"
           "return JSON.stringify({pop:!!document.getElementById('__rdr-pop'),"
           "rect:(document.getElementById('__rdr-pop')||{getBoundingClientRect:()=>({})}).getBoundingClientRect()});})()", "note"),
        wait(200), shot("footnote"),
        js("JSON.stringify(RDR.progress())", "prog"),
    ])
    run()

page.loadFinished.connect(on_load)
page.load(QUrl.fromLocalFile(html))
QTimer.singleShot(60000, app.quit)
sys.exit(app.exec())
