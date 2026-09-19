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
TEST = r"""
(function(){
  RDR.init({theme:{name:'light',font:'serif',fontSize:19,lineHeight:1.78,gap:72}});
  var out={pages:RDR.pages(), roundtrip:[], themeCycle:[]};
  for(var i=0;i<RDR.pages();i++){
    RDR.goto(i);
    var c=RDR.capture();
    RDR.goto(0);                       // move away
    var back=RDR.restore(c);
    out.roundtrip.push({want:i, got:back, ok:back===i, char:c&&c.char, text:c&&c.text.slice(0,14)});
  }
  // theme cycle at page 3
  RDR.goto(3); var c3=RDR.capture();
  ['dark','sepia','light'].forEach(function(t){
    RDR.setTheme({name:t,font:'serif',fontSize:19,lineHeight:1.78});
    out.themeCycle.push({theme:t,page:RDR.page(),pages:RDR.pages(),
      charOnScreen:(function(){ var p=RDR.capture(); return p&&p.char; })()});
  });
  out.finalPage=RDR.page(); out.origChar=c3&&c3.char;
  // font size sweep
  out.fontSweep=[];
  RDR.goto(3); var c4=RDR.capture();
  [15,19,24,30,19].forEach(function(fs){
    RDR.setTheme({name:'light',font:'serif',fontSize:fs,lineHeight:1.78});
    out.fontSweep.push({fs:fs,page:RDR.page(),pages:RDR.pages()});
  });
  out.endChar=RDR.capture().char; out.startChar=c4.char;
  return JSON.stringify(out);
})()
"""
def go(ok):
    QTimer.singleShot(500, lambda: pg.runJavaScript(READER,0, lambda _:
      QTimer.singleShot(200, lambda: pg.runJavaScript(TEST,0, lambda r:(print(r), app.quit())))))
pg.loadFinished.connect(go)
pg.load(QUrl.fromLocalFile(os.path.join(HERE,"book.html")))
QTimer.singleShot(40000, app.quit)
sys.exit(app.exec())
