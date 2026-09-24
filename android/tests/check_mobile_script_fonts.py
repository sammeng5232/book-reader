"""Bounded live Chromium QA for mobile script fonts, with pixel-level references.

Runs a loopback HTTP server for actual font files and the current reader assets.
No phone state or book source is changed. Canvas uses the DOM's computed font
stack and compares pixels against independently loaded selected font files.

Run with a private prepared Windows/Office font library (not committed to git):
    python android/tests/check_mobile_script_fonts.py --font-dir PATH --output-dir PATH
Requires PySide6 and fontTools. This is an opt-in integration script, not a
pytest-discovered test. It opens only an offscreen Chromium window and loopback
HTTP server, and saves its results/screenshot/static variable reference in the
explicit output directory. Font bytes never enter the repository.
"""
import argparse
import hashlib
import http.server
import json
import os
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--font-dir',type=Path,required=True,help='Private font library containing catalog.json')
parser.add_argument('--output-dir',type=Path,required=True,help='Report, screenshot and temporary font output directory')
parser.add_argument('--repo-root',type=Path,default=Path(__file__).resolve().parents[2])
args=parser.parse_args()
ROOT=args.repo_root.resolve()
WORK=args.output_dir.resolve()
FONTS=args.font_dir.resolve()
WORK.mkdir(parents=True,exist_ok=True)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --no-sandbox")
from PySide6.QtCore import QUrl
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEnginePage
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont

catalog = json.loads((FONTS / "catalog.json").read_text(encoding="utf8"))["fonts"]
by_id = {entry["id"]: entry for entry in catalog}
def entry(name):
    return next(e for e in catalog if e["name"] == name)

selection = {
    "latin": entry("Times New Roman"), "hans": entry("楷体 / KaiTi"),
    "hant": entry("微軟正黑體 / Microsoft JhengHei"), "japanese": entry("Yu Gothic"),
    "korean": entry("Malgun Gothic"),
}
def face(e):
    value = {"family": e["family"], "url": "/__er_fonts/user/" + e["id"],
            "weight": e["weight"], "style": "italic" if e["italic"] or
            any(s in e["style"].lower() for s in ("italic", "oblique")) else "normal"}
    if 'weightRange' in e: value['weightRange']=e['weightRange']
    return value
faces = [face(e) for e in catalog]
settings = {"font_" + script: e["family"] for script, e in selection.items()}
settings.update(font_faces=faces, use_book_fonts=False, font_size_px=28,
                font_cjk="KaiTi", layout="scroll", theme="light")
references = selection | {"arial": entry("Arial"), "arial_narrow": entry("Arial Narrow"),
                           "arial_bold": entry("Arial · Bold"),
                           "arial_narrow_bold": entry("Arial Narrow · Bold"),
                           "latin_bold": entry("Times New Roman · Bold"),
                           "latin_italic": entry("Times New Roman · Italic"),
                           "latin_bold_italic": entry("Times New Roman · Bold Italic")}
variable=entry('Reem Kufi')
assert variable['weightRange']==[400,700], 'The selected fixture must expose a 400..700 real wght axis'
variable_static=WORK/'qa-variable-700.ttf'
with TTFont(FONTS/variable['file']) as variable_font:
    static_font=instantiateVariableFont(variable_font,{'wght':700},inplace=False)
    static_font.save(variable_static)
body = ('<main id="book-content"><p id="mixed">WAVY pqgj 123 中文骨直令</p>'
        '<p id="latin">WAVY pqgj 123</p><p id="hans" lang="zh-Hans"><b id="hans-child">骨直令</b></p>'
        '<p id="hant" lang="zh-Hant"><span id="hant-child">骨直令</span></p>'
        '<p id="ja" lang="ja"><span id="ja-child">骨直令かな</span></p>'
        '<p id="ko" lang="ko"><span id="ko-child">骨直令한국</span></p>'
        '<p lang="zh-TW"><span id="tw-child">骨直令</span></p>'
        '<p lang="zh-Hans"><span lang="en" id="nested-en">WAVY pqgj 中文</span></p>'
        '<p><strong id="bold">WAVY pqgj 123</strong> <em id="italic">WAVY pqgj 123</em> '
        '<strong><em id="bolditalic">WAVY pqgj 123</em></strong></p>'
        '<pre id="code">WAVY pqgj 123</pre><math id="math"><mi>x</mi></math>'
        '</main>')
full_css = "\n".join('@font-face{font-family:' + json.dumps(f["family"]) + ';src:url(' +
                     json.dumps(f["url"]) + ');font-weight:' + (' '.join(map(str,f['weightRange'])) if 'weightRange' in f else str(f["weight"])) +
                     ';font-style:' + f["style"] + ';font-display:swap;}' for f in faces)


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        if str(args[2] if len(args)>2 else '') not in ('200','304'):
            print('HTTP',args,flush=True)
    def do_GET(self):
        path = urlsplit(self.path).path
        if path.startswith("/__er_fonts/user/"):
            e = by_id.get(path.rsplit("/", 1)[-1]); file = FONTS / e["file"] if e else None
            if file is None or not file.is_file(): self.send_error(404); return
            content = file.read_bytes(); mime = "font/otf" if file.suffix == ".otf" else "font/ttf"
        elif path in ("/reader.js", "/reader.css"):
            file = ROOT / "assets" / path[1:]; content = file.read_bytes()
            mime = "application/javascript" if path.endswith("js") else "text/css"
        elif path == '/qa-variable-700.ttf':
            content=variable_static.read_bytes(); mime='font/ttf'
        else:
            content = ('<!doctype html><html lang="zh-Hans"><head><meta charset="utf-8">'
                       '<link rel="stylesheet" href="/reader.css"><style>' + full_css +
                       '</style><script src="/reader.js"></script></head><body>' + body + '</body></html>').encode()
            mime = "text/html; charset=utf-8"
        self.send_response(200); self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content))); self.end_headers(); self.wfile.write(content)


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
app = QApplication([])
class DebugPage(QWebEnginePage):
    def javaScriptConsoleMessage(self, level, message, line, source):
        print('CHROMIUM', level, message, flush=True)
view = QWebEngineView(); view.setPage(DebugPage(view)); view.resize(1200, 900); view.show()
rows = []
def wait(predicate, timeout=25):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents(); QTest.qWait(10)
        if predicate(): return
    raise AssertionError("Chromium timed out")
def js(source):
    out = []
    view.page().runJavaScript("JSON.stringify((" + source + "))", lambda value: out.append(value))
    wait(lambda: bool(out))
    return json.loads(out[0]) if out[0] else None
def async_js(source):
    js("(function(){window.__qa_result=null;Promise.resolve().then(async function(){" + source +
       "}).then(v=>window.__qa_result={ok:true,value:v}).catch(e=>window.__qa_result={ok:false,error:String(e)});return true})()")
    wait(lambda: js("window.__qa_result") is not None)
    result = js("window.__qa_result")
    if not result["ok"]: raise AssertionError(result["error"])
    return result.get("value")
def check(name, ok, data=None):
    rows.append({"name":name,"ok":bool(ok),"data":data})
    print(("PASS " if ok else "FAIL ") + name + " " + json.dumps(data,ensure_ascii=False),flush=True)
def fresh(mobile=True, fixed=False):
    loaded = []; view.loadFinished.connect(loaded.append)
    view.load(QUrl(f"http://127.0.0.1:{server.server_port}/test?{time.time()}"))
    wait(lambda: bool(loaded)); view.loadFinished.disconnect(loaded.append)
    js("(function(){window.__qa_nodes=Array.from(document.querySelectorAll('#book-content *')).flatMap(e=>Array.from(e.childNodes).filter(n=>n.nodeType===3));window.__qa_text=__qa_nodes.map(n=>n.nodeValue);return true})()")
    config = dict(settings=settings, mobileHost=mobile, fixedLayout=fixed,
                  viewport={"w":1200,"h":1600} if fixed else None)
    js("epubReader.init(" + json.dumps(config) + ")")
    # Reference families are independent font faces with no unicode-range.
    refs = {key:{"url":"/__er_fonts/user/"+e["id"]} for key,e in references.items()}
    async_js("var refs=" + json.dumps(refs) + ";await Promise.all(Object.keys(refs).map(async k=>{var f=new FontFace('QA '+k,'url('+refs[k].url+')');try{await f.load();}catch(e){throw Error(k+': '+e)}document.fonts.add(f);}));await document.fonts.ready;return true;")
    js(r"""(function(){
      window.qaHash=function(text,family,weight,style){var c=document.createElement('canvas');c.width=900;c.height=100;
        var x=c.getContext('2d');x.font=(style||'normal')+' '+(weight||'400')+' 42px '+family;x.fillStyle='#000';x.textBaseline='top';x.fillText(text,2,2);
        var bytes=x.getImageData(0,0,900,100).data,h=2166136261;for(var i=0;i<bytes.length;i++)h=Math.imul(h^bytes[i],16777619);return (h>>>0).toString(16)};
      window.qaNode=function(id,text){var e=document.getElementById(id),c=getComputedStyle(e);return {family:c.fontFamily,weight:c.fontWeight,style:c.fontStyle,hash:qaHash(text||e.textContent,c.fontFamily,c.fontWeight,c.fontStyle)}};
      return true;})()""")
    async_js("await Promise.all(['latin','hans','hant','japanese','korean'].map(s=>document.fonts.load('42px \"ER Mobile '+s+'\"','WAVY 骨直令かな한국')));await document.fonts.ready;return true;")


try:
    fresh()
    view.grab().save(str(WORK/'font-routing-preview.png'))
    for id,script,text in [("latin","latin","WAVY pqgj 123"),("hant-child","hant","骨直令"),
                           ("ja-child","japanese","骨直令かな"),("ko-child","korean","骨直令한국"),
                           ("tw-child","hant","骨直令")]:
        got=js("qaNode("+json.dumps(id)+","+json.dumps(text)+")")
        reference_stack='"QA latin"' + (',"QA '+script+'"' if script != 'latin' else '')
        want=js("qaHash("+json.dumps(text)+", "+json.dumps(reference_stack)+")")
        check("actual pixels " + id + " use " + script,got["hash"]==want,{"got":got,"reference":want})
    mixed=js("qaNode('mixed')")
    want=js("qaHash(document.getElementById('mixed').textContent,'\"QA latin\",\"QA hans\"')")
    check("single mixed text node uses Latin plus KaiTi",mixed["hash"]==want,{"got":mixed,"reference":want})
    all_kaiti=js("qaHash(document.getElementById('mixed').textContent,'\"QA hans\"')")
    check("mixed pixels differ from all-KaiTi Latin leakage",mixed["hash"]!=all_kaiti,{"mixed":mixed["hash"],"allKaiTi":all_kaiti})
    hans=js("qaNode('mixed','骨直令')")
    check("Han pixels equal KaiTi",hans["hash"]==js("qaHash('骨直令','\"QA latin\",\"QA hans\"')"),hans)
    for id,ref in [("bold","latin_bold"),("italic","latin_italic"),("bolditalic","latin_bold_italic")]:
        got=js("qaNode("+json.dumps(id)+")")
        want=js("qaHash('WAVY pqgj 123',"+json.dumps('"QA '+ref+'"')+")")
        check("full faces preserve " + id,got["hash"]==want,{"got":got,"reference":want})
    before=js("({flat:epubReader.flatText(),matches:epubReader.search('骨直令'),capture:epubReader.capture()})")
    changed=dict(settings,font_latin=references["arial"]["family"])
    js("epubReader.applySettings("+json.dumps(changed)+")")
    async_js("await document.fonts.load('42px \"ER Mobile latin\"','WAVY pqgj');await document.fonts.ready;return true;")
    for id,text,ref in [("latin","WAVY pqgj 123","arial"),("mixed","骨直令","hans")]:
        got=js("qaNode("+json.dumps(id)+","+json.dumps(text)+")")
        reference_stack='"QA arial"' + (',"QA '+ref+'"' if ref != 'arial' else '')
        want=js("qaHash("+json.dumps(text)+","+json.dumps(reference_stack)+")")
        check("Arial switch retains correct " + ref,got["hash"]==want,{"got":got,"reference":want})
    got=js("qaNode('bold')")
    regular=js("qaHash('WAVY pqgj 123','\"QA arial_bold\"')")
    narrow=js("qaHash('WAVY pqgj 123','\"QA arial_narrow_bold\"')")
    check("Arial bold does not become Arial Narrow Bold",got["hash"]==regular,{"got":got,"ArialBold":regular,"ArialNarrowBold":narrow})
    changed['font_hans']=selection['hant']['family']
    js("epubReader.applySettings("+json.dumps(changed)+")")
    async_js("await document.fonts.load('42px \"ER Mobile hans\"','骨直令');await document.fonts.ready;return true;")
    got=js("qaNode('mixed','骨直令')")
    want=js("qaHash('骨直令','\"QA arial\",\"QA hant\"')")
    check("changing Chinese font updates Han glyphs",got['hash']==want,{'got':got,'reference':want})
    got=js("qaNode('mixed','WAVY pqgj 123')")
    want=js("qaHash('WAVY pqgj 123','\"QA arial\"')")
    check("changing Chinese font preserves Latin selection",got['hash']==want,{'got':got,'reference':want})
    after=js("({flat:epubReader.flatText(),matches:epubReader.search('骨直令'),nodes:__qa_nodes.every((n,i)=>n.isConnected&&n.nodeValue===__qa_text[i]),mixedChildCount:document.getElementById('mixed').childNodes.length})")
    check("font routing preserves text nodes and search offsets",before["flat"]==after["flat"] and before["matches"]==after["matches"] and after["nodes"] and after["mixedChildCount"]==1,after)
    check("code typography remains monospace","ER Mobile" not in js("getComputedStyle(document.getElementById('code')).fontFamily"))
    check("MathML typography is not overridden","ER Mobile" not in js("getComputedStyle(document.getElementById('math')).fontFamily"))
    changed['font_latin']=variable['family']
    js("epubReader.applySettings("+json.dumps(changed)+")")
    loaded_variable=async_js("var f=new FontFace('QA variable static 700','url(/qa-variable-700.ttf)');await f.load();document.fonts.add(f);var faces=await document.fonts.load('700 42px \"ER Mobile latin\"','WAVY pqgj 123');await document.fonts.ready;return faces.map(f=>({family:f.family,weight:f.weight,status:f.status}));")
    check("variable 700 resolves a loaded range face",any(f['weight']=='400 700' and f['status']=='loaded' for f in loaded_variable),loaded_variable)
    got=js("qaNode('bold')")
    want=js("qaHash('WAVY pqgj 123','\"QA variable static 700\"')")
    check("variable weight 700 pixels equal independently instantiated axis",got['hash']==want,{'got':got,'static700':want})
    js("epubReader.applySettings("+json.dumps(dict(changed,use_book_fonts=True))+")")
    check("book-font mode disables routing",js("!document.documentElement.hasAttribute('data-er-script-fonts')&&document.querySelector('style[data-er=mobile-fonts]').textContent===''"))
    fresh(mobile=False)
    check("desktop does not activate mobile routing",js("!document.documentElement.hasAttribute('data-er-script-fonts')&&document.querySelector('style[data-er=mobile-fonts]').textContent===''"))
    fresh(mobile=True,fixed=True)
    check("fixed layout does not activate mobile routing",js("!document.documentElement.hasAttribute('data-er-script-fonts')&&document.querySelector('style[data-er=mobile-fonts]').textContent===''"))
finally:
    (WORK/"font-routing-results.json").write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding="utf8")
    (WORK/'font-routing-metadata.json').write_text(json.dumps({'catalog_sha256':hashlib.sha256((FONTS/'catalog.json').read_bytes()).hexdigest(),'reader_js_sha256':hashlib.sha256((ROOT/'assets/reader.js').read_bytes()).hexdigest(),'font_count':len(catalog)},indent=2),encoding='utf8')
    view.close(); view.page().deleteLater(); app.processEvents(); server.shutdown()
failures=[r["name"] for r in rows if not r["ok"]]
print(json.dumps({"total":len(rows),"failures":failures},ensure_ascii=False),flush=True)
raise SystemExit(bool(failures))
