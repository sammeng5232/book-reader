"""Load every prepared font through real Chromium's OTS sanitizer, no phone use.

Opt-in integration check, requiring PySide6 and an external private font library:
    python android/tests/check_font_library_load.py --font-dir PATH --output-dir PATH
No font files are copied. Reports record the exact input catalogue SHA256.
"""
import argparse
import http.server
import json
import os
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--font-dir',type=Path,required=True,help='Private font library containing catalog.json')
parser.add_argument('--output-dir',type=Path,required=True,help='Directory for progress and final JSON reports')
args=parser.parse_args()
HERE=args.output_dir.resolve()
LIB=args.font_dir.resolve()
HERE.mkdir(parents=True,exist_ok=True)
catalog_bytes = (LIB/'catalog.json').read_bytes()
catalog = json.loads(catalog_bytes)['fonts']
by_id = {e['id']:e for e in catalog}
os.environ['QT_QPA_PLATFORM']='offscreen'
os.environ['QTWEBENGINE_CHROMIUM_FLAGS']='--disable-gpu --no-sandbox'
from PySide6.QtCore import QUrl
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEnginePage

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        ident=urlsplit(self.path).path[1:]
        if ident in by_id:
            content=(LIB/by_id[ident]['file']).read_bytes(); mime='application/octet-stream'
        else:
            content=b'<!doctype html><meta charset="utf-8"><body></body>'; mime='text/html'
        self.send_response(200); self.send_header('Content-Type',mime)
        self.send_header('Content-Length',str(len(content))); self.end_headers()
        try: self.wfile.write(content)
        except (ConnectionError, BrokenPipeError): pass

server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()
app=QApplication([])
console=[]
class Page(QWebEnginePage):
    def javaScriptConsoleMessage(self, level, message, line, source):
        console.append(message)
        print('CHROMIUM',message,flush=True)
view=QWebEngineView(); view.setPage(Page(view)); view.resize(800,600); view.show()
loaded=[]; view.loadFinished.connect(loaded.append)
view.load(QUrl(f'http://127.0.0.1:{server.server_port}/'))
def wait(predicate, timeout=60):
    until=time.monotonic()+timeout
    while time.monotonic()<until:
        app.processEvents(); QTest.qWait(5)
        if predicate():return
    raise TimeoutError('Chromium timeout')
wait(lambda:bool(loaded))
def js(s):
    out=[]; view.page().runJavaScript('JSON.stringify(('+s+'))',out.append)
    wait(lambda:bool(out));return json.loads(out[0]) if out[0] else None
rows=[]
try:
    for offset in range(0,len(catalog),8):
        entries=catalog[offset:offset+8]
        js('(()=>{window.qa=null; Promise.all('+json.dumps([e['id'] for e in entries])+'.map(async id=>{try {var f=new FontFace("QA "+id,"url(/"+id+")");await f.load();return {id,status:f.status};} catch(e) {return {id,status:"error",error:String(e)};}})).then(r=>window.qa=r);return true})()')
        wait(lambda:js('window.qa') is not None)
        for result in js('window.qa'):
            e=by_id[result['id']];result['name']=e['name'];rows.append(result)
            if result['status']!='loaded': print('FAIL',e['name'],result,flush=True)
        print('PROGRESS',len(rows),'/',len(catalog),'failed',sum(e['status']!='loaded' for e in rows),flush=True)
        (HERE/'font-web-load-progress.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
finally:
    import hashlib
    report={'total':len(rows),'catalog_total':len(catalog),'loaded':sum(e['status']=='loaded' for e in rows),
      'catalog_sha256':hashlib.sha256(catalog_bytes).hexdigest(),
      'failed':[e for e in rows if e['status']!='loaded'],'console':console,'fonts':rows}
    (HERE/'font-web-load-results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    view.close();view.page().deleteLater();app.processEvents();server.shutdown()
print(json.dumps({k:v for k,v in report.items() if k not in ('fonts','console')},ensure_ascii=False),flush=True)
raise SystemExit(bool(report['failed']) or len(rows)!=len(catalog))
