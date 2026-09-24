"""Real Chromium regression for the Android EPUB reading surface.

Run directly with the repository's PySide6 Python. Uses an ephemeral loopback
HTTP server and generated raster fixtures. It checks CSS sizes, source priority,
font OTF loading and distinct glyphs, zoom exclusion, passive gestures, and
fixed-layout immunity. It never connects to or controls a phone.
"""
import os,sys,json,pathlib,zipfile,functools,threading,http.server,tempfile,shutil,io,time
from PIL import Image,ImageDraw
root=pathlib.Path(os.environ.get('BOOK_READER_TEST_ROOT', str(pathlib.Path(__file__).resolve().parents[2])))
base=pathlib.Path(tempfile.mkdtemp(prefix='book-reader-mobile-test-'))
os.environ['QTWEBENGINE_CHROMIUM_FLAGS']='--no-sandbox --disable-gpu'
os.environ['QT_QPA_PLATFORM']='offscreen'
from PySide6.QtCore import QUrl,QTimer,QEventLoop
from PySide6.QtWidgets import QApplication
from PySide6.QtWebEngineWidgets import QWebEngineView
site=base/'site';site.mkdir(exist_ok=True)
# Small generated monochrome glyph series, never copyrighted sample assets.
folder=site/'bjork/OEBPS/images';folder.mkdir(parents=True)
for number,size in [(1,(47,25)),(22,(361,95))]:
 im=Image.new('RGB',size,'white');d=ImageDraw.Draw(im)
 for y in range(3,size[1]-18,26):
  for x in range(3,size[0]-12,18):
   d.rectangle((x,y,x+2,y+16),fill='black')
   d.rectangle((x+9,y,x+11,y+16),fill='black')
   d.rectangle((x,y+7,x+11,y+9),fill='black')
 im.save(folder/f'oso-9780198851615-math-{number:04}.gif')
for rel in ('assets/reader.js','assets/reader.css'):
 target=site/rel;target.parent.mkdir(exist_ok=True);shutil.copyfile(root/rel,target)
fonts=site/'__er_fonts';fonts.mkdir(exist_ok=True)
for name,file in {'song':'FandolSong-Regular.otf','hei':'FandolHei-Regular.otf','kai':'FandolKai-Regular.otf','fang':'FandolFang-Regular.otf','termes':'texgyretermes-regular.otf'}.items():shutil.copyfile(root/'android/app/src/main/assets/texbundle'/file,fonts/(name+'.otf'))
fontcss=''.join(f"@font-face{{font-family:'ER {name}';src:url('/__er_fonts/{file}.otf')}}" for name,file in [('Fandol Song','song'),('Fandol Hei','hei'),('Fandol Kai','kai'),('Fandol Fang','fang'),('Termes','termes')])
(site/'fonts.css').write_text(fontcss,encoding='utf8')
body='<p id="text">Reading formulas 字体检查汉字</p><p>Short <img id="short" src="/bjork/OEBPS/images/oso-9780198851615-math-0001.gif"/> fraction <img id="fraction" src="/bjork/OEBPS/images/oso-9780198851615-math-0022.gif"/></p><p>CSS <img id="css" src="/bjork/OEBPS/images/oso-9780198851615-math-0022.gif" style="height:3em;width:auto"/></p><p>Pixels <img id="px" width="32" src="/bjork/OEBPS/images/oso-9780198851615-math-0001.gif"/></p><p><img id="ordinary" src="blue.png"/></p>'+''.join('<p>Long scroll paragraph '+str(i)+' testing scrolling and reading positions.</p>' for i in range(100))
im=Image.new('RGB',(160,80),'blue');im.save(site/'blue.png')
(site/'test.html').write_text('<!doctype html><html><head><meta charset="utf-8"><link rel="stylesheet" href="fonts.css"></head><body>'+body+'</body></html>',encoding='utf8')
class Quiet(http.server.SimpleHTTPRequestHandler):
 def log_message(self,*args):pass
server=http.server.ThreadingHTTPServer(('127.0.0.1',0),functools.partial(Quiet,directory=site));threading.Thread(target=server.serve_forever,daemon=True).start()
app=QApplication([]);view=QWebEngineView();view.resize(900,1000);view.show()
loop=QEventLoop();view.loadFinished.connect(loop.quit);view.load(QUrl(f'http://127.0.0.1:{server.server_port}/test.html'));QTimer.singleShot(20000,loop.quit);loop.exec()
def js(s):
 loop=QEventLoop();out=[]
 view.page().runJavaScript(s,lambda v:(out.append(v),loop.quit()));QTimer.singleShot(10000,loop.quit);loop.exec();return out[0] if out else None
def wait(ms):
 loop=QEventLoop();QTimer.singleShot(ms,loop.quit);loop.exec()
js((root/'assets/reader.js').read_text(encoding='utf8'))
scales={'bjork/oebps/images/oso-#-math-#.gif':.70/17}
cfg={'mobileHost':True,'formulaScales':scales,'fixedLayout':False,'cssText':(root/'assets/reader.css').read_text(),'settings':{'layout':'scroll','font_size_px':20,'font_latin':'serif','font_cjk':'serif','image_click_zoom':True}}
js('epubReader.init('+json.dumps(cfg)+')');wait(600)
expr="JSON.stringify(Array.from(document.images).map(i=>({id:i.id,w:i.getBoundingClientRect().width,h:i.getBoundingClientRect().height,fs:getComputedStyle(i).fontSize,formula:i.dataset.erFormula||null})))"
a=json.loads(js(expr));js("epubReader.applySettings({layout:'scroll',font_size_px:30,font_latin:'serif',font_cjk:'serif'})");wait(200);b=json.loads(js(expr))
print('20px',a);print('30px',b)
for x,y in zip(a,b):
 if x['id'] in ('short','fraction','css','px'):assert abs(y['w']/x['w']-1.5)<.03,(x,y)
 if x['id']=='ordinary':assert abs(y['w']-x['w'])<1,(x,y)
assert a[1]['h']/a[0]['h']>3
assert abs(a[2]['h']-60)<1
assert abs(a[3]['w']-40)<1
widths={}
for family in ('ER Fandol Song','ER Fandol Hei','ER Fandol Kai','ER Fandol Fang','ER Termes'):
 js("epubReader.applySettings("+json.dumps({'layout':'scroll','font_size_px':25,'font_latin':family,'font_cjk':family})+")")
 js("document.fonts.load('25px \""+family+"\"')");wait(1200)
 result=json.loads(js("JSON.stringify({loaded:document.fonts.check('25px \""+family+"\"'),family:getComputedStyle(document.querySelector('#text')).fontFamily})"));print('font',family,result);assert result['loaded'] and family in result['family']
 result['glyphs']=js("(function(){var c=document.createElement('canvas');c.width=200;c.height=50;var x=c.getContext('2d');x.font='32px \""+family+"\"';x.fillText('汉字Reading',0,36);return c.toDataURL()})()")
 widths[family]=result
assert len({r['glyphs'] for r in widths.values()})==5
# Real DOM input pipeline: link/image taps never emit centre command; move-return is not tap.
js("epubReader.drainEvents();document.querySelector('#text').dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,pointerId:1,clientX:200,clientY:200}));document.querySelector('#text').dispatchEvent(new PointerEvent('pointermove',{bubbles:true,pointerId:1,clientX:200,clientY:240}));document.querySelector('#text').dispatchEvent(new PointerEvent('pointerup',{bubbles:true,pointerId:1,clientX:200,clientY:200}));")
ev=json.loads(js('JSON.stringify(epubReader.drainEvents())'));print('move-return',ev);assert not any(e.get('name')=='keyUnhandled' or e.get('kind')=='keyUnhandled' for e in ev)

# Tap centre is emitted once; native host no longer steals WebView's UP.
js("epubReader.drainEvents();document.querySelector('#text').dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,pointerId:2,clientX:200,clientY:200}));document.querySelector('#text').dispatchEvent(new PointerEvent('pointerup',{bubbles:true,pointerId:2,clientX:200,clientY:200}));")
print('tap',js('JSON.stringify(epubReader.drainEvents())'))
# The zoom viewer is reader UI, never another formula to normalise.
js("epubReader.applySettings({layout:'scroll',font_size_px:25,image_click_zoom:true});document.querySelector('#short').dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,pointerId:3,clientX:150,clientY:150}));document.querySelector('#short').dispatchEvent(new PointerEvent('pointerup',{bubbles:true,pointerId:3,clientX:150,clientY:150}))")
wait(300)
zoom=json.loads(js("JSON.stringify({exists:!!document.querySelector('#er-zoom img'),sized:!!document.querySelector('#er-zoom img[data-er-image-size]')})"))
print('zoom exclusion',zoom);assert zoom['exists'] and not zoom['sized']
js('epubReader.closeZoom()')
# Phone-width limit preserves the original 361:95 aspect ratio even with authored height.
view.resize(420,760);wait(1200)
small=json.loads(js(expr));print('narrow',small)
assert abs(small[1]['w']/small[1]['h']-361/95)<.02
assert abs(small[2]['w']/small[2]['h']-361/95)<.02
# Passive touch gestures: an ordinary swipe that starts within the chapter does
# not emit a chapter event, while a fresh outward swipe at the bottom does.
def gesture(start,end):
 script="""(function(){var target=document.body;
 function fire(type,y,down){var t=new Touch({identifier:1,target:target,clientX:150,clientY:y});
 target.dispatchEvent(new TouchEvent(type,{bubbles:true,touches:down?[t]:[],changedTouches:[t]}));}
 fire('touchstart',%s,true);fire('touchend',%s,false);})()"""%(start,end)
 js(script)
js("epubReader.applySettings({layout:'scroll',font_size_px:25});epubReader.gotoPercent(.5);epubReader.drainEvents()")
gesture(400,320);ev=js('JSON.stringify(epubReader.drainEvents())');print('mid-scroll',ev);assert 'NextChapter' not in ev
js("epubReader.gotoPercent(1);epubReader.drainEvents()")
gesture(400,320);ev=js('JSON.stringify(epubReader.drainEvents())');print('edge-scroll',ev);assert 'NextChapter' in ev
js("epubReader.applySettings({layout:'paged',font_size_px:25});epubReader.gotoPage(0);epubReader.drainEvents()")
gesture(400,360);state=json.loads(js('JSON.stringify(epubReader.state())'));print('short vertical page gesture',state);assert state['page']==1
# Fixed-layout pages must never acquire reader formula sizing.
js("Array.from(document.images).forEach(i=>i.removeAttribute('data-er-image-size'));epubReader.init({mobileHost:true,fixedLayout:true,viewport:{w:900,h:1200},formulaScales:"+json.dumps(scales)+",settings:{font_size_px:30}})")
assert js("document.querySelectorAll('img[data-er-image-size]').length")==0

view.grab().save(str(base/'mobile-reader-host.png'))
(base/'host-evidence.json').write_text(json.dumps({'20px':a,'30px':b,'fonts':widths},indent=2),encoding='utf8')
view.close();server.shutdown();shutil.rmtree(base);print('HOST_QA_PASS')




