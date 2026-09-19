import sys, json, os
from PySide6.QtCore import QUrl, QTimer
from PySide6.QtWidgets import QApplication
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
HERE=os.path.dirname(os.path.abspath(__file__))
READER=open(os.path.join(HERE,"reader.js"),encoding="utf-8").read()
app=QApplication(sys.argv)
class P(QWebEnginePage):
    def javaScriptConsoleMessage(self,l,m,line,s): print("[c]",m,line,file=sys.stderr)
v=QWebEngineView(); pg=P(v); v.setPage(pg)
pg.settings().setAttribute(QWebEngineSettings.WebAttribute.ShowScrollBars,False)
v.resize(900,700); v.show()
CHK=("JSON.stringify((function(){var b=document.body.getBoundingClientRect();"
 "return {W:innerWidth,H:innerHeight,bl:Math.round(b.left),bt:Math.round(b.top),"
 "bw:Math.round(b.width),bh:Math.round(b.height),"
 "fits:b.width<=innerWidth+1&&b.height<=innerHeight+1,"
 "centred:Math.abs((innerWidth-b.width)/2-b.left)<2&&Math.abs((innerHeight-b.height)/2-b.top)<2,"
 "sw:document.documentElement.scrollWidth,sh:document.documentElement.scrollHeight,"
 "hit:(document.elementFromPoint(Math.round(b.left+b.width/2),Math.round(b.top+b.height/4))||{}).id};})())")
out={}; SIZES=[(900,700),(1600,600),(500,1200),(1200,1500)]; i={"n":0}
def fin(): print("===RESULT==="); print(json.dumps(out,ensure_ascii=False,indent=1)); app.quit()
def step():
    if i["n"]>=len(SIZES): fin(); return
    w,h=SIZES[i["n"]]; i["n"]+=1; v.resize(w,h)
    QTimer.singleShot(600, lambda: pg.runJavaScript(CHK,0,got))
def got(r):
    try: r=json.loads(r)
    except Exception: pass
    out.setdefault("sizes",[]).append(r); QTimer.singleShot(120, step)
def on_load(ok):
    QTimer.singleShot(500, lambda: pg.runJavaScript(READER,0, lambda _:
      pg.runJavaScript("JSON.stringify(RDR.init({layout:'pre-paginated',theme:{name:'dark'}}))",0,
        lambda r:(out.update({"init":json.loads(r)}), QTimer.singleShot(200, step)))))
pg.loadFinished.connect(on_load)
pg.load(QUrl.fromLocalFile(os.path.join(HERE,"fxl.html")))
QTimer.singleShot(40000, fin)
sys.exit(app.exec())
