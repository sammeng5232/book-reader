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
out={}; SIZES=[(1500,950),(560,1000),(1100,620),(900,700)]; i={"n":0}
CHECK = ("JSON.stringify((function(){var c=RDR.capture();"
 "var f=window.__ANCHOR;var lo=RDR.progress();"
 "var n=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT).nextNode();"
 "var idx=document.body.textContent.indexOf(window.__TXT);"
 "var r=null;"
 "if(window.__RANGE){var rr=window.__RANGE.getClientRects();if(rr.length)r={l:Math.round(rr[0].left),t:Math.round(rr[0].top)};}"
 "return {W:innerWidth,H:innerHeight,page:RDR.page(),pages:RDR.pages(),"
 "char:c&&c.char,text:c&&c.text.slice(0,16),frac:+(c&&c.frac).toFixed(4),"
 "anchorRect:r,anchorVisible:!!r&&r.l>=0&&r.l<innerWidth&&r.t>=0&&r.t<innerHeight};})())")
def fin():
    print("===RESULT==="); print(json.dumps(out,ensure_ascii=False,indent=1)); app.quit()
def step():
    if i["n"]>=len(SIZES): fin(); return
    w,h=SIZES[i["n"]]; i["n"]+=1; v.resize(w,h)
    QTimer.singleShot(700, lambda: pg.runJavaScript(CHECK,0,got))
def got(r):
    try: r=json.loads(r)
    except Exception: pass
    out.setdefault("sizes",[]).append(r); QTimer.singleShot(150, step)
def start(_):
    pg.runJavaScript(
      "RDR.init({theme:{name:'light',font:'serif',fontSize:19,lineHeight:1.78,gap:72}});"
      "RDR.goto(14);"
      "var c=RDR.capture(); window.__TXT=c.text;"
      "var fl=(function(){var tw=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);"
      "var s='',n,nodes=[],st=[];while((n=tw.nextNode())){st.push(s.length);nodes.push(n);s+=n.nodeValue;}"
      "return {s:s,nodes:nodes,st:st};})();"
      "var ci=c.char,a=0;for(var k=0;k<fl.st.length;k++){if(fl.st[k]<=ci)a=k;}"
      "var rg=document.createRange();var nd=fl.nodes[a],off=ci-fl.st[a];"
      "rg.setStart(nd,Math.min(off,nd.nodeValue.length-1));rg.setEnd(nd,Math.min(off+6,nd.nodeValue.length));"
      "window.__RANGE=rg;"
      "JSON.stringify({page:RDR.page(),pages:RDR.pages(),char:c.char,text:c.text})",0,
      lambda r:(out.update({"start":json.loads(r)}), QTimer.singleShot(200, step)))
def on_load(ok):
    QTimer.singleShot(500, lambda: pg.runJavaScript(READER,0,lambda _: QTimer.singleShot(200, lambda: start(None))))
pg.loadFinished.connect(on_load)
pg.load(QUrl.fromLocalFile(os.path.join(HERE,"big.html")))
QTimer.singleShot(60000, fin)
sys.exit(app.exec())
