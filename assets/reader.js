/* ===========================================================================
   Book Reader — assets/reader.js
   The in-page reading engine (owner C).  webhost.py (owner D) injects this file
   into every book document at DocumentCreation, in ApplicationWorld (isolated
   from any script the book carries), and Python drives it through
   QWebEnginePage.runJavaScript().

   Python -> JS : window.epubReader.<fn>()           (CONTRACT.md section 4)
   JS -> Python : window.epubReaderHost.<signal>(x)  (QWebChannel facade, owner D)
                  signals: ready(state), positionChanged(state), linkClicked(href),
                  selectionChanged(info|null), noteRequested(id),
                  keyUnhandled(keyDescription)
                  Payloads are passed as plain OBJECTS/strings; the facade does
                  the JSON.stringify.  (init({hostPayload:'json'}) switches to
                  pre-stringified payloads for a facade that does not.)

   Everything here is lifted from docs/research/reading-ux-engine.md and
   _proto2/reader.js, both proven by running code in the exact Chromium
   (140.0.7339.225) that ships with PySide6 6.11.1 on this machine.  Each
   comment marked LOAD-BEARING marks a bug that was already hit and fixed.

   GPOS UNIT DECISION  (CONTRACT.md 2.1 rule 5 -- epublib.py matches this)
   ----------------------------------------------------------------------
   Every character offset that crosses the Python boundary -- locator.gpos,
   search() {gpos,length}, showMatches() ranges, applyHighlights() start/end,
   selectionInfo() start/end, state().gpos -- is measured in UNICODE CODE
   POINTS, i.e. Python `str` indices into epublib.plain_text(zip_name).
   It is NOT UTF-16 code units.  JavaScript strings are UTF-16, so an astral
   character (CJK Ext-B U+20BB7, emoji) is 2 JS units but 1 Python index;
   section 7 converts at the boundary (toCP/fromCP) and costs nothing for the
   overwhelmingly common document with no surrogate pair.  Verified with
   U+20BB7 and U+2A6A5 in a live QWebEngineView.  flatText() returns the
   string itself, so its Python len() is the code-point length.

   FLAT TEXT (CONTRACT.md 2.1): text nodes under <body> in document order, raw
   nodeValue concatenated with no separators/collapsing/trimming, rejecting any
   text inside <script>, <style> or <noscript> (at any depth, any namespace).
   display:none text is KEPT.

   SYNTHETIC keyUnhandled VALUES (never a real key description):
     'EpubReader.NextChapter'  turn past the last page / FXL next
     'EpubReader.PrevChapter'  turn before the first page / FXL previous
     'EpubReader.TapCentre'    tap in the middle 56% zone (toggle chrome)

   THE FOUR INVARIANTS YOU MUST NOT BREAK
   --------------------------------------
   1. PAGINATION ARITHMETIC.  content-box width == W, column-width == W - gap,
      column-gap == gap, padding-inline == gap/2  =>  the scroll step is
      EXACTLY W.  All of W/gap/colW are measured integer px; never vw/vh.
   2. PAGE MAPPING uses the EXACT, UNROUNDED scrollLeft.  The rounded form
      (`currentPage + floor(rect.left/step)`) drifts up to half a page right
      after a relayout.
   3. POSITION is a flattened-text global character offset (gpos) plus a
      snippet plus a structural path.  Never DOM child indices alone.
   4. Never capture inside a resize handler, and never re-capture after a
      restore.  Both leak a fraction of a page per cycle.  The locator is
      pinned (st.lastLoc) to the last user navigation or explicit restore.
   =========================================================================== */
(function () {
'use strict';

/* Idempotent load: injecting the script twice must be a no-op. */
if (window.epubReader && window.epubReader.__epubReaderEngine === 1) { return; }

var doc = document;
/* LOAD-BEARING: at DocumentCreation (where webhost injects us) there is NO
   documentElement yet -- verified null in this Chromium.  `de` is refreshed
   by currentDE() and at boot; nothing may dereference it before then. */
var de  = doc.documentElement;
function currentDE() { if (!de) { de = doc.documentElement; } return de; }

/* LOAD-BEARING: THE VIEWPORT SCROLLER IS document.scrollingElement, NOT <html>.
   Most real EPUB chapters start with `<?xml ...?>` and carry no DOCTYPE; served
   as text/html they render in QUIRKS MODE, where <body> is the viewport
   scroller and documentElement.clientHeight is the height of the whole <html>
   box (measured 12521px on one of the user's books: the page count came out as
   2 and a third of the chapter was unreachable).  Every scroll/client metric
   goes through SE(); viewport size through viewportSize(). */
function SE() { return doc.scrollingElement || currentDE(); }
function viewportSize() {
  var w = window.innerWidth || 0, h = window.innerHeight || 0;
  var root = currentDE(), b = doc.body;
  /* Standards mode: root.client* IS the viewport minus any scrollbar. */
  if (doc.compatMode !== 'BackCompat') {
    return [(root && root.clientWidth) || w, (root && root.clientHeight) || h];
  }
  /* Quirks mode: body.client* is the viewport only while <body> is not
     "potentially scrollable" (CSSOM View: html AND body overflow both neither
     visible nor clip).  Otherwise it is the body box -- fall back to inner*. */
  if (b && root) {
    var bs = getComputedStyle(b), rs = getComputedStyle(root);
    var open = function (v) { return v === 'visible' || v === 'clip'; };
    var potentially = !(open(bs.overflowX) && open(bs.overflowY)) && !(open(rs.overflowX) && open(rs.overflowY));
    if (!potentially && b.clientWidth > 0 && b.clientHeight > 0) { return [b.clientWidth, b.clientHeight]; }
  }
  return [w, h];
}

var NS_OPS   = 'http://www.idpf.org/2007/ops';
var NS_XHTML = 'http://www.w3.org/1999/xhtml';
var MIN_COL  = 200;    /* never let a column get narrower than this            */
var MAX_HITS = 5000;   /* cap on in-document search matches                    */

/* ===========================================================================
   1. STATE
   =========================================================================== */
var st = {
  booted: false,
  inited: false,

  mode: 'paginated',          /* internal: 'paginated' | 'scroll' | 'fixed'    */
  wanted: 'paginated',        /* what the user asked for before FXL/vertical   */
  rtl: false,
  vertical: false,            /* writing-mode vertical-*: scroll fallback      */
  verticalRL: false,
  fxl: null,                  /* {w,h,src} when this is a fixed-layout page    */

  W: 0, H: 0,                 /* measured viewport px                          */
  half: 0, gap: 0, colW: 0,   /* half == padding-inline == gap/2               */
  step: 0,                    /* == W, always                                  */
  padT: 0, padB: 0, pageH: 0,

  pages: 1, page: 0,

  flat: null,                 /* {text,nodes,starts,solid,index,cp}            */
  lastLoc: null,              /* pinned to the last USER navigation / restore  */
  progScroll: null,           /* [left, top] after our own last scroll write   */

  matches: [],                /* [{gpos,length}] from search()/showMatches()   */
  matchActive: -1,
  highlights: [],             /* [{id,start,end,color,style,note}]             */

  book: null,                 /* {offset,total} chars, for whole-book percent  */
  settings: null,
  tokens: null,               /* resolved theme tokens after applySettings     */
  hostPayload: 'object',
  zoomOn: true,
  resolvedU16: -1,
  verticalDetected: false,
  fxlScale: 1,
  fxlOffset: null
};

/* ===========================================================================
   2. STYLE PLUMBING
   Layer order is established by a <style> inserted as the FIRST child of
   <html>, before <head>, so it holds no matter where reader.css lands.
   =========================================================================== */
function mkStyle(id, first) {
  currentDE();
  var existing = doc.querySelector('style[data-er="' + id + '"], link[data-er="' + id + '"]');
  if (existing) { return existing; }
  var s = doc.createElementNS(NS_XHTML, 'style');
  s.setAttribute('data-er', id);
  if (first) { de.insertBefore(s, de.firstChild); }
  else { (doc.head || de).appendChild(s); }
  return s;
}

var shOrder = null;
function ensureOrder() {
  if (!currentDE()) { return false; }
  if (!shOrder || !shOrder.isConnected) {
    shOrder = mkStyle('order', true);
    if (!shOrder.textContent) { shOrder.textContent = '@layer er-safety, er-theme;'; }
  }
  if (de.firstChild !== shOrder) { de.insertBefore(shOrder, de.firstChild); }
  return true;
}
/* Declare the layer order as early as the document allows: immediately if
   <html> exists, else the moment the parser creates it. */
if (!ensureOrder()) {
  try {
    var orderObs = new MutationObserver(function () {
      if (ensureOrder()) { orderObs.disconnect(); }
    });
    orderObs.observe(doc, { childList: true });
  } catch (e) { /* boot() inserts it */ }
}

var shTok = null, shGeom = null, shOpt = null, shPaint = null;

function ensureSheets() {
  ensureOrder();
  if (!shTok)   { shTok   = mkStyle('tokens'); }
  if (!shGeom)  { shGeom  = mkStyle('geom');  }
  if (!shOpt)   { shOpt   = mkStyle('opt');   }
  if (!shPaint) { shPaint = mkStyle('paint'); }
}

/* reader.css, injected by us when Python hands us its text or its URL
   (webhost normally injects it itself; this path is for other hosts). */
function ensureBaseCSS(cfg) {
  if (!currentDE()) { return; }
  if (doc.querySelector('[data-er="base"]')) { return; }
  ensureOrder();
  if (cfg && cfg.cssText) {
    var s = doc.createElementNS(NS_XHTML, 'style');
    s.setAttribute('data-er', 'base');
    s.textContent = cfg.cssText;
    de.insertBefore(s, shOrder.nextSibling);
  } else if (cfg && cfg.cssHref) {
    var l = doc.createElementNS(NS_XHTML, 'link');
    l.setAttribute('data-er', 'base');
    l.setAttribute('rel', 'stylesheet');
    l.setAttribute('href', cfg.cssHref);
    de.insertBefore(l, shOrder.nextSibling);
  }
}

/* ===========================================================================
   3. SETTINGS, FONTS, THEMES
   Keys are the literal settings.json `reader` block from the product spec so
   store.py values can be handed straight through.
   =========================================================================== */
var DEFAULTS = {
  theme: 'day',                /* 'day' | 'paper' | 'night' (resolve 'system' in Python) */
  colors: null,                /* optional theme.py token dict, see THEME TOKENS */
  layout: 'paged',             /* 'paged' | 'scroll'                            */
  font_cjk: 'Microsoft YaHei',
  font_latin: 'Georgia',
  use_book_fonts: false,
  font_size_px: 21,            /* 161 PPI, dpr 1.0 -> 16px is too small here    */
  font_weight: 0,              /* 0 = leave the book's weights alone            */
  line_height: 1.9,            /* CJK-friendly; 1.75 is the Latin figure        */
  para_spacing_em: null,       /* null = keep the book's paragraph spacing      */
  text_indent_ch: null,        /* null = keep the book's indents (poetry!)      */
  text_align: null,            /* null = keep the book's alignment              */
  page_margin_px: 64,          /* side margin; the column gap is twice this     */
  max_measure_ch: 0,           /* 0 = fill the window                           */
  image_click_zoom: true,
  invert_images_in_dark: false,
  hyphens: 'manual',
  highlight_colors: null
};

/* Only families that are ACTUALLY INSTALLED on this machine.  There is no
   Source Han and no Noto CJK here; naming them silently falls back to a
   default face and the user sees a font change that never happened. */
var CJK_ALIAS = {
  'Microsoft YaHei':    ['Microsoft YaHei', 'Microsoft YaHei UI', '微软雅黑'],
  'SimSun':             ['SimSun', 'NSimSun', '宋体'],
  'SimHei':             ['SimHei', '黑体'],
  'KaiTi':              ['KaiTi', '楷体'],
  'FangSong':           ['FangSong', '仿宋'],
  'DengXian':           ['DengXian', '等线'],
  'Microsoft JhengHei': ['Microsoft JhengHei', 'Microsoft JhengHei UI', '微軟正黑體']
};
var LATIN_ALIAS = {
  'Georgia':           ['Georgia'],
  'Cambria':           ['Cambria'],
  'Constantia':        ['Constantia'],
  'Palatino Linotype': ['Palatino Linotype'],
  'Segoe UI':          ['Segoe UI Variable Text', 'Segoe UI'],
  'Consolas':          ['Consolas']
};
var LATIN_SERIF = { 'Georgia': 1, 'Cambria': 1, 'Constantia': 1, 'Palatino Linotype': 1 };
var CJK_SERIF   = { 'SimSun': 1, 'KaiTi': 1, 'FangSong': 1 };

function quoteFamily(name) {
  return /^[A-Za-z][A-Za-z0-9-]*$/.test(name) ? name : '"' + name + '"';
}
function fontStack(latin, cjk) {
  var out = [];
  var i, a;
  a = LATIN_ALIAS[latin] || (latin ? [latin] : []);
  for (i = 0; i < a.length; i++) { out.push(quoteFamily(a[i])); }
  a = CJK_ALIAS[cjk] || (cjk ? [cjk] : []);
  for (i = 0; i < a.length; i++) { out.push(quoteFamily(a[i])); }
  var generic = (LATIN_SERIF[latin] || CJK_SERIF[cjk]) ? 'serif' : 'sans-serif';
  if (!out.length) { return generic; }
  out.push(generic);
  return out.join(',');
}
function monoStack(cjk) {
  var out = ['Consolas', '"Cascadia Mono"'];
  var a = CJK_ALIAS[cjk] || [];
  for (var i = 0; i < a.length; i++) { out.push(quoteFamily(a[i])); }
  out.push('monospace');
  return out.join(',');
}

/* Mobile font routing keeps the text DOM intact: unicode-range separates Latin
   from CJK even when the selected Chinese font also contains Latin glyphs.
   Han shapes shared by Chinese/Japanese/Korean follow inherited BCP-47 lang. */
var shMobileFonts = null;
function applyMobileScriptFonts(s, fixed) {
  if (!shMobileFonts) { shMobileFonts = mkStyle('mobile-fonts'); }
  var enabled = mobileHost && !fixed && !s.use_book_fonts &&
                Object.prototype.hasOwnProperty.call(s, 'font_hans');
  de.toggleAttribute('data-er-script-fonts', enabled);
  if (!enabled) { shMobileFonts.textContent = ''; return; }
  var faces = Array.isArray(s.font_faces) ? s.font_faces : [];
  var han = 'U+2E80-2FFF,U+3000-303F,U+31C0-31EF,U+3400-4DBF,U+4E00-9FFF,' +
            'U+F900-FAFF,U+FE10-FE1F,U+FE30-FE4F,U+FF00-FFEF,U+20000-323AF';
  var ranges = {
    latin:'U+0000-052F,U+1E00-2BFF',
    hans:han, hant:han,
    japanese:han+',U+3040-30FF,U+31F0-31FF,U+1B000-1B16F',
    korean:han+',U+1100-11FF,U+3130-318F,U+A960-A97F,U+AC00-D7FF'
  };
  var auto = {latin:'serif',hans:'ER Fandol Song',hant:'ER Fandol Song',
              japanese:'sans-serif',korean:'sans-serif'};
  var systemNames = {
    latin:{serif:['Noto Serif','Droid Serif'], 'sans-serif':['Roboto','Noto Sans'], monospace:['Droid Sans Mono','Noto Sans Mono']},
    hans:{serif:['Noto Serif CJK SC'], 'sans-serif':['Noto Sans CJK SC'], monospace:['Noto Sans Mono CJK SC','Noto Sans CJK SC']},
    hant:{serif:['Noto Serif CJK TC'], 'sans-serif':['Noto Sans CJK TC'], monospace:['Noto Sans Mono CJK TC','Noto Sans CJK TC']},
    japanese:{serif:['Noto Serif CJK JP'], 'sans-serif':['Noto Sans CJK JP'], monospace:['Noto Sans Mono CJK JP','Noto Sans CJK JP']},
    korean:{serif:['Noto Serif CJK KR'], 'sans-serif':['Noto Sans CJK KR'], monospace:['Noto Sans Mono CJK KR','Noto Sans CJK KR']}
  };
  var q = function (v) { return '"'+String(v).replace(/\\/g,'\\\\').replace(/"/g,'\\"').replace(/[\r\n\f]/g,' ')+'"'; };
  var aliases={}, rules=[];
  Object.keys(ranges).forEach(function (script) {
    var family = String(s['font_'+script] || auto[script]);
    var alias = 'ER Mobile '+script;
    aliases[script]=q(alias);
    var matched=faces.filter(function(f){return f && f.family===family &&
      typeof f.url==='string' && /^\/__er_fonts\/(?:user\/)?[A-Za-z0-9_.-]+$/.test(f.url);});
    if (matched.length) {
      matched.forEach(function(f){
        var weight=Math.max(100,Math.min(900,Number(f.weight)||400));
        if (Array.isArray(f.weightRange) && f.weightRange.length===2) {
          var lo=Math.max(100,Math.min(900,Number(f.weightRange[0])||400));
          var hi=Math.max(100,Math.min(900,Number(f.weightRange[1])||400));
          if (lo<hi) { weight=lo+' '+hi; }
        }
        var style=f.style==='italic'||f.italic?'italic':'normal';
        rules.push('@font-face{font-family:'+q(alias)+';src:url('+q(f.url)+');font-weight:'+weight+
          ';font-style:'+style+';font-display:swap;unicode-range:'+ranges[script]+';}');
      });
    } else {
      var names=(systemNames[script]||{})[family] || [family];
      var src=names.map(function(n){return 'local('+q(n)+')';}).join(',');
      rules.push('@font-face{font-family:'+q(alias)+';src:'+src+';font-weight:100 900;'+
        'font-style:normal;font-display:swap;unicode-range:'+ranges[script]+';}');
    }
  });
  function stack(language) {
    var order=['latin',language,'japanese','korean','hans','hant'];
    return order.filter(function(k,i){return order.indexOf(k)===i;}).map(function(k){return aliases[k];}).join(',')+',serif';
  }
  var tag=(de.getAttribute('lang')||de.getAttribute('xml:lang')||doc.body&&doc.body.getAttribute('lang')||'').toLowerCase();
  var base=/^ja(?:-|$)/.test(tag)?'japanese':/^ko(?:-|$)/.test(tag)?'korean':
    /^zh-(?:hant|tw|hk|mo)(?:-|$)/.test(tag)?'hant':'hans';
  de.style.setProperty('--er-ff',stack(base));
  var scope=':root[data-er-script-fonts]:not([data-er-fxl])';
  var fontRules=[scope+'{--er-mobile-font:'+stack(base)+';}'];
  [['zh','hans'],['zh-Hans','hans'],['zh-CN','hans'],['zh-SG','hans'],
   ['zh-Hant','hant'],['zh-TW','hant'],['zh-HK','hant'],['zh-MO','hant'],
   ['ja','japanese'],['ko','korean']].forEach(function(pair){
    fontRules.push(scope+':lang('+pair[0]+'),'+scope+' :lang('+pair[0]+'){--er-mobile-font:'+stack(pair[1])+';}');
  });
  // Selected typefaces are an explicit reader preference. Preserve semantic
  // weights/styles and code/MathML fonts, not publisher family declarations.
  fontRules.push(scope+','+scope+' body,'+scope+' body *:not(code):not(pre):not(kbd):not(samp):not(tt):not(math):not(math *):not(svg):not(svg *)'+
    ':not(code *):not(pre *):not(kbd *):not(samp *):not(tt *){font-family:var(--er-mobile-font)!important;}');
  rules.push('@layer er-safety{'+fontRules.join('')+'}');
  shMobileFonts.textContent=rules.join('\n');
}

/* ===========================================================================
   THEME TOKENS  (see the table at the top of reader.css)
   Precedence: settings.colors (inline on <html>)  >  theme.py's unlayered
   token sheet  >  the built-in palette below (layered, zero specificity)  >
   reader.css defaults.  After writing, the EFFECTIVE values are read back from
   computed style, so dark detection and highlight paint follow whichever
   source actually won.
   =========================================================================== */
var TOKENS = ['bg', 'fg', 'secondary', 'accent', 'border', 'selection',
              'hl-yellow', 'hl-green', 'hl-blue', 'hl-pink',
              'find-bg', 'find-fg', 'find-active-bg', 'find-active-fg'];

/* Fallback palettes, matched to product-spec themes.json / highlight_colors.
   theme.py (owner B) normally injects the real values; see reader.css. */
var THEMES = {
  day:   { bg: '#FFFFFF', fg: '#1A1A1A', secondary: '#6B6B6B', accent: '#1155CC', border: '#E0E0E0',
           selection: '#B4D5FE', 'hl-yellow': '#FFF08A', 'hl-green': '#BDEBBD', 'hl-blue': '#B9DCF7', 'hl-pink': '#F8C6D8' },
  paper: { bg: '#F6F0E4', fg: '#33302B', secondary: '#7A7268', accent: '#8A5A22', border: '#DFD5C0',
           selection: '#E3D3AE', 'hl-yellow': '#EFDF9A', 'hl-green': '#C7DEBB', 'hl-blue': '#C2D6E2', 'hl-pink': '#E8C3C9' },
  night: { bg: '#16181C', fg: '#C9CCD1', secondary: '#7E848E', accent: '#7FB4F5', border: '#262A31',
           selection: '#2E4763', 'hl-yellow': '#6A5A18', 'hl-green': '#2F5433', 'hl-blue': '#27455C', 'hl-pink': '#5C2C3D' }
};
var FIND_FALLBACK = {
  light: { 'find-bg': '#FFD54A', 'find-fg': '#111111', 'find-active-bg': '#FF7A1A', 'find-active-fg': '#FFFFFF' },
  dark:  { 'find-bg': '#7A6210', 'find-fg': '#F2E9C8', 'find-active-bg': '#C1620A', 'find-active-fg': '#FFFFFF' }
};
/* theme.py names its themes light / sepia / dark; store.py's themes.json uses
   day / paper / night.  Both are accepted. */
var THEME_ALIAS = { light: 'day', sepia: 'paper', dark: 'night', gray: 'paper', black: 'night' };
/* Accepted spellings for each token inside settings.colors: theme.py's
   Theme.tokens() keys (hl_yellow, find_bg ...), css_variables() keys
   (--er-bg ...), or the product-spec themes.json keys (muted, link, sel ...). */
var TOKEN_KEYS = {
  bg: ['bg', 'background'], fg: ['fg', 'text', 'foreground'],
  secondary: ['secondary', 'muted'], accent: ['accent', 'link'],
  border: ['border', 'ui_line', 'line'], selection: ['selection', 'sel'],
  'hl-yellow': ['hl-yellow', 'hl_yellow', 'highlight_yellow'],
  'hl-green': ['hl-green', 'hl_green', 'highlight_green'],
  'hl-blue': ['hl-blue', 'hl_blue', 'highlight_blue'],
  'hl-pink': ['hl-pink', 'hl_pink', 'highlight_pink'],
  'find-bg': ['find-bg', 'find_bg'], 'find-fg': ['find-fg', 'find_fg'],
  'find-active-bg': ['find-active-bg', 'find_active_bg'], 'find-active-fg': ['find-active-fg', 'find_active_fg']
};

function normSettings(sIn) {
  var s = {}, k;
  for (k in DEFAULTS) { if (Object.prototype.hasOwnProperty.call(DEFAULTS, k)) { s[k] = DEFAULTS[k]; } }
  if (sIn) { for (k in sIn) { if (Object.prototype.hasOwnProperty.call(sIn, k) && sIn[k] !== undefined) { s[k] = sIn[k]; } } }
  s.theme = THEME_ALIAS[s.theme] || s.theme;
  if (!THEMES[s.theme]) { s.theme = 'day'; }
  return s;
}

function luminance(rgb) {
  var f = function (c) { c = c / 255; return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); };
  return 0.2126 * f(rgb[0]) + 0.7152 * f(rgb[1]) + 0.0722 * f(rgb[2]);
}

function explicitTokens(s) {
  var out = {}, c = (s.colors && typeof s.colors === 'object') ? s.colors : {};
  for (var i = 0; i < TOKENS.length; i++) {
    var name = TOKENS[i], keys = TOKEN_KEYS[name].concat(['--er-' + name]);
    for (var k = 0; k < keys.length; k++) {
      if (typeof c[keys[k]] === 'string' && c[keys[k]]) { out[name] = c[keys[k]]; break; }
    }
    if (!out[name] && name.indexOf('hl-') === 0) {
      var col = name.slice(3);
      if (c.hl && typeof c.hl[col] === 'string') { out[name] = c.hl[col]; }
    }
  }
  return out;
}

/* store.py's highlight_colors: {yellow:{day,paper,night}, ...}.  Keyed by the
   palette that matches the effective background. */
function highlightColorsFor(s, dark) {
  var out = {}, hc = s.highlight_colors;
  if (!hc || typeof hc !== 'object') { return out; }
  var key = THEMES[s.theme] && !s.colors ? s.theme : (dark ? 'night' : 'day');
  var names = ['yellow', 'green', 'blue', 'pink'];
  for (var i = 0; i < names.length; i++) {
    var ent = hc[names[i]];
    if (typeof ent === 'string') { out['hl-' + names[i]] = ent; }
    else if (ent && typeof ent === 'object' && (ent[key] || ent.day)) { out['hl-' + names[i]] = ent[key] || ent.day; }
  }
  return out;
}

function writeFallbackSheet(palette, find) {
  var decl = [], i, name;
  for (i = 0; i < TOKENS.length; i++) {
    name = TOKENS[i];
    decl.push('--er-' + name + ':' + (palette[name] || find[name]));
  }
  shTok.textContent = '@layer er-theme{:where(:root){' + decl.join(';') + '}}';
}

function readTokens(palette, find) {
  var cs = getComputedStyle(de), t = {};
  for (var j = 0; j < TOKENS.length; j++) {
    t[TOKENS[j]] = (cs.getPropertyValue('--er-' + TOKENS[j]) || '').trim() || palette[TOKENS[j]] || find[TOKENS[j]];
  }
  return t;
}

function applyTokens(s) {
  var palette = THEMES[s.theme] || THEMES.day;
  var guessDark = s.theme === 'night';
  var ex = explicitTokens(s), i, name;

  /* 1. low-precedence fallbacks + explicit inline values */
  writeFallbackSheet(palette, FIND_FALLBACK[guessDark ? 'dark' : 'light']);
  for (i = 0; i < TOKENS.length; i++) {
    name = TOKENS[i];
    if (ex[name]) { de.style.setProperty('--er-' + name, ex[name]); }
    else { de.style.removeProperty('--er-' + name); }
  }

  /* 2. dark or light is decided by the background that actually WON */
  var tok = readTokens(palette, FIND_FALLBACK.light);
  var dark;
  if (s.colors && (s.colors.scheme === 'dark' || s.colors.scheme === 'light')) { dark = s.colors.scheme === 'dark'; }
  else { var bgp = parseRGB(resolveColor(tok.bg)); dark = bgp ? luminance(bgp) < 0.35 : guessDark; }
  /* 3. store.py highlight_colors are palette DEFAULTS: they join the layered
        fallback sheet, so theme.py's (WCAG-checked) token sheet still wins */
  var hcs = highlightColorsFor(s, dark), merged = {}, k2;
  for (k2 in palette) { if (Object.prototype.hasOwnProperty.call(palette, k2)) { merged[k2] = palette[k2]; } }
  for (k2 in hcs) { if (Object.prototype.hasOwnProperty.call(hcs, k2)) { merged[k2] = hcs[k2]; } }
  writeFallbackSheet(merged, FIND_FALLBACK[dark ? 'dark' : 'light']);

  tok = readTokens(merged, FIND_FALLBACK[dark ? 'dark' : 'light']);
  tok.scheme = dark ? 'dark' : 'light';
  de.style.setProperty('--er-scheme', tok.scheme);
  st.tokens = tok;
  return tok;
}

/* ===========================================================================
   4. CSSOM REWRITING — the honest way to theme a book.
   Never `* { color: X !important }`: it destroys link colour, heading colour,
   code colour and any semantic colouring.  Rewrite the book's own rules.
   =========================================================================== */
var BOOK_SHEETS = [];

/* Our own sheets must never be rewritten as if they were the book's:
   - anything we created carries data-er;
   - reader.css (webhost injects it WITHOUT data-er) declares the er- layers;
   - theme.py's token sheet declares nothing but --er-* custom properties.
   Without this, releaseRootTypography() would strip reader.css's own
   `:root{font-size:var(--er-fs)}` and the font-size setting would do nothing. */
function isOwnSheet(sh) {
  var on = sh.ownerNode;
  if (on && on.getAttribute && on.getAttribute('data-er') !== null) { return true; }
  var rules;
  try { rules = sh.cssRules; } catch (e) { return true; /* unreadable: cannot rewrite anyway */ }
  if (!rules || !rules.length) { return false; }
  for (var i = 0; i < rules.length && i < 6; i++) {
    var r = rules[i];
    if (r.nameList && r.nameList.length && String(r.nameList[0]).indexOf('er-') === 0) { return true; }
    if (typeof r.name === 'string' && r.cssRules && r.name.indexOf('er-') === 0) { return true; }
  }
  var decls = 0, foreign = 0;
  eachStyleRule(rules, function (sr) {
    for (var k = 0; k < sr.style.length; k++) {
      decls++;
      if (String(sr.style[k]).indexOf('--er-') !== 0) { foreign++; }
    }
  });
  return decls > 0 && foreign === 0;
}

function collectSheets() {
  BOOK_SHEETS.length = 0;
  var list = doc.styleSheets;
  for (var i = 0; i < list.length; i++) {
    var sh = list[i];
    if (isOwnSheet(sh)) { continue; }
    try { void sh.cssRules; BOOK_SHEETS.push(sh); } catch (e) { /* unreadable */ }
  }
  return BOOK_SHEETS.length;
}

/* LOAD-BEARING (pitfall 7): in Chromium >= 112 a CSSStyleRule ALSO exposes
   .cssRules because of CSS nesting, so `if (r.cssRules) { recurse; continue; }`
   silently skips EVERY style rule.  Test r.style first; recurse only when
   there really are nested rules. */
function eachStyleRule(rules, fn) {
  if (!rules) { return; }
  for (var i = 0; i < rules.length; i++) {
    var r = rules[i];
    if (r.style) { fn(r); }
    if (r.cssRules && r.cssRules.length) { eachStyleRule(r.cssRules, fn); }
  }
}

var ROOT_SEL  = /(^|,)\s*(html|body|:root)\s*(,|$)/i;
var PARA_SEL  = /(^|,)[^,]*\bp\b(\.[\w-]+)?\s*(,|$)/i;

/* Mobile image sizing. Save the author's dimensions BEFORE the geometry
   safety sheet resets images to auto. Unsized formula series use one shared
   em/pixel ratio, never a fixed total height per formula. */
var mobileHost = false, formulaScales = {}, imageSizing = new WeakMap();
function imageDeclarations() {
  var rules = [], order = 0;
  function read(rs) {
    for (var i = 0; rs && i < rs.length; i++) {
      var r = rs[i];
      if (r.media && r.conditionText && !window.matchMedia(r.conditionText).matches) { continue; }
      if (r.style && r.selectorText && ['width','height','max-width','max-height'].some(function (k) {
        return !!r.style.getPropertyValue(k);
      })) { rules.push({ rule:r, order:order++ }); }
      if (r.cssRules) { read(r.cssRules); }
    }
  }
  BOOK_SHEETS.forEach(function (s) { try { read(s.cssRules); } catch (e) {} });
  return rules;
}
function selectorWeight(selector) {
  var s = selector.replace(/:where\([^)]*\)/g, '');
  return (s.match(/#[\w-]+/g) || []).length * 1000000 +
    (s.match(/\.[\w-]+|\[[^\]]*\]|:(?!:)[\w-]+/g) || []).length * 1000 +
    (s.replace(/#[\w-]+|\.[\w-]+|\[[^\]]*\]|:[\w-]+(?:\([^)]*\))?/g, '').match(/[A-Za-z][\w-]*/g) || []).length;
}
function captureImageSizing() {
  if (!mobileHost || st.fxl) { return; }
  if (!Array.prototype.some.call(doc.images, function (img) { return !imageSizing.has(img); })) { return; }
  var rules = imageDeclarations(), props = ['width','height','max-width','max-height'];
  var own = [], initial = !st.inited;
  if (initial) {
    Array.prototype.forEach.call(doc.styleSheets, function (s) {
      if (isOwnSheet(s) && !s.disabled) { own.push(s); s.disabled = true; }
    });
  }
  try {
    Array.prototype.forEach.call(doc.images, function (img) {
      if (img.closest('#er-zoom,#er-pop')) { return; }
      if (imageSizing.has(img)) { return; }
      var values = {}, ranks = {}, cs = getComputedStyle(img), font = parseFloat(cs.fontSize) || 16;
      props.forEach(function (k) {
        var v = img.getAttribute(k);
        if (v) { values[k] = /^\d+(\.\d+)?$/.test(v) ? v + 'px' : v; ranks[k] = -1; }
      });
      function take(style, score) {
        props.forEach(function (k) {
          var value = style.getPropertyValue(k), rank = score + (style.getPropertyPriority(k) ? 1e12 : 0);
          if (value && (ranks[k] === undefined || rank >= ranks[k])) { values[k] = value; ranks[k] = rank; }
        });
      }
      rules.forEach(function (entry) {
        entry.rule.selectorText.split(',').forEach(function (selector) {
          try { if (img.matches(selector)) { take(entry.rule.style, selectorWeight(selector) * 10000 + entry.order); } }
          catch (e) { /* unsupported selector: retain the browser's original rule */ }
        });
      });
      take(img.style, 1e11);
      props.forEach(function (k) {
        var v = (values[k] || '').trim(), m = /^([\d.]+)(px|pt|pc|in|cm|mm)$/i.exec(v);
        if (m) {
          var px = Number(m[1]) * ({px:1,pt:4/3,pc:16,in:96,cm:96/2.54,mm:96/25.4})[m[2].toLowerCase()];
          values[k] = (px / font).toFixed(6) + 'em';
        }
      });
      imageSizing.set(img, { values:values, candidate:null });
    });
  } finally { own.forEach(function (s) { s.disabled = false; }); }
}
function formulaCandidate(img, record) {
  if (record.candidate !== null) { return record.candidate; }
  var w = img.naturalWidth, h = img.naturalHeight;
  if (!img.complete || !w) { return false; }
  record.candidate = false;
  if (h < 8 || h > 300 || w < 3 || w > 1800 || w*h > 300000) { return false; }
  try {
    var canvas = doc.createElement('canvas');
    var scale = Math.min(1, 80 / Math.max(w,h));
    canvas.width = Math.max(1, Math.round(w*scale)); canvas.height = Math.max(1, Math.round(h*scale));
    var ctx = canvas.getContext('2d', {willReadFrequently:true});
    ctx.fillStyle = '#fff'; ctx.fillRect(0,0,canvas.width,canvas.height);
    ctx.drawImage(img,0,0,canvas.width,canvas.height);
    var data = ctx.getImageData(0,0,canvas.width,canvas.height).data, ink = 0, colour = 0;
    for (var i=0;i<data.length;i+=4) {
      if (Math.max(data[i],data[i+1],data[i+2])-Math.min(data[i],data[i+1],data[i+2])>35) { colour++; }
      if ((data[i]+data[i+1]+data[i+2])/3 < 160) { ink++; }
    }
    var total = data.length/4;
    record.candidate = colour <= total*.02 && ink > total*.01 && ink < total*.35;
  } catch (e) { /* foreign or undecodable pictures keep source dimensions */ }
  return record.candidate;
}
function applyReaderImageSizing() {
  if (!mobileHost || st.fxl) { return; }
  captureImageSizing();
  Array.prototype.forEach.call(doc.images, function (img) {
    if (img.closest('#er-zoom,#er-pop')) { return; }
    var record = imageSizing.get(img); if (!record) { return; }
    var v = record.values, width = v.width, height = v.height;
    var explicit = (width && width !== 'auto') || (height && height !== 'auto');
    var scale = 0;
    try {
      var path = decodeURIComponent(new URL(img.currentSrc || img.src, doc.baseURI).pathname).replace(/^\//,'').toLowerCase();
      scale = Number(formulaScales[path.replace(/\d+/g,'#')]) || 0;
    } catch (e) {}
    if (!explicit && scale && img.naturalHeight * scale <= 12 && formulaCandidate(img,record)) {
      width = (img.naturalWidth*scale).toFixed(6) + 'em'; height = 'auto';
      img.setAttribute('data-er-formula','estimated');
    } else if (!explicit) { return; }
    // Constrain with one axis. CSS max-width plus a definite height otherwise
    // squeezes a fraction horizontally when it meets the phone's page edge.
    if ((!width || width === 'auto') && height && height !== 'auto' && img.naturalHeight) {
      var hm = /^([\d.]+)(em|ex|rem|px|pt|cm|mm|in|%)$/.exec(height);
      if (hm && hm[2] !== '%') {
        width = (Number(hm[1]) * img.naturalWidth / img.naturalHeight).toFixed(6) + hm[2];
        height = 'auto';
      }
    }
    img.setAttribute('data-er-image-size','');
    img.style.setProperty('--er-image-width',width || 'auto');
    img.style.setProperty('--er-image-height',height || 'auto');
    ['max-width','max-height'].forEach(function (k) {
      if (v[k] && v[k] !== 'none') { img.style.setProperty('--er-image-' + k,v[k]); }
    });
  });
}

/* 4a. px/pt font-size -> rem, so the user's size scales the BOOK'S hierarchy.
       Verified: the h1:body ratio stayed exactly 2.000 from 16px to 26px.
       Tracked per sheet, so a stylesheet that finishes loading after init() is
       still normalised on the next applySettings(). */
var fontSized = (typeof WeakSet === 'function') ? new WeakSet() : null;
function normaliseFontSizes() {
  var n = 0;
  for (var i = 0; i < BOOK_SHEETS.length; i++) {
    var sh = BOOK_SHEETS[i];
    if (fontSized) { if (fontSized.has(sh)) { continue; } fontSized.add(sh); }
    eachStyleRule(sh.cssRules, function (r) {
      var v = r.style.getPropertyValue('font-size');
      if (!v) { return; }
      var m = /^\s*([\d.]+)(px|pt)\s*$/.exec(v);
      if (!m) { return; }
      var px = m[2] === 'pt' ? parseFloat(m[1]) * 4 / 3 : parseFloat(m[1]);
      r.style.setProperty('font-size', (px / 16).toFixed(4) + 'rem',
                          r.style.getPropertyPriority('font-size'));
      n++;
    });
  }
  return n;
}

/* 4b. Lift ONLY the book's ROOT typography, reversibly, so the user's font and
       line-height reach body text while pre{font-family:monospace} and other
       intentional choices survive. */
var famUndo = [];
function releaseRootTypography(on) {
  var props = ['font-family', 'line-height', 'text-align', 'font-size'];
  if (on) {
    if (famUndo.length) { return 0; }
    for (var i = 0; i < BOOK_SHEETS.length; i++) {
      eachStyleRule(BOOK_SHEETS[i].cssRules, function (r) {
        if (!ROOT_SEL.test(r.selectorText || '')) { return; }
        for (var j = 0; j < props.length; j++) {
          var p = props[j], v = r.style.getPropertyValue(p);
          if (v) { famUndo.push([r, p, v, r.style.getPropertyPriority(p)]); r.style.removeProperty(p); }
        }
      });
    }
    return famUndo.length;
  }
  var u = famUndo.splice(0).reverse();
  for (var k = 0; k < u.length; k++) { u[k][0].style.setProperty(u[k][1], u[k][2], u[k][3]); }
  return 0;
}

/* 4c. Lift the book's paragraph spacing / first-line indent, but ONLY when the
       user explicitly overrode them.  Default is null => the book keeps its
       poetry indents and its hanging-indent designs. */
var paraUndo = [];
function releaseParagraphStyle(props) {
  if (props && props.length) {
    if (paraUndo.length) { return 0; }
    for (var i = 0; i < BOOK_SHEETS.length; i++) {
      eachStyleRule(BOOK_SHEETS[i].cssRules, function (r) {
        if (!PARA_SEL.test(r.selectorText || '')) { return; }
        for (var j = 0; j < props.length; j++) {
          var p = props[j], v = r.style.getPropertyValue(p);
          if (v) { paraUndo.push([r, p, v, r.style.getPropertyPriority(p)]); r.style.removeProperty(p); }
        }
      });
    }
    return paraUndo.length;
  }
  var u = paraUndo.splice(0).reverse();
  for (var k = 0; k < u.length; k++) { u[k][0].style.setProperty(u[k][1], u[k][2], u[k][3]); }
  return 0;
}

/* 4d. Dark mode: hue-preserving lightness flip of the BOOK'S declarations.
       Measured contrast went from 1.13:1 (broken) to 11.55:1. */
var COLOR_PROPS = ['color', 'background-color', 'border-color', 'border-top-color',
  'border-right-color', 'border-bottom-color', 'border-left-color', 'outline-color',
  'text-decoration-color', 'column-rule-color', 'caret-color', 'fill', 'stroke'];
var colorUndo = [];
var _probe = null;

function resolveColor(v) {
  if (!_probe) { _probe = doc.createElement('span'); _probe.setAttribute('data-er', 'probe'); }
  _probe.style.cssText = '';
  _probe.style.setProperty('color', v);
  if (!_probe.style.color) { return null; }
  de.appendChild(_probe);
  var c = getComputedStyle(_probe).color;
  _probe.remove();
  return c;
}
function parseRGB(c) {
  var m = /^rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)(?:[,/\s]+([\d.%]+))?\s*\)$/.exec(c || '');
  if (!m) { return null; }
  var a = m[4] === undefined ? 1 : (String(m[4]).indexOf('%') >= 0 ? parseFloat(m[4]) / 100 : +m[4]);
  return [+m[1], +m[2], +m[3], a];
}
function flipLightness(css, D) {
  var p = parseRGB(resolveColor(css));
  if (!p) { return null; }
  var r = p[0], g = p[1], b = p[2], a = p[3];
  if (a === 0) { return null; }
  var rr = r / 255, gg = g / 255, bb = b / 255;
  var mx = Math.max(rr, gg, bb), mn = Math.min(rr, gg, bb), d = mx - mn;
  var h = 0, s = 0, l = (mx + mn) / 2;
  if (d) {
    s = l > 0.5 ? d / (2 - mx - mn) : d / (mx + mn);
    h = mx === rr ? ((gg - bb) / d + (gg < bb ? 6 : 0)) : mx === gg ? ((bb - rr) / d + 2) : ((rr - gg) / d + 4);
    h *= 60;
  }
  var nl = D.hi - l * (D.hi - D.lo);
  var ns = Math.min(s, 0.5);
  return 'hsl(' + h.toFixed(0) + ' ' + (ns * 100).toFixed(0) + '% ' + (nl * 100).toFixed(0) + '%' +
         (a < 1 ? ' / ' + a.toFixed(2) : '') + ')';
}
function recolourBook(on, tokens) {
  var i, u, k;
  if (!on) {
    u = colorUndo.splice(0).reverse();
    for (k = 0; k < u.length; k++) { u[k][0].style.setProperty(u[k][1], u[k][2], u[k][3]); }
    return 0;
  }
  if (colorUndo.length) { return 0; }
  var lo = 0.12, hi = 0.92;
  if (tokens) {
    var bgp = parseRGB(resolveColor(tokens.bg));
    var fgp = parseRGB(resolveColor(tokens.fg));
    if (bgp) { lo = Math.max(0.05, Math.min(0.30, (bgp[0] + bgp[1] + bgp[2]) / 765 + 0.03)); }
    if (fgp) { hi = Math.max(0.70, Math.min(0.96, (fgp[0] + fgp[1] + fgp[2]) / 765 + 0.08)); }
  }
  var D = { hi: hi, lo: lo };
  var n = 0;
  for (i = 0; i < BOOK_SHEETS.length; i++) {
    eachStyleRule(BOOK_SHEETS[i].cssRules, function (r) {
      /* LOAD-BEARING (pitfall 8): writing the `background` shorthand expands it
         into `background-color`, so a read-modify-write loop over
         [background, background-color, ...] flips the same colour TWICE
         (#eee came out light grey in dark mode).  Snapshot every declaration of
         the rule BEFORE writing any of them. */
      var snap = [];
      var bg = r.style.getPropertyValue('background');
      if (bg) { snap.push(['background', bg, r.style.getPropertyPriority('background')]); }
      for (var j = 0; j < COLOR_PROPS.length; j++) {
        var p = COLOR_PROPS[j];
        if (bg && p === 'background-color') { continue; }
        var v = r.style.getPropertyValue(p);
        if (!v || v === 'transparent' || v === 'currentcolor' || /var\(/i.test(v)) { continue; }
        snap.push([p, v, r.style.getPropertyPriority(p)]);
      }
      for (var q = 0; q < snap.length; q++) {
        var pp = snap[q][0], vv = snap[q][1], pr = snap[q][2];
        if (pp === 'background' && /url\(/i.test(vv)) {
          /* keep the image, kill the tint */
          colorUndo.push([r, 'background', vv, pr]);
          r.style.setProperty('background-color', 'transparent', pr);
          n++; continue;
        }
        var nv = flipLightness(vv, D);
        if (!nv) {
          if (pp === 'background') {
            colorUndo.push([r, pp, vv, pr]);
            r.style.setProperty(pp, 'transparent', pr);
            n++;
          }
          continue;
        }
        colorUndo.push([r, pp, vv, pr]);
        r.style.setProperty(pp, nv, pr);
        n++;
      }
    });
  }
  return n;
}

/* ===========================================================================
   5. ONE-TIME DOM DEFENCES — what CSS alone cannot fix.
   =========================================================================== */
var defended = false;
function defend() {
  if (defended || !doc.body) { return; }
  defended = true;
  var els = doc.querySelectorAll('body *');
  var cap = Math.min(els.length, 20000);
  for (var i = 0; i < cap; i++) {
    var el = els[i], cs = getComputedStyle(el);
    /* position:fixed floats over EVERY page (verified: identical viewport x on
       page 0 and page 2).  As absolute it belongs to one page and scrolls away. */
    if (cs.position === 'fixed') { el.style.setProperty('position', 'absolute', 'important'); }
    /* Chromium IGNORES break-before/after:page inside multicol.  getComputedStyle
       still reports the author's intent, so translate it to `column`. */
    if (cs.breakBefore === 'page') { el.style.setProperty('break-before', 'column', 'important'); }
    if (cs.breakAfter  === 'page') { el.style.setProperty('break-after',  'column', 'important'); }
  }
  try { de.style.setProperty('overflow-anchor', 'none'); } catch (e) { /* ignore */ }
}

function tameWideBlocks() {
  var ts = doc.querySelectorAll('table');
  for (var i = 0; i < ts.length; i++) {
    var t = ts[i];
    if (t.parentElement && t.parentElement.classList.contains('er-hscroll')) { continue; }
    if (t.getBoundingClientRect().width > st.colW + 1) {
      var w = doc.createElement('div');
      w.className = 'er-hscroll';
      w.setAttribute('data-er', 'hscroll');
      t.parentNode.insertBefore(w, t);
      w.appendChild(t);
    }
  }
}

/* ===========================================================================
   6. GEOMETRY AND PAGINATION
   =========================================================================== */
function unitPx() {
  var host = doc.body || de;
  var p = doc.createElement('div');
  p.setAttribute('data-er', 'probe');
  p.style.cssText = 'position:absolute!important;top:-99999px!important;left:-99999px!important;' +
                    'visibility:hidden!important;pointer-events:none!important;' +
                    'width:1ch!important;height:1em!important;padding:0!important;' +
                    'margin:0!important;border:0!important;';
  host.appendChild(p);
  var r = p.getBoundingClientRect();
  var out = { ch: r.width || 10, em: r.height || 21 };
  p.remove();
  return out;
}

/* Han-dominant documents measure a "character" as 1em (one 字), Latin ones as
   1ch.  40 characters then means the same thing to a Chinese and an English
   reader, which is what the setting name promises. */
function measureLimitPx() {
  var s = st.settings;
  if (!s || !s.max_measure_ch || s.max_measure_ch <= 0) { return 0; }
  var u = unitPx();
  var han = hanDominant();
  return Math.round(s.max_measure_ch * (han ? u.em : u.ch));
}
var _han = null;
function hanDominant() {
  if (_han !== null) { return _han; }
  var t = st.flat ? st.flat.text : (doc.body ? doc.body.textContent : '');
  var sample = t.slice(0, 4000);
  var m = sample.match(/[㐀-鿿豈-﫿]/g);
  var letters = sample.match(/[A-Za-z㐀-鿿豈-﫿]/g);
  _han = !!(m && letters && m.length * 2 > letters.length);
  return _han;
}

function measure() {
  var vs = viewportSize();
  st.W = vs[0];
  st.H = vs[1];

  var s = st.settings || DEFAULTS;
  var dirRTL = getComputedStyle(de).direction === 'rtl';
  st.rtl = dirRTL;

  /* INVARIANT: half = padding-inline, gap = 2*half, colW = W - gap.
     colW + gap === W exactly, therefore the column step is exactly W.
     All integers, so there is no sub-pixel drift to accumulate. */
  var half = Math.round(s.page_margin_px);
  var lim = measureLimitPx();
  if (lim > 0 && lim < st.W) { half = Math.max(half, Math.floor((st.W - lim) / 2)); }
  var maxHalf = Math.max(0, Math.floor((st.W - MIN_COL) / 2));
  half = Math.max(0, Math.min(half, maxHalf));

  st.half = half;
  st.gap  = half * 2;
  st.colW = st.W - st.gap;
  st.step = st.W;

  st.padT = Math.max(0, Math.min(Math.round(s.page_margin_px), Math.floor(st.H / 4)));
  st.padB = st.padT;
  st.pageH = Math.max(1, st.H - st.padT - st.padB);
  de.style.setProperty('--er-image-page-height', st.mode === 'paginated' ? st.pageH + 'px' : '100000px');
}

/* Page turns are instant: a book's `html{scroll-behavior:smooth}` would
   otherwise animate every programmatic scroll (also pinned in reader.css). */
var NO_ANIM = 'scroll-behavior:auto!important;scroll-snap-type:none!important;';

function paginatedCSS() {
  return '@layer er-safety{' +
  'html{margin:0!important;padding:0!important;border:0!important;float:none!important;' +
       'width:' + st.W + 'px!important;height:' + st.H + 'px!important;' +
       'min-width:0!important;max-width:none!important;min-height:0!important;max-height:none!important;' +
       /* overflow:hidden, NOT overflow:clip - clip kills programmatic scrollLeft */
       'overflow:hidden!important;position:static!important;' +
       'columns:auto!important;column-count:auto!important;column-width:auto!important;' +
       'background:var(--er-bg)!important;color:var(--er-fg)!important;' +
       'writing-mode:horizontal-tb!important;' + NO_ANIM +
       'transform:none!important;zoom:1!important;}' +
  /* The tail spacer.  Without it maxScroll = N*step - gap/2 - W, so the last
     page is short by gap/2 and its text touches the right edge.  html{padding-
     right} does NOT extend the scroll range; an absolutely positioned spacer
     does.  Mirrored for RTL, whose scrollLeft runs negative. */
  'html::after{content:\'\'!important;display:block!important;position:absolute!important;' +
       'top:0!important;left:var(--er-tail-left,0px)!important;' +
       'width:var(--er-tail,0px)!important;height:1px!important;' +
       'visibility:hidden!important;pointer-events:none!important;}' +
  'body{box-sizing:border-box!important;margin:0!important;border:0!important;float:none!important;' +
       'width:' + st.W + 'px!important;height:' + st.H + 'px!important;' +
       'min-width:0!important;max-width:none!important;min-height:0!important;max-height:none!important;' +
       'padding:' + st.padT + 'px ' + st.half + 'px ' + st.padB + 'px!important;' +
       'column-width:' + st.colW + 'px!important;column-count:auto!important;' +
       'column-gap:' + st.gap + 'px!important;column-fill:auto!important;column-rule:none!important;' +
       'overflow:visible!important;position:static!important;display:block!important;' +
       'background:transparent!important;transform:none!important;zoom:1!important;' +
       'writing-mode:horizontal-tb!important;' + NO_ANIM + '}' +
  /* BOTH width:auto and height:auto are required or a book's img{width:100%}
     plus our max-height destroys the aspect ratio (900x1400 became 836x604). */
  'img,svg,video,canvas,object,embed,iframe,picture{' +
       'max-width:100%!important;max-height:' + st.pageH + 'px!important;' +
       'width:auto!important;height:auto!important;object-fit:contain!important;' +
       'box-sizing:border-box!important;position:static!important;}' +
  '}';
}

function scrolledCSS() {
  return '@layer er-safety{' +
  'html{margin:0!important;padding:0!important;border:0!important;' +
       'width:' + st.W + 'px!important;height:auto!important;min-height:100%!important;' +
       'overflow-x:hidden!important;overflow-y:auto!important;position:static!important;' +
       'columns:auto!important;column-count:auto!important;column-width:auto!important;' +
       'background:var(--er-bg)!important;color:var(--er-fg)!important;' +
       'writing-mode:horizontal-tb!important;' + NO_ANIM + '}' +
  'html::after{content:none!important;}' +
  'body{box-sizing:border-box!important;margin:0!important;border:0!important;float:none!important;' +
       'width:' + st.W + 'px!important;height:auto!important;' +
       'min-height:0!important;max-height:none!important;max-width:none!important;' +
       'padding:' + st.padT + 'px ' + st.half + 'px ' + (st.padB * 3) + 'px!important;' +
       /* LOAD-BEARING: column-count:auto, NOT 1.  column-count:1 still makes body
          a multicol container, and every break-before:column that defend()
          wrote for the book's page breaks then opens a new column off to the
          right -- whole chapters vanished sideways in scroll mode (verified). */
       'columns:auto!important;column-count:auto!important;column-width:auto!important;' +
       'overflow:visible!important;position:static!important;display:block!important;' +
       'background:transparent!important;transform:none!important;zoom:1!important;' +
       'writing-mode:horizontal-tb!important;' + NO_ANIM + '}' +
  'img,svg,video,canvas,object,embed,iframe,picture{' +
       'max-width:100%!important;width:auto!important;height:auto!important;' +
       'object-fit:contain!important;box-sizing:border-box!important;}' +
  '}';
}

/* Vertical writing mode (classical Chinese / Japanese books).  Multicol stacks
   along the block axis there, so the page model does not apply (research fact
   33); the document is shown as ONE long horizontal scroll in its own writing
   mode.  The writing mode is lifted onto <html> so the scroll origin is the
   start edge (right for vertical-rl, where scrollLeft runs negative).  The
   book's vertical text is never flattened to horizontal. */
function verticalCSS() {
  var wm = st.verticalRL ? 'vertical-rl' : 'vertical-lr';
  return '@layer er-safety{' +
  'html{margin:0!important;padding:0!important;border:0!important;' +
       'width:' + st.W + 'px!important;height:' + st.H + 'px!important;' +
       'overflow-x:auto!important;overflow-y:hidden!important;position:static!important;' +
       'columns:auto!important;column-count:auto!important;column-width:auto!important;' +
       'background:var(--er-bg)!important;color:var(--er-fg)!important;' +
       'writing-mode:' + wm + '!important;' + NO_ANIM + '}' +
  'html::after{content:none!important;}' +
  'body{box-sizing:border-box!important;margin:0!important;border:0!important;float:none!important;' +
       'height:' + st.H + 'px!important;width:auto!important;' +
       'min-width:0!important;max-width:none!important;min-height:0!important;max-height:none!important;' +
       'padding:' + st.padT + 'px ' + st.half + 'px!important;' +
       'columns:auto!important;column-count:auto!important;column-width:auto!important;' +
       'overflow:visible!important;position:static!important;display:block!important;' +
       'background:transparent!important;transform:none!important;zoom:1!important;' +
       'writing-mode:' + wm + '!important;' + NO_ANIM + '}' +
  'img,svg,video,canvas,object,embed,iframe,picture{' +
       'max-width:100%!important;max-height:' + st.pageH + 'px!important;' +
       'width:auto!important;height:auto!important;object-fit:contain!important;' +
       'box-sizing:border-box!important;}' +
  '}';
}

var sgn = function () { return st.rtl ? -1 : 1; };
var curPage = function () { return Math.round(Math.abs(SE().scrollLeft) / (st.step || 1)); };
function clampPage(i) { return Math.max(0, Math.min(st.pages - 1, i | 0)); }

/* --- scroll-mode axis helpers ------------------------------------------------
   One "position along the reading axis" abstraction for both horizontal text
   (scrollTop) and vertical text (scrollLeft; negative for vertical-rl). */
function scrollMax() {
  return st.vertical ? Math.max(0, SE().scrollWidth - SE().clientWidth)
                     : Math.max(0, SE().scrollHeight - SE().clientHeight);
}
function scrollPos() {
  if (!st.vertical) { return SE().scrollTop; }
  return st.verticalRL ? -SE().scrollLeft : SE().scrollLeft;
}
function setScrollPos(v) {
  v = Math.max(0, Math.min(scrollMax(), v));
  if (!st.vertical) { SE().scrollTop = v; }
  else { SE().scrollLeft = st.verticalRL ? -v : v; }
  st.progScroll = [SE().scrollLeft, SE().scrollTop];
  return v;
}
function viewLen() { return st.vertical ? st.W : st.H; }
function viewPad() { return st.vertical ? st.half : st.padT; }
/* distance of a rect's start / end edge from the viewport's reading start */
function rectStart(rc) {
  if (!st.vertical) { return rc.top; }
  return st.verticalRL ? (st.W - rc.right) : rc.left;
}
function rectEnd(rc) {
  if (!st.vertical) { return rc.bottom; }
  return st.verticalRL ? (st.W - rc.left) : rc.right;
}

var relayoutDepth = 0;
function relayout() {
  if (!doc.body) { return; }
  if (relayoutDepth) { return; }
  relayoutDepth++;
  try {
    ensureSheets();
    measure();

    if (st.mode === 'fixed') { applyFXL(); buildFlat(); repaint(); return; }
    de.removeAttribute('data-er-fxl');

    if (st.mode === 'scroll') {
      shGeom.textContent = st.vertical ? verticalCSS() : scrolledCSS();
      de.style.setProperty('--er-tail', '0px');
      st.pages = 1; st.page = 0;
      void SE().scrollWidth;
      buildFlat(); repaint();
      return;
    }

    shGeom.textContent = paginatedCSS();
    de.style.setProperty('--er-tail-left', st.rtl ? 'calc(-1 * var(--er-tail, 0px))' : '0px');
    de.style.setProperty('--er-tail', '0px');
    void SE().scrollWidth;                 /* force layout before measuring */

    tameWideBlocks();                    /* mutates the DOM, so do it BEFORE counting */
    void SE().scrollWidth;

    var raw = SE().scrollWidth;
    /* scrollWidth == N*step - gap/2 exactly: column boxes are always colW wide
       even when the last column is nearly empty.  Exact, not an estimate. */
    st.pages = Math.max(1, Math.round((raw + st.half) / st.step));
    de.style.setProperty('--er-tail', (st.pages * st.step) + 'px');
    void SE().scrollWidth;

    buildFlat();
    repaint();

    /* always leave the scroller on an exact page boundary */
    var p = clampPage(Math.round(Math.abs(SE().scrollLeft) / st.step));
    SE().scrollLeft = sgn() * p * st.step;
    if (SE().scrollTop) { SE().scrollTop = 0; }
    st.progScroll = [SE().scrollLeft, SE().scrollTop];
    st.page = p;
  } finally {
    relayoutDepth--;
  }
}

function gotoPageInternal(i) {
  if (st.mode === 'fixed') { st.page = 0; report(); return 0; }
  if (st.mode === 'scroll') {
    /* scrolled mode has one "page"; treat goto(n) as a viewport-length step */
    setScrollPos(i * viewLen());
    if (!restoring) { var l0 = capture(); if (l0) { st.lastLoc = l0; } }
    report();
    return 0;
  }
  i = clampPage(i);
  SE().scrollLeft = sgn() * i * st.step;   /* instant.  No animation, ever. */
  st.progScroll = [SE().scrollLeft, SE().scrollTop];
  st.page = i;
  /* Pin the locator to the last USER navigation.  Re-capturing after a restore
     re-anchors to the top of the restored page and loses a fraction of a page
     on every reflow (measured drift: char 9619 -> 6283 over four resizes). */
  if (!restoring) { var l = capture(); if (l) { st.lastLoc = l; } }
  report();
  return i;
}

/* ===========================================================================
   7. FLATTENED TEXT — the cross-engine invariant (contract section 2.1)

   MUST equal, character for character, epublib.plain_text(zip_name):
     1. text nodes in document order, starting at <body>
     2. reject text inside SCRIPT / STYLE / NOSCRIPT at ANY depth and in any
        namespace (an SVG <style> has tagName 'style', an XHTML-parsed one
        'style' too -- a case-sensitive tagName test misses both; epublib's
        parser rejects the whole subtree).  display:none is NOT excluded --
        Python cannot see computed style, so JS must not use it either.
     3. raw nodeValue concatenated: no separators, no whitespace collapsing,
        no trimming
     4. entities resolved by the parser
     5. offsets counted in UNICODE CODE POINTS, not UTF-16 code units
   =========================================================================== */
var REJECT_SEL = 'script,style,noscript';
function buildFlat() {
  var body = doc.body;
  if (!body) { st.flat = null; return; }
  var tw = doc.createTreeWalker(body, NodeFilter.SHOW_TEXT, {
    acceptNode: function (n) {
      if (!n.nodeValue) { return NodeFilter.FILTER_REJECT; }
      var pe = n.parentElement;
      if (!pe) { return NodeFilter.FILTER_REJECT; }
      if (pe.closest(REJECT_SEL)) { return NodeFilter.FILTER_REJECT; }
      return NodeFilter.FILTER_ACCEPT;
    }
  });
  var nodes = [], starts = [], solid = [], parts = [], len = 0, n;
  var index = new Map();
  while ((n = tw.nextNode())) {
    index.set(n, nodes.length);
    starts.push(len);
    /* LOAD-BEARING (pitfall 9): whitespace-only nodes between block elements
       generate NO client rects, so a page lookup returns -1.  Treating that as
       "search right" throws away half the array and capture lands on arbitrary
       content.  Keep them in the string (so a search phrase still joins across
       elements) but index only \S nodes for the binary search. */
    if (/\S/.test(n.nodeValue)) { solid.push(nodes.length); }
    nodes.push(n);
    parts.push(n.nodeValue);
    len += n.nodeValue.length;
  }
  var text = parts.join('');
  st.flat = { text: text, nodes: nodes, starts: starts, solid: solid, index: index, cp: buildCP(text) };
  _han = null;
}

/* --- UTF-16 <-> code point ------------------------------------------------
   Chromium string indices are UTF-16 code units; Python str indices are code
   points.  They diverge on astral characters, e.g. CJK ext-B U+20BB7.  gpos is
   canonically CODE POINTS (Python's unit).  When the document contains no
   surrogate pair at all -- the overwhelmingly common case -- `cp` is null and
   both conversions are the identity, costing nothing. */
function buildCP(s) {
  if (!/[\uD800-\uDBFF]/.test(s)) { return null; }
  var pairs = [];
  for (var i = 0; i < s.length; i++) {
    var c = s.charCodeAt(i);
    if (c >= 0xD800 && c <= 0xDBFF && i + 1 < s.length) {
      var d = s.charCodeAt(i + 1);
      if (d >= 0xDC00 && d <= 0xDFFF) { pairs.push(i); i++; }
    }
  }
  if (!pairs.length) { return null; }
  var cpStart = new Array(pairs.length);
  for (var k = 0; k < pairs.length; k++) { cpStart[k] = pairs[k] - k; }
  return { pairs: pairs, cpStart: cpStart, len: s.length - pairs.length };
}
function toCP(u16) {          /* UTF-16 index -> code point index */
  var m = st.flat && st.flat.cp;
  if (!m) { return u16; }
  var lo = 0, hi = m.pairs.length - 1, cnt = 0;
  while (lo <= hi) { var k = (lo + hi) >> 1; if (m.pairs[k] + 1 < u16) { cnt = k + 1; lo = k + 1; } else { hi = k - 1; } }
  return u16 - cnt;
}
function fromCP(cp) {         /* code point index -> UTF-16 index */
  var m = st.flat && st.flat.cp;
  if (!m) { return cp; }
  var lo = 0, hi = m.cpStart.length - 1, cnt = 0;
  while (lo <= hi) { var k = (lo + hi) >> 1; if (m.cpStart[k] < cp) { cnt = k + 1; lo = k + 1; } else { hi = k - 1; } }
  return cp + cnt;
}
function flatLen() {
  if (!st.flat) { return 0; }
  return st.flat.cp ? st.flat.cp.len : st.flat.text.length;
}

function locate(u16) {        /* UTF-16 index -> [textNode, offsetInNode] */
  var f = st.flat;
  if (!f || !f.nodes.length) { return [null, 0]; }
  var starts = f.starts, lo = 0, hi = starts.length - 1, a = 0;
  while (lo <= hi) { var m = (lo + hi) >> 1; if (starts[m] <= u16) { a = m; lo = m + 1; } else { hi = m - 1; } }
  return [f.nodes[a], u16 - starts[a]];
}

function firstRect(node, off, len) {
  if (!node) { return null; }
  var L = node.nodeValue.length;
  if (!L) { return null; }
  var a = Math.max(0, Math.min(off, L - 1));
  var r = doc.createRange();
  r.setStart(node, a);
  r.setEnd(node, Math.min(a + (len || 1), L));
  var rc = r.getClientRects();
  return rc.length ? rc[0] : null;
}

/* LOAD-BEARING (pitfall 4): the EXACT, unrounded scrollLeft.  The rounded form
   `curPage() + floor(rect.left/step)` drifts up to half a page right after a
   relayout because scrollLeft is then not a multiple of the NEW step. */
function pageOfRect(rc) {
  if (!rc) { return -1; }
  var d = st.rtl ? (st.W - rc.right) : rc.left;
  var docX = d + Math.abs(SE().scrollLeft);
  return Math.floor((docX + 1) / st.step);
}

/* The first client rect at or after flat index u16.  A gpos handed over from
   Python (search hit, TOC target, highlight start) may land on collapsed
   whitespace or inside display:none text, which has NO rects; walk forward to
   the next rendered character instead of giving up, then backward. */
function rectAtOrAfter(u16) {
  var f = st.flat;
  if (!f || !f.nodes.length) { return null; }
  var p = locate(u16), nd = p[0], off = p[1];
  var rc = firstRect(nd, off);
  if (rc) { return rc; }
  var ni = f.index.get(nd), v = nd.nodeValue, j, k, m;
  for (j = off + 1; j < v.length; j++) {
    if (/\S/.test(v.charAt(j))) { rc = firstRect(nd, j); if (rc) { return rc; } break; }
  }
  for (k = ni + 1; k < f.nodes.length && k - ni < 4000; k++) {
    m = /\S/.exec(f.nodes[k].nodeValue);
    if (!m) { continue; }
    rc = firstRect(f.nodes[k], m.index);
    if (rc) { return rc; }
  }
  for (k = ni - 1; k >= 0 && ni - k < 4000; k--) {
    v = f.nodes[k].nodeValue;
    for (j = v.length - 1; j >= 0; j--) {
      if (/\S/.test(v.charAt(j))) { rc = firstRect(f.nodes[k], j); if (rc) { return rc; } break; }
    }
  }
  return null;
}
function pageOfChar(u16) { return pageOfRect(rectAtOrAfter(u16)); }
function nodePages(node) {
  var r = doc.createRange();
  r.selectNodeContents(node);
  var rc = r.getClientRects();
  if (!rc.length) { return null; }
  return [pageOfRect(rc[0]), pageOfRect(rc[rc.length - 1])];
}

/* In scrolled mode "is this at or after the viewport start" is a question
   along the reading axis (y for horizontal text, x for vertical text). */
function rectAhead(rc) {
  if (!rc) { return false; }
  return st.mode === 'scroll' ? (rectEnd(rc) > 0.5) : (pageOfRect(rc) >= curPage());
}
function nodeAhead(node) {
  if (st.mode === 'scroll') {
    var r = doc.createRange(); r.selectNodeContents(node);
    var rc = r.getClientRects();
    if (!rc.length) { return null; }
    return rectEnd(rc[rc.length - 1]) > 0.5;
  }
  var pp = nodePages(node);
  if (!pp) { return null; }
  return pp[1] >= curPage();
}

/* ===========================================================================
   8. CAPTURE / RESTORE — the CFI-lite locator
   =========================================================================== */
function chapterPercentOf(cpPos) {
  var n = flatLen();
  return n ? Math.max(0, Math.min(1, cpPos / n)) : 0;
}

function capture() {
  if (!st.flat || !st.flat.nodes.length) { return null; }
  var f = st.flat, cur = curPage();

  if (st.mode === 'fixed') {
    return { gpos: 0, snippet: f.text.substr(0, 32), before: '', path: null,
             page: 0, pages: 1, pageDelta: 0, total: flatLen(),
             percent: bookPercentOf(0), chapterPercent: 0, mode: 'fixed' };
  }
  if (!f.solid.length) { return null; }

  /* Outer binary search on the node's LAST page, so a node that starts on an
     earlier page but spans onto `cur` is still selected.  Pages are monotonic
     in document order, which is what makes a binary search legal here. */
  var lo = 0, hi = f.solid.length - 1, ans = -1;
  while (lo <= hi) {
    var m = (lo + hi) >> 1;
    var ah = nodeAhead(f.nodes[f.solid[m]]);
    if (ah === true) { ans = m; hi = m - 1; } else { lo = m + 1; }
  }
  var ni = f.solid[ans < 0 ? f.solid.length - 1 : ans];
  var nd = f.nodes[ni];

  /* Inner binary search: the first character of that node that is on/after the
     viewport start. */
  var l2 = 0, h2 = nd.nodeValue.length - 1, best = 0;
  while (l2 <= h2) {
    var mm = (l2 + h2) >> 1, rc = firstRect(nd, mm);
    if (!rc) { l2 = mm + 1; continue; }
    if (rectAhead(rc)) { best = mm; h2 = mm - 1; } else { l2 = mm + 1; }
  }
  var u16 = f.starts[ni] + best;
  var gp = toCP(u16);

  /* LOAD-BEARING (pitfall 20): a page may contain no text at all (a full-page
     image).  The located character then lives on a LATER page; without a signed
     pageDelta the position creeps forward on every reflow. */
  var delta = 0;
  if (st.mode === 'paginated') {
    var locPage = pageOfRect(firstRect(nd, best));
    delta = locPage >= 0 ? cur - locPage : 0;
  }

  var extra = {};
  if (st.mode === 'scroll') {
    var mx = scrollMax();
    extra.scrollFrac = mx > 0 ? scrollPos() / mx : 0;
  }

  return {
    gpos: gp,
    snippet: f.text.substr(u16, 40),
    before: f.text.substr(Math.max(0, u16 - 20), Math.min(20, u16)),
    path: pathOf(nd, best),
    page: st.mode === 'paginated' ? cur : 0,
    pages: st.pages,
    pageDelta: delta,
    total: flatLen(),
    percent: bookPercentOf(gp),
    chapterPercent: chapterPercentOf(gp),
    mode: publicMode(),
    scrollFrac: extra.scrollFrac
  };
}

/* 'fixed' is internal; the contract's modes are 'paginated' | 'scroll'.  A
   fixed-layout page reports 'paginated' with fixedLayout:true. */
function publicMode() { return st.mode === 'scroll' ? 'scroll' : 'paginated'; }

/* A locator for an arbitrary flat index (used to pin an explicit restore). */
function locatorAt(u16, pageDelta) {
  var f = st.flat, p = locate(u16), gp = toCP(u16);
  return {
    gpos: gp,
    snippet: f.text.substr(u16, 40),
    before: f.text.substr(Math.max(0, u16 - 20), Math.min(20, u16)),
    path: p[0] ? pathOf(p[0], p[1]) : null,
    page: st.page,
    pages: st.pages,
    pageDelta: pageDelta | 0,
    total: flatLen(),
    percent: bookPercentOf(gp),
    chapterPercent: chapterPercentOf(gp),
    mode: publicMode()
  };
}

/* The position as Python should persist it: the PINNED locator (refreshed page
   fields), not a fresh page-top capture.  Re-capturing after a restore would
   re-anchor to the top of the restored page and creep (pitfall 6). */
function currentLocator() {
  if (st.mode === 'scroll') { flushScrollCapture(); }
  var l = st.lastLoc;
  if (!l) { l = capture(); if (l) { st.lastLoc = l; } return l; }
  var out = {}, k;
  for (k in l) { if (Object.prototype.hasOwnProperty.call(l, k)) { out[k] = l[k]; } }
  out.page = st.mode === 'paginated' ? st.page : 0;
  out.pages = st.pages;
  out.total = flatLen();
  out.percent = bookPercentOf(l.gpos || 0);
  out.chapterPercent = chapterPercentOf(l.gpos || 0);
  out.mode = publicMode();
  return out;
}

var restoring = false;
function restore(loc) {
  if (!loc || !st.flat) { return false; }
  restoring = true;
  var ok = false;
  try { ok = restoreInner(loc); } finally { restoring = false; }
  /* Pin the anchor: later relayouts (font, theme, resize, mode) restore THIS
     position, not the top of whatever page it landed on. */
  if (ok && st.resolvedU16 >= 0) {
    if (loc !== st.lastLoc) { st.lastLoc = locatorAt(st.resolvedU16, loc.pageDelta); }
  } else if (!ok) {
    var c = capture(); if (c) { st.lastLoc = c; }
  }
  return ok;
}

function resolveGpos(loc) {
  /* gpos (verified by snippet) -> snippet search -> path -> clamped gpos.
     Returns a UTF-16 index or -1. */
  var f = st.flat, t = f.text;
  var snip = loc.snippet || loc.text || '';
  if (typeof loc.gpos === 'number' && loc.gpos >= 0) {
    var u = fromCP(loc.gpos);
    if (u <= t.length) {
      if (!snip || t.substr(u, snip.length) === snip) { return u; }
    }
  }
  if (snip) {
    var key = snip.slice(0, 16);
    var hits = [], i = -1, guard = 0;
    while ((i = t.indexOf(key, i + 1)) >= 0 && guard++ < 4000) { hits.push(i); }
    if (hits.length) {
      var want = typeof loc.gpos === 'number' ? fromCP(loc.gpos) : 0;
      var best = hits[0];
      for (var k = 0; k < hits.length; k++) {
        if (loc.before && t.substr(Math.max(0, hits[k] - loc.before.length), loc.before.length) === loc.before) {
          return hits[k];
        }
        if (Math.abs(hits[k] - want) < Math.abs(best - want)) { best = hits[k]; }
      }
      return best;
    }
  }
  if (loc.path) {
    var nd = resolvePath(loc.path);
    if (nd) {
      var idx = st.flat.index.get(nd);
      if (idx != null) { return st.flat.starts[idx] + Math.min(loc.path.offset || 0, nd.nodeValue.length); }
    }
  }
  if (typeof loc.gpos === 'number' && loc.gpos >= 0) { return Math.min(fromCP(loc.gpos), Math.max(0, t.length - 1)); }
  return -1;
}

function restoreInner(loc) {
  var u = resolveGpos(loc);
  st.resolvedU16 = u;
  var exact = u >= 0;

  if (st.mode === 'fixed') { report(); return exact; }

  if (st.mode === 'scroll') {
    if (u >= 0) {
      var rc = rectAtOrAfter(u);
      if (rc) { setScrollPos(scrollPos() + rectStart(rc) - viewPad()); report(); return true; }
    }
    var fr = (typeof loc.scrollFrac === 'number') ? loc.scrollFrac
           : (typeof loc.chapterPercent === 'number') ? loc.chapterPercent : 0;
    setScrollPos(fr * scrollMax());
    report();
    return false;
  }

  var p = -1;
  if (u >= 0) { p = pageOfChar(u); }
  if (p < 0 && typeof loc.chapterPercent === 'number') {
    p = Math.round(loc.chapterPercent * (st.pages - 1));
    exact = false;
  }
  if (p < 0) { p = 0; exact = false; }
  if (loc.pageDelta) { p += loc.pageDelta; }
  gotoPageInternal(clampPage(p));
  return exact;
}

/* Structural path: ELEMENT-index steps up to the nearest id'd ancestor, then
   the index of the text node inside that element.  Element indices survive
   text/comment node injection, which raw childNodes indices do not.  This is a
   fallback only; gpos is the primary anchor. */
function pathOf(node, offset) {
  var steps = [], anchor = null;
  var el = node.parentElement;
  if (!el) { return { anchor: null, steps: [], tindex: 0, offset: offset }; }

  var tindex = 0, cn = el.childNodes;
  for (var i = 0; i < cn.length; i++) {
    if (cn[i] === node) { break; }
    if (cn[i].nodeType === 3) { tindex++; }
  }
  var n = el;
  while (n && n !== doc.body) {
    if (n.id) { anchor = n.id; break; }
    var par = n.parentElement;
    if (!par) { break; }
    var idx = 0, sib = n;
    while ((sib = sib.previousElementSibling)) { idx++; }
    steps.unshift(idx);
    n = par;
  }
  return { anchor: anchor, steps: steps, tindex: tindex, offset: offset };
}
function resolvePath(p) {
  if (!p) { return null; }
  var n = p.anchor ? doc.getElementById(p.anchor) : doc.body;
  if (!n) { return null; }
  var steps = p.steps || [];
  for (var i = 0; i < steps.length; i++) {
    n = n.children[steps[i]];
    if (!n) { return null; }
  }
  var want = p.tindex || 0, seen = 0, cn = n.childNodes;
  for (var j = 0; j < cn.length; j++) {
    if (cn[j].nodeType === 3) {
      if (seen === want) { return cn[j]; }
      seen++;
    }
  }
  for (var q = 0; q < cn.length; q++) { if (cn[q].nodeType === 3) { return cn[q]; } }
  return null;
}

function bookPercentOf(gp) {
  if (st.book && st.book.total > 0) {
    return Math.max(0, Math.min(1, (st.book.offset + gp) / st.book.total));
  }
  return chapterPercentOf(gp);
}

/* ===========================================================================
   9. SEARCH AND HIGHLIGHTS — CSS Custom Highlight API.
   Zero DOM mutation means zero reflow, so page numbers never shift.
   =========================================================================== */
var QUOTES = { '‘': "'", '’': "'", '“': '"', '”': '"',
               '‐': '-', '‑': '-', '‒': '-', '–': '-',
               '—': '-', '―': '-' };

/* LENGTH-PRESERVING case fold.  Plain toLowerCase() can change a string's
   length (U+0130 -> "i" + U+0307), which would silently shift every offset
   after it.  Folding code point by code point and keeping the original
   whenever the length changes guarantees index-for-index alignment. */
function fold(s) {
  var out = [], i, ch, l;
  var chars = Array.from(s);
  for (i = 0; i < chars.length; i++) {
    ch = chars[i];
    l = ch.toLowerCase();
    if (l.length !== ch.length) { l = ch; }
    out.push(QUOTES[l] || l);
  }
  return out.join('');
}

function searchFlat(query) {
  var out = [];
  if (!st.flat || !query) { return out; }
  var hay = fold(st.flat.text), needle = fold(String(query));
  if (!needle) { return out; }
  var i = hay.indexOf(needle);
  while (i >= 0 && out.length < MAX_HITS) {
    out.push({ gpos: toCP(i), length: toCP(i + needle.length) - toCP(i), u16: i, u16len: needle.length });
    i = hay.indexOf(needle, i + needle.length);
  }
  return out;
}

function rangeOf(u16a, u16b) {
  var f = st.flat;
  if (!f) { return null; }
  u16a = Math.max(0, Math.min(u16a, f.text.length));
  u16b = Math.max(u16a, Math.min(u16b, f.text.length));
  var A = locate(u16a), B = locate(u16b);
  if (!A[0] || !B[0]) { return null; }
  var r = doc.createRange();
  try {
    r.setStart(A[0], Math.min(A[1], A[0].nodeValue.length));
    r.setEnd(B[0], Math.min(B[1], B[0].nodeValue.length));
  } catch (e) { return null; }
  return r;
}
function rangeOfGpos(gposA, gposB) { return rangeOf(fromCP(gposA), fromCP(gposB)); }

function hlSupported() {
  return typeof window.Highlight === 'function' && typeof CSS !== 'undefined' && !!CSS.highlights;
}
/* Paint order: user highlights (0) under all find matches (1) under the
   active match (2). */
var HL_PRIORITY = { 'er-find': 1, 'er-find-active': 2 };
function hlSet(name, ranges) {
  if (!hlSupported()) { return; }
  if (!ranges || !ranges.length) { CSS.highlights['delete'](name); return; }
  /* LOAD-BEARING: build with add(), never `new Highlight(...ranges)` through
     Function.prototype tricks -- the previous `new window.Highlight.apply ? ...`
     parsed as `new (Highlight.apply)`, threw, was swallowed, and NOTHING was
     ever painted.  add() also has no argument-count limit. */
  var h = new window.Highlight();
  for (var i = 0; i < ranges.length; i++) { h.add(ranges[i]); }
  h.priority = HL_PRIORITY[name] || 0;
  CSS.highlights.set(name, h);
}
function hlClear(name) { if (hlSupported()) { CSS.highlights['delete'](name); } }

/* Repaint everything that is anchored by gpos.  Called after every relayout so
   highlights survive font changes, theme changes and window resizes. */
function repaint() {
  paintMatches();
  paintHighlights();
}

function paintMatches() {
  if (!hlSupported() || !st.flat) { return; }
  var all = [], act = null;
  for (var i = 0; i < st.matches.length; i++) {
    var m = st.matches[i];
    var r = rangeOfGpos(m.gpos, m.gpos + m.length);
    if (!r) { continue; }
    if (i === st.matchActive) { act = r; }
    all.push(r);
  }
  hlSet('er-find', all);
  hlSet('er-find-active', act ? [act.cloneRange()] : []);
}

var HL_COLORS = ['yellow', 'green', 'blue', 'pink'];
function paintHighlights() {
  if (!hlSupported() || !st.flat) { return; }
  var buckets = {};
  for (var i = 0; i < st.highlights.length; i++) {
    var h = st.highlights[i];
    var r = (h.end > h.start) ? rangeOfGpos(h.start, h.end) : null;
    if (!r || r.collapsed) { h.anchor_state = 'lost'; continue; }
    h.anchor_state = 'exact';
    var color = HL_COLORS.indexOf(h.color) >= 0 ? h.color : 'yellow';
    var name = (h.style === 'underline' ? 'er-hu-' : 'er-hl-') + color;
    (buckets[name] || (buckets[name] = [])).push(r);
  }
  for (var k = 0; k < HL_COLORS.length; k++) {
    hlSet('er-hl-' + HL_COLORS[k], buckets['er-hl-' + HL_COLORS[k]] || []);
    hlSet('er-hu-' + HL_COLORS[k], buckets['er-hu-' + HL_COLORS[k]] || []);
  }
}

/* Resolved literal colours for the highlight pseudos.  reader.css already
   paints them through var(); this sheet makes the page independent of whether
   reader.css arrived, and gives underlines a visible (darker) stroke. */
function paintCSS(tok) {
  var css = [];
  for (var i = 0; i < HL_COLORS.length; i++) {
    var nm = HL_COLORS[i], col = tok['hl-' + nm];
    css.push('::highlight(er-hl-' + nm + '){background-color:' + col + ';}');
    css.push('::highlight(er-hu-' + nm + '){text-decoration:underline;' +
             'text-decoration-color:color-mix(in srgb,' + col + ' 45%,' + tok.fg + ');' +
             'text-decoration-thickness:.14em;text-underline-offset:.14em;}');
  }
  css.push('::highlight(er-find){background-color:' + tok['find-bg'] + ';color:' + tok['find-fg'] + ';}');
  css.push('::highlight(er-find-active){background-color:' + tok['find-active-bg'] +
           ';color:' + tok['find-active-fg'] + ';}');
  return css.join('\n');
}

/* ===========================================================================
   10. SELECTION
   =========================================================================== */
function flatIndexAtOrAfter(node) {
  /* binary search over the flat node list by document position */
  var f = st.flat;
  if (!f || !f.nodes.length) { return -1; }
  var lo = 0, hi = f.nodes.length - 1, ans = -1;
  while (lo <= hi) {
    var m = (lo + hi) >> 1;
    var rel = node.compareDocumentPosition(f.nodes[m]);
    /* FOLLOWING (4) or CONTAINED_BY (16) => f.nodes[m] is at or after `node` */
    if ((rel & 4) || (rel & 16) || f.nodes[m] === node) { ans = m; hi = m - 1; } else { lo = m + 1; }
  }
  return ans;
}
function gposOfPoint(node, offset) {
  var f = st.flat;
  if (!f || !node) { return null; }
  if (node.nodeType === 3) {
    var i = f.index.get(node);
    if (i != null) { return toCP(f.starts[i] + Math.min(offset, node.nodeValue.length)); }
    var j = flatIndexAtOrAfter(node);
    return j >= 0 ? toCP(f.starts[j]) : null;
  }
  var child = node.childNodes[offset];
  if (!child) {
    /* boundary point after the last child: the first flat node that follows
       the whole subtree of `node` */
    var lo = 0, hi = f.nodes.length - 1, ans = -1;
    while (lo <= hi) {
      var m = (lo + hi) >> 1, rel = node.compareDocumentPosition(f.nodes[m]);
      if ((rel & 4) && !(rel & 16)) { ans = m; hi = m - 1; } else { lo = m + 1; }
    }
    return ans < 0 ? flatLen() : toCP(f.starts[ans]);
  }
  var k = flatIndexAtOrAfter(child);
  if (k < 0) { return flatLen(); }
  return toCP(f.starts[k]);
}

function selectionInfo() {
  var sel = window.getSelection();
  if (!sel || sel.isCollapsed || !sel.rangeCount) { return null; }
  var r = sel.getRangeAt(0);
  var txt = String(sel);
  if (!txt) { return null; }
  var a = gposOfPoint(r.startContainer, r.startOffset);
  var b = gposOfPoint(r.endContainer, r.endOffset);
  if (a == null || b == null) { return null; }
  if (b < a) { var t = a; a = b; b = t; }
  return {
    text: txt, start: a, end: b,
    snippet: st.flat ? st.flat.text.substr(fromCP(a), 40) : '',
    before:  st.flat ? st.flat.text.substr(Math.max(0, fromCP(a) - 20), Math.min(20, fromCP(a))) : '',
    page: st.page,
    percent: bookPercentOf(a)
  };
}

/* ===========================================================================
   11. FOOTNOTE POPOVERS
   =========================================================================== */
function epubType(el) {
  var v = null;
  try { v = el.getAttributeNS(NS_OPS, 'type'); } catch (e) { /* ignore */ }
  return v || el.getAttribute('epub:type') || '';
}
function isNoteref(a) {
  return /\bnoteref\b/.test(epubType(a)) || a.getAttribute('role') === 'doc-noteref';
}
var pop = null;
function showNote(anchor) {
  var href = anchor.getAttribute('href') || '';
  if (href.charAt(0) !== '#') { return false; }
  var id;
  try { id = decodeURIComponent(href.slice(1)); } catch (e) { id = href.slice(1); }
  var tgt = doc.getElementById(id);
  if (!tgt) { return false; }
  hideNote();

  pop = doc.createElement('div');
  pop.id = 'er-pop';
  pop.setAttribute('data-er', 'pop');
  pop.setAttribute('popover', 'manual');
  var clone = tgt.cloneNode(true);
  clone.removeAttribute('id');
  var ids = clone.querySelectorAll('[id]');
  for (var i = 0; i < ids.length; i++) { ids[i].removeAttribute('id'); }
  var backs = clone.querySelectorAll('a[href^="#"]');
  for (var j = 0; j < backs.length; j++) {
    var b = backs[j];
    while (b.firstChild) { b.parentNode.insertBefore(b.firstChild, b); }
    b.remove();
  }
  pop.appendChild(clone);
  de.appendChild(pop);            /* NOT into body: it is position:fixed */
  try { pop.showPopover(); } catch (e) { pop.style.display = 'block'; }

  var a = anchor.getBoundingClientRect(), pr = pop.getBoundingClientRect();
  var x = Math.max(8, Math.min(st.W - pr.width - 8, a.left - 12));
  var y = (a.bottom + 10 + pr.height <= st.H) ? (a.bottom + 10) : Math.max(8, a.top - 10 - pr.height);
  pop.style.left = Math.round(x) + 'px';
  pop.style.top  = Math.round(y) + 'px';
  return true;
}
function hideNote() {
  if (!pop) { return; }
  try { pop.hidePopover(); } catch (e) { /* ignore */ }
  pop.remove();
  pop = null;
}

/* ===========================================================================
   12. CLICK-TO-ZOOM IMAGES
   =========================================================================== */
var zoomEl = null;
function openZoom(img) {
  closeZoom();
  zoomEl = doc.createElement('div');
  zoomEl.id = 'er-zoom';
  zoomEl.setAttribute('data-er', 'zoom');
  zoomEl.setAttribute('popover', 'manual');
  var big = doc.createElement('img');
  big.src = img.currentSrc || img.src;
  var alt = img.getAttribute('alt');
  if (alt) { big.setAttribute('alt', alt); }
  zoomEl.appendChild(big);
  de.appendChild(zoomEl);
  try { zoomEl.showPopover(); } catch (e) { zoomEl.style.display = 'flex'; }
  return true;
}
function closeZoom() {
  if (!zoomEl) { return false; }
  try { zoomEl.hidePopover(); } catch (e) { /* ignore */ }
  zoomEl.remove();
  zoomEl = null;
  return true;
}

/* ===========================================================================
   13. FIXED LAYOUT — detect and scale to fit.  Never paginate.
   =========================================================================== */
function detectFXL() {
  var m = doc.querySelector('meta[name="viewport"]');
  if (m) {
    var c = m.getAttribute('content') || '';
    var w = /(?:^|[;,\s])width\s*=\s*([\d.]+)/.exec(c);
    var h = /(?:^|[;,\s])height\s*=\s*([\d.]+)/.exec(c);
    if (w && h && +w[1] > 0 && +h[1] > 0) { return { w: +w[1], h: +h[1], src: 'meta' }; }
  }
  var svg = doc.querySelector('body > svg[viewBox], body > div > svg[viewBox]');
  if (svg) {
    var v = (svg.getAttribute('viewBox') || '').split(/[\s,]+/).map(Number);
    if (v.length === 4 && v[2] > 0 && v[3] > 0) { return { w: v[2], h: v[3], src: 'svg' }; }
  }
  var img = doc.querySelector('body > img, body > div > img');
  if (img && img.naturalWidth > 0 && img.naturalHeight > 0 && doc.body.children.length <= 2) {
    return { w: img.naturalWidth, h: img.naturalHeight, src: 'img' };
  }
  return null;
}
function applyFXL() {
  var f = st.fxl;
  if (!f) { return; }
  var s  = Math.min(st.W / f.w, st.H / f.h);
  var tx = Math.round((st.W - f.w * s) / 2);
  var ty = Math.round((st.H - f.h * s) / 2);
  ensureSheets();
  de.setAttribute('data-er-fxl', '');
  shGeom.textContent = '@layer er-safety{' +
    'html{margin:0!important;padding:0!important;border:0!important;' +
         'width:' + st.W + 'px!important;height:' + st.H + 'px!important;' +
         'overflow:hidden!important;position:static!important;' +
         'columns:auto!important;column-count:auto!important;column-width:auto!important;' +
         'writing-mode:horizontal-tb!important;transform:none!important;zoom:1!important;' + NO_ANIM +
         'background:var(--er-bg)!important;}' +
    'html::after{content:none!important;}' +
    'body{margin:0!important;padding:0!important;border:0!important;float:none!important;' +
         'width:' + f.w + 'px!important;height:' + f.h + 'px!important;' +
         'min-width:0!important;max-width:none!important;min-height:0!important;max-height:none!important;' +
         'columns:auto!important;column-count:auto!important;column-width:auto!important;' +
         /* clip, not hidden: in quirks mode hidden would make <body> a
            potential scroller and body.clientWidth would stop being the
            viewport (see viewportSize) */
         'overflow:clip!important;position:relative!important;' +
         'transform-origin:0 0!important;' +
         'transform:translate(' + tx + 'px,' + ty + 'px) scale(' + s.toFixed(6) + ')!important;}' +
    'img,svg,video,canvas,object,embed,iframe,picture{' +
         'max-width:none!important;max-height:none!important;}' +
  '}';
  st.pages = 1;
  st.page = 0;
  st.fxlScale = s;
  st.fxlOffset = [tx, ty];
}

/* ===========================================================================
   14. HOST BRIDGE  (JS -> Python)
   =========================================================================== */
var eventLog = [];      /* polling fallback when no QWebChannel object exists  */
function emit(signal, payload, raw) {
  var h = window.epubReaderHost;
  var arg;
  if (raw) { arg = payload; }
  else if (payload === undefined || payload === null) { arg = null; }
  else { arg = (st.hostPayload === 'object') ? payload : JSON.stringify(payload); }

  if (h && typeof h[signal] === 'function') {
    try { if (arg === null) { h[signal](); } else { h[signal](arg); } return true; }
    catch (e) { /* fall through to the log */ }
  }
  eventLog.push({ t: Date.now(), signal: signal, payload: payload });
  if (eventLog.length > 200) { eventLog.splice(0, eventLog.length - 200); }
  return false;
}

var reportPending = false;
function report() {
  if (reportPending) { return; }
  reportPending = true;
  var fire = function () { reportPending = false; emit('positionChanged', stateObj()); };
  if (typeof queueMicrotask === 'function') { queueMicrotask(fire); } else { setTimeout(fire, 0); }
}

function stateObj() {
  var l = st.lastLoc;
  var gp = l && typeof l.gpos === 'number' ? l.gpos : 0;
  return {
    page: st.page,
    pages: st.pages,
    gpos: gp,
    percent: bookPercentOf(gp),
    chapterPercent: chapterPercentOf(gp),
    mode: publicMode(),
    fixedLayout: !!st.fxl,
    rtl: st.rtl,
    vertical: st.vertical,
    chars: flatLen(),
    matches: st.matches.length,
    matchActive: st.matchActive
  };
}

/* ===========================================================================
   15. INPUT — installed on WINDOW in the CAPTURE phase at injection time.
   webhost injects this file at DocumentCreation, before any book script can
   run, so our window-capture listeners are first in the listener list; with
   stopImmediatePropagation() no book listener (window/document/element, any
   phase) sees a key we handle.  Verified across worlds: ours runs in
   ApplicationWorld, the book's in the main world.  Every handler is inert
   until init().
   =========================================================================== */
function turn(d) {
  if (st.mode === 'scroll') {
    var pos = scrollPos(), mx = scrollMax();
    var stepLen = Math.max(40, viewLen() - Math.round(viewLen() * 0.12));
    if (d < 0 && pos <= 0.5) { emit('keyUnhandled', 'EpubReader.PrevChapter', true); return; }
    if (d > 0 && pos >= mx - 1) { emit('keyUnhandled', 'EpubReader.NextChapter', true); return; }
    setScrollPos(pos + d * stepLen);
    var l = capture(); if (l) { st.lastLoc = l; }
    report();
    return;
  }
  if (st.mode === 'fixed') {
    emit('keyUnhandled', d > 0 ? 'EpubReader.NextChapter' : 'EpubReader.PrevChapter', true);
    return;
  }
  var want = st.page + d;
  if (want < 0) { emit('keyUnhandled', 'EpubReader.PrevChapter', true); return; }
  if (want > st.pages - 1) { emit('keyUnhandled', 'EpubReader.NextChapter', true); return; }
  gotoPageInternal(want);
}

function keyDesc(e) {
  var p = [];
  if (e.ctrlKey)  { p.push('Ctrl'); }
  if (e.altKey)   { p.push('Alt'); }
  if (e.shiftKey) { p.push('Shift'); }
  if (e.metaKey)  { p.push('Meta'); }
  var k = e.key;
  if (k && k.length === 1) { k = k.toUpperCase(); }
  p.push(k);
  return p.join('+');
}
function isEditable(t) {
  return !!(t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/i.test(t.tagName || '')));
}

/* scroll mode: a user scroll re-anchors the locator, debounced; our own
   programmatic scrolls (restore, relayout) never do. */
var scrollCapT = 0, scrollCapPending = false;
function scheduleScrollCapture() {
  scrollCapPending = true;
  clearTimeout(scrollCapT);
  scrollCapT = setTimeout(flushScrollCapture, 100);
}
function flushScrollCapture() {
  if (!scrollCapPending) { return; }
  scrollCapPending = false;
  clearTimeout(scrollCapT);
  if (st.mode !== 'scroll' || restoring || relayoutDepth) { return; }
  var l = capture(); if (l) { st.lastLoc = l; }
  report();
}
function flushNav() { var l = capture(); if (l) { st.lastLoc = l; } report(); }

var rzT = 0;
function onResize() {
  if (!st.inited) { return; }
  /* LOAD-BEARING (pitfall 5): do NOT capture here.  By the time a resize
     event is delivered the document has already reflowed (book CSS using
     vh/vw, media queries), so a capture taken now describes the NEW layout
     while our page arithmetic still describes the old one -- traced to a
     captured char of 8855 when the true anchor was 9619.  Reuse the locator
     pinned at the last user navigation. */
  clearTimeout(rzT);
  rzT = setTimeout(function () {
    rzT = 0;
    var loc = st.lastLoc;
    relayout();
    if (loc) { restore(loc); }
    report();
  }, 120);
}

/* Images, stylesheets and web fonts that finish AFTER init() change the page
   count.  Debounced (90 ms after the last event) but never starved: a chapter
   streaming in forty images still relayouts at least every 400 ms, and there
   is no cap on the number of events -- a capped counter left pages unreachable
   on image-heavy chapters. */
var lateT = 0, lateFirst = 0;
function lateRelayout() {
  if (!st.inited) { return; }
  var now = Date.now();
  if (!lateT) { lateFirst = now; }
  clearTimeout(lateT);
  lateT = setTimeout(function () {
    lateT = 0;
    if (rzT) { return; }                /* a resize relayout is already queued */
    var loc = st.lastLoc;
    relayout();
    if (loc) { restore(loc); }
    report();
  }, (now - lateFirst > 400) ? 0 : 90);
}

var inputInstalled = false;
function installInput() {
  if (inputInstalled) { return; }
  inputInstalled = true;
  var W_ = window;

  /* --- keyboard ------------------------------------------------------- */
  W_.addEventListener('keydown', function (e) {
    if (!st.inited) { return; }
    var t = e.target;
    if (isEditable(t)) { return; }

    if (e.key === 'Escape') {
      if (zoomEl) { closeZoom(); e.preventDefault(); e.stopImmediatePropagation(); return; }
      if (pop) { hideNote(); e.preventDefault(); e.stopImmediatePropagation(); return; }
      emit('keyUnhandled', keyDesc(e), true);
      return;
    }
    /* Ctrl/Alt/Meta combinations belong to the Qt chrome (Ctrl+F, Ctrl+T,
       Ctrl+D, Ctrl +/-).  Forward them and get out of the way. */
    if (e.ctrlKey || e.altKey || e.metaKey) { emit('keyUnhandled', keyDesc(e), true); return; }

    var handled = true;
    switch (e.key) {
      case 'ArrowRight': case 'ArrowDown': case 'PageDown':
        turn(1); break;
      case 'ArrowLeft': case 'ArrowUp': case 'PageUp':
        turn(-1); break;
      case 'Enter':
        turn(1); break;
      case ' ': case 'Spacebar':
        turn(e.shiftKey ? -1 : 1); break;
      case 'Home':
        if (st.mode === 'scroll') { setScrollPos(0); flushNav(); } else { gotoPageInternal(0); }
        break;
      case 'End':
        if (st.mode === 'scroll') { setScrollPos(scrollMax()); flushNav(); } else { gotoPageInternal(st.pages - 1); }
        break;
      default:
        handled = false;
    }
    if (handled) {
      /* LOAD-BEARING (verified fact 25 + cross-world probe): capture phase on
         window plus stopImmediatePropagation beats the book's own listeners. */
      e.preventDefault();
      e.stopImmediatePropagation();
      return;
    }
    if (e.key.length === 1 || /^F\d{1,2}$/.test(e.key) || e.key === 'Tab' || e.key === 'Backspace' ||
        e.key === 'Delete' || e.key === 'Insert') {
      emit('keyUnhandled', keyDesc(e), true);
    }
  }, true);

  /* --- wheel ---------------------------------------------------------- */
  var acc = 0, wheelT = 0;
  W_.addEventListener('wheel', function (e) {
    if (!st.inited || zoomEl) { return; }
    if (e.ctrlKey) { return; }                       /* Ctrl+wheel belongs to Qt */
    if (st.mode === 'scroll') {
      if (!st.vertical) { return; }                  /* native vertical scroll   */
      e.preventDefault(); e.stopImmediatePropagation();
      var dv = Math.abs(e.deltaY) >= Math.abs(e.deltaX) ? e.deltaY : e.deltaX;
      setScrollPos(scrollPos() + dv);
      scheduleScrollCapture();
      return;
    }
    if (st.mode !== 'paginated') { return; }
    e.preventDefault();
    e.stopImmediatePropagation();
    acc += (Math.abs(e.deltaY) >= Math.abs(e.deltaX) ? e.deltaY : e.deltaX);
    var now = performance.now();
    if (Math.abs(acc) >= 40 && now - wheelT > 110) {
      turn(acc > 0 ? 1 : -1);
      acc = 0;
      wheelT = now;
    }
  }, { capture: true, passive: false });

  /* --- pointer (mouse / pen / touch all arrive as pointer events) ------ */
  var px = 0, py = 0, pid = null, moved = false, downT = 0;
  W_.addEventListener('pointerdown', function (e) {
    if (!st.inited) { return; }
    pid = e.pointerId; px = e.clientX; py = e.clientY; moved = false; downT = performance.now();
  }, true);
  W_.addEventListener('pointermove', function (e) {
    if (e.pointerId !== pid) { return; }
    if (Math.abs(e.clientX - px) > 8 || Math.abs(e.clientY - py) > 8) { moved = true; }
  }, true);
  W_.addEventListener('pointercancel', function () { pid = null; moved = true; }, true);
  W_.addEventListener('pointerup', function (e) {
    if (!st.inited || e.pointerId !== pid) { return; }
    pid = null;
    var dx = e.clientX - px, dy = e.clientY - py, dt = performance.now() - downT;

    if (zoomEl) { closeZoom(); e.preventDefault(); e.stopImmediatePropagation(); return; }
    if (pop && !pop.contains(e.target)) { hideNote(); return; }

    if (!mobileHost && moved && st.mode === 'paginated' && Math.abs(dx) > 55 && Math.abs(dx) > Math.abs(dy) * 1.6 && dt < 700) {
      e.preventDefault(); e.stopImmediatePropagation();
      turn(dx < 0 ? 1 : -1);
      return;
    }
    if (moved) { reportSelection(); return; }
    if (mobileHost && dt > 350) { return; }

    /* links are handled on `click` (below) so the navigation it would cause
       can be cancelled; a tap on a link must not also turn the page */
    if (e.target && e.target.closest && e.target.closest('a[href]')) { return; }
    if (pop && pop.contains(e.target)) { return; }

    var selTxt = window.getSelection ? String(window.getSelection() || '') : '';
    if (selTxt.length) { reportSelection(); return; }

    /* a click on a highlight opens its note rather than turning the page */
    var hit = highlightAt(e.clientX, e.clientY);
    if (hit) { e.preventDefault(); e.stopImmediatePropagation(); emit('noteRequested', String(hit.id), true); return; }

    if (st.zoomOn && e.target && /^img$/i.test(e.target.tagName || '')) {
      e.preventDefault(); e.stopImmediatePropagation();
      openZoom(e.target);
      return;
    }

    if (st.mode !== 'paginated') {
      if (mobileHost) { emit('keyUnhandled', 'EpubReader.TapCentre', true); }
      return;
    }
    var z = e.clientX / Math.max(1, st.W);      /* 22% / 56% / 22% zones */
    if (z < 0.22) { turn(-1); }
    else if (z > 0.78) { turn(1); }
    else { emit('keyUnhandled', 'EpubReader.TapCentre', true); }
  }, true);

  /* Observe touch without cancelling WebView's pan/fling. Pointer events may
     be cancelled when native scrolling starts, but passive touchend still
     tells us whether a SECOND outward gesture started at a chapter boundary.
     A fling that merely reaches the last paragraph must not skip it. */
  var mobileTouch = null, lastChapterGesture = 0;
  W_.addEventListener('touchstart', function (e) {
    if (!mobileHost || !st.inited || e.touches.length !== 1 || zoomEl ||
        (pop && pop.contains(e.target)) || isEditable(e.target)) { mobileTouch = null; return; }
    var t = e.touches[0];
    mobileTouch = {x:t.clientX,y:t.clientY,time:Date.now(),start:scrollPos(),max:scrollMax()};
  }, {capture:true,passive:true});
  W_.addEventListener('touchcancel', function () { mobileTouch = null; }, {capture:true,passive:true});
  W_.addEventListener('touchend', function (e) {
    var down = mobileTouch; mobileTouch = null;
    if (!down || !e.changedTouches.length || e.touches.length || zoomEl ||
        (window.getSelection && String(window.getSelection() || '').length)) { return; }
    var t = e.changedTouches[0], dx = t.clientX-down.x, dy = t.clientY-down.y;
    var elapsed = Date.now()-down.time, ax = Math.abs(dx), ay = Math.abs(dy);
    if (elapsed > 1200 || Math.max(ax,ay) < 32) { return; }
    if (st.mode === 'paginated') {
      if (ay > ax*1.25) { turn(dy < 0 ? 1 : -1); }
      else if (ax > ay*1.25) { turn(dx < 0 ? 1 : -1); }
      return;
    }
    if (st.mode !== 'scroll' || st.vertical || ay < ax*1.35 || Date.now()-lastChapterGesture < 400) { return; }
    if (dy < 0 && down.start >= down.max-2) {
      lastChapterGesture=Date.now(); emit('keyUnhandled','EpubReader.NextChapter',true);
    } else if (dy > 0 && down.start <= 2) {
      lastChapterGesture=Date.now(); emit('keyUnhandled','EpubReader.PrevChapter',true);
    }
  }, {capture:true,passive:true});

  /* LOAD-BEARING: preventDefault on pointerup does NOT cancel the click that
     follows, so a link would both be reported AND navigate (a double
     navigation through webhost's policy).  Links are taken on `click`. */
  var onLink = function (e) {
    if (!st.inited) { return; }
    var a = e.target && e.target.closest ? e.target.closest('a[href]') : null;
    if (!a) { return; }
    e.preventDefault(); e.stopImmediatePropagation();
    if (e.type !== 'click') { return; }
    if (isNoteref(a) && showNote(a)) { return; }
    emit('linkClicked', a.getAttribute('href') || '', true);
  };
  W_.addEventListener('click', onLink, true);
  W_.addEventListener('auxclick', onLink, true);

  W_.addEventListener('contextmenu', function (e) {
    if (!st.inited) { return; }
    e.preventDefault();
    emit('selectionChanged', selectionInfo());
  }, true);

  doc.addEventListener('selectionchange', function () { if (st.inited) { reportSelection(); } });

  /* --- scroll ----------------------------------------------------------- */
  doc.addEventListener('scroll', function () {
    if (!st.inited || restoring || relayoutDepth) { return; }
    if (st.mode === 'paginated') {
      if (SE().scrollTop !== 0) { SE().scrollTop = 0; }
      /* a resize is being delivered: onResize owns the position */
      var vsz = viewportSize();
      if (rzT || vsz[0] !== st.W || vsz[1] !== st.H) { return; }
      if (Math.abs(Math.abs(SE().scrollLeft) - st.page * st.step) < 1) { return; }   /* our own write */
      /* a genuine scroll (focus moved to a link on another page, find-in-page,
         drag-selection autoscroll): snap to that page and re-anchor there */
      var p = clampPage(Math.round(Math.abs(SE().scrollLeft) / st.step));
      SE().scrollLeft = sgn() * p * st.step;
      st.progScroll = [SE().scrollLeft, SE().scrollTop];
      st.page = p;
      var l = capture(); if (l) { st.lastLoc = l; }
      report();
      return;
    }
    if (st.mode === 'scroll') {
      var ps = st.progScroll;
      if (ps && Math.abs(SE().scrollLeft - ps[0]) < 1 && Math.abs(SE().scrollTop - ps[1]) < 1) { return; }
      if (rzT) { return; }
      scheduleScrollCapture();
    }
  }, { passive: true });

  /* --- resize --------------------------------------------------------- */
  /* A ResizeObserver on <html>/<body> NEVER FIRES: the safety layer pins their
     px size, so their observed box never changes (page count stayed frozen at
     57 across four resizes).  Listen to the window. */
  W_.addEventListener('resize', onResize);
  if (W_.visualViewport) { W_.visualViewport.addEventListener('resize', onResize); }

  /* --- late reflow: images and web fonts change the page count --------- */
  /* LOAD-BEARING: element `load`/`error` events are listened for on the
     DOCUMENT (capture).  The DOM's event path for type "load" stops at the
     Document and never reaches Window, so a window-capture listener misses
     every <img> (verified: 30 late images, page count stuck at 10 of 40). */
  W_.addEventListener('load', function () { lateRelayout(); });
  doc.addEventListener('load', function (e) {
    var t = e.target;
    if (t && /^(img|link)$/i.test(t.tagName || '')) { applyReaderImageSizing(); }
    if (t && /^(img|iframe|image|link|svg|object|video)$/i.test(t.tagName || '')) { lateRelayout(); }
  }, true);
  doc.addEventListener('error', function (e) {
    if (e.target && /^(img|image|link)$/i.test(e.target.tagName || '')) { lateRelayout(); }
  }, true);
  /* A book's @font-face is fetched only when layout first needs it, which can
     be AFTER init(); document.fonts.ready may already have resolved by then.
     `loadingdone` fires for every batch that finishes. */
  try {
    if (doc.fonts && typeof doc.fonts.addEventListener === 'function') {
      doc.fonts.addEventListener('loadingdone', lateRelayout);
    }
  } catch (e) { /* ignore */ }
}

var selT = 0;
function reportSelection() {
  clearTimeout(selT);
  selT = setTimeout(function () { emit('selectionChanged', selectionInfo()); }, 80);
}

function highlightAt(x, y) {
  if (!st.highlights.length || !st.flat) { return null; }
  var r = null;
  if (doc.caretRangeFromPoint) { r = doc.caretRangeFromPoint(x, y); }
  if (!r || !r.startContainer || r.startContainer.nodeType !== 3) { return null; }
  var i = st.flat.index.get(r.startContainer);
  if (i == null) { return null; }
  var gp = toCP(st.flat.starts[i] + r.startOffset);
  for (var k = 0; k < st.highlights.length; k++) {
    var h = st.highlights[k];
    if (gp >= h.start && gp < h.end) { return h; }
  }
  return null;
}

/* ===========================================================================
   16. APPLY SETTINGS
   Live: every call re-derives the whole presentation from `settings` and
   returns to the pinned locator, so the reading position survives font,
   theme, layout-mode and margin changes.
   =========================================================================== */
function applySettings(sIn, skipRestore) {
  var loc = st.lastLoc || capture();      /* read BEFORE mutating anything */
  var s = normSettings(sIn);
  st.settings = s;
  ensureSheets();

  /* undo any previous book rewriting before re-deriving it */
  recolourBook(false);
  releaseRootTypography(false);
  releaseParagraphStyle(null);
  collectSheets();

  var tok = applyTokens(s);
  var fxl = !!st.fxl;

  var set = function (k, v) { de.style.setProperty(k, v); };
  set('--er-link', 'var(--er-accent)');
  set('--er-fs', Math.max(8, Math.round(Number(s.font_size_px) || 21)) + 'px');
  set('--er-lh', String(Number(s.line_height) || 1.9));
  set('--er-ff', fontStack(s.font_latin, s.font_cjk));
  set('--er-mono', monoStack(s.font_cjk));
  applyMobileScriptFonts(s, fxl);
  set('--er-align', s.text_align || 'start');
  set('--er-hyphens', s.hyphens || 'manual');

  de.setAttribute('data-er-zoom', s.image_click_zoom ? 'on' : 'off');
  st.zoomOn = !!s.image_click_zoom;
  if (tok.scheme === 'dark') {
    de.setAttribute('data-er-imgdark', s.invert_images_in_dark ? 'invert' : 'dim');
  } else {
    de.removeAttribute('data-er-imgdark');
  }

  /* Optional theme-layer rules, emitted only when the user set them (null keeps
     the book's own paragraph design).  Never for fixed-layout pages. */
  var opt = [];
  var wantSpacing = !fxl && s.para_spacing_em !== null && s.para_spacing_em !== undefined;
  var wantIndent  = !fxl && s.text_indent_ch !== null && s.text_indent_ch !== undefined;
  if (wantSpacing) { opt.push('p{margin-block:' + Number(s.para_spacing_em) + 'em;}'); }
  if (wantIndent)  { opt.push('p{text-indent:' + Number(s.text_indent_ch) + 'em;}'); }
  if (!fxl && s.font_weight) { opt.push('body{font-weight:' + Number(s.font_weight) + ';}'); }
  shOpt.textContent = opt.length ? '@layer er-theme{' + opt.join('') + '}' : '';

  shPaint.textContent = paintCSS(tok);

  if (!fxl) {
    /* fixed-layout pages are pictures of pages: their typography and colours
       are part of the design and are never rewritten */
    normaliseFontSizes();
    if (!s.use_book_fonts) { releaseRootTypography(true); }
    var pp = [];
    if (wantSpacing) { pp.push('margin-top', 'margin-bottom', 'margin'); }
    if (wantIndent) { pp.push('text-indent'); }
    if (pp.length) { releaseParagraphStyle(pp); }
    if (tok.scheme === 'dark') { recolourBook(true, tok); }
  }

  /* mode: the user's choice, unless the document forces our hand */
  var wanted = (s.layout === 'scroll' || s.layout === 'scrolled') ? 'scroll' : 'paginated';
  st.wanted = wanted;
  if (st.fxl) { st.mode = 'fixed'; }
  else if (st.vertical) { st.mode = 'scroll'; }
  else { st.mode = wanted; }

  applyReaderImageSizing();
  relayout();
  if (loc && !skipRestore) { restore(loc); }
  report();
  return stateObj();
}

/* ===========================================================================
   17. BOOTSTRAP + CALL QUEUE
   Every public function is safe to call before DOMContentLoaded (and before
   init): the call is queued and replayed in order -- init() once <body>
   exists, everything else once init() has run.  A queued call returns the
   documented "not ready" value (null / false / '' / []).
   =========================================================================== */
var queue = [];
function canRun(needsInit) { return st.booted && (!needsInit || st.inited); }
function flushQueue() {
  var q = queue.splice(0), rest = [];
  for (var i = 0; i < q.length; i++) {
    if (!canRun(q[i].needsInit)) { rest.push(q[i]); continue; }
    try { q[i].run(); } catch (e) { /* a queued call must not break the rest */ }
  }
  queue = rest.concat(queue);
}
function boot() {
  if (st.booted) { return; }
  if (!doc.body && doc.readyState === 'loading') { return; }
  currentDE();
  if (!de) { return; }
  st.booted = true;
  ensureOrder();
  flushQueue();
}
if (doc.body) { boot(); }
else {
  doc.addEventListener('DOMContentLoaded', boot);
  doc.addEventListener('readystatechange', boot);
}

function guard(fn, fallback, needsInit) {
  return function () {
    var self = this, args = arguments;
    if (!canRun(needsInit !== false)) {
      queue.push({ needsInit: needsInit !== false, run: function () { fn.apply(self, args); } });
      return fallback;
    }
    return fn.apply(self, args);
  };
}

/* ===========================================================================
   18. PUBLIC API  (contract section 4)
   =========================================================================== */
var fontsHooked = false;
function doInit(cfg) {
  cfg = cfg || {};
  mobileHost = cfg.mobileHost === true;
  formulaScales = cfg.formulaScales || {};
  if (!doc.body) { return null; }
  ensureBaseCSS(cfg);
  ensureSheets();

  if (cfg.hostPayload === 'object' || cfg.hostPayload === 'json') { st.hostPayload = cfg.hostPayload; }
  if (cfg.book && typeof cfg.book.total === 'number') {
    st.book = { offset: cfg.book.offset || 0, total: cfg.book.total };
  }

  /* Vertical writing mode makes multicol stack along the block axis; the page
     model does not apply (research fact 33) -> scroll fallback.  Detected ONCE,
     before any geometry sheet of ours can mask the book's own writing-mode. */
  if (st.verticalDetected !== true) {
    var wm = String(getComputedStyle(doc.body).writingMode || getComputedStyle(de).writingMode || '');
    st.vertical = /^(vertical|sideways)/.test(wm);
    st.verticalRL = /-rl$/.test(wm);
    st.verticalDetected = true;
  }

  var wantFXL = cfg.fixedLayout === true || cfg.layout === 'pre-paginated';
  st.fxl = wantFXL ? (cfg.viewport || detectFXL() || { w: 1200, h: 1600, src: 'default' })
                   : (cfg.fixedLayout === false ? null : detectFXL());
  if (st.fxl) { de.setAttribute('data-er-fxl', ''); } else { de.removeAttribute('data-er-fxl'); }
  if (mobileHost) { de.setAttribute('data-er-mobile', ''); }

  collectSheets();
  captureImageSizing();
  defend();
  installInput();

  var s = cfg.settings || {};
  if (cfg.mode === 'scroll' || cfg.mode === 'scrolled') { s = Object.assign({}, s, { layout: 'scroll' }); }
  else if (cfg.mode === 'paginated' || cfg.mode === 'paged') { s = Object.assign({}, s, { layout: 'paged' }); }

  applySettings(s, true);

  var ok = false;
  if (cfg.locator) { ok = restore(cfg.locator); }
  else if (st.lastLoc) { restore(st.lastLoc); }          /* re-init: stay put */
  else { st.lastLoc = capture(); }

  st.inited = true;
  if (!fontsHooked && doc.fonts && doc.fonts.ready && typeof doc.fonts.ready.then === 'function') {
    fontsHooked = true;
    doc.fonts.ready.then(lateRelayout)['catch'](function () { /* ignore */ });
  }
  report();
  emit('ready', stateObj());
  flushQueue();

  var out = stateObj();
  out.restored = ok;
  out.viewport = st.fxl;
  return out;
}

function scrollToRect(rc) {
  setScrollPos(scrollPos() + rectStart(rc) - viewPad());
}

var API = {
  __epubReaderEngine: 1,

  init: guard(function (cfg) { return doInit(cfg); }, null, false),

  applySettings: guard(function (s) { return applySettings(s, false); }, null),

  state: guard(function () { return stateObj(); }, null),

  nextPage: guard(function () { turn(1); return stateObj(); }, null),
  prevPage: guard(function () { turn(-1); return stateObj(); }, null),

  gotoPage: guard(function (n) { gotoPageInternal(Number(n) || 0); return stateObj(); }, null),

  gotoPercent: guard(function (p) {
    p = Math.max(0, Math.min(1, Number(p) || 0));
    if (st.mode === 'scroll') {
      setScrollPos(p * scrollMax());
      flushNav();
      return stateObj();
    }
    if (st.mode === 'fixed') { return stateObj(); }
    var n = flatLen();
    if (n) {
      var u = fromCP(Math.max(0, Math.min(n - 1, Math.round(p * n))));
      var pg = pageOfChar(u);
      if (pg >= 0) { gotoPageInternal(pg); return stateObj(); }
    }
    gotoPageInternal(Math.round(p * (st.pages - 1)));
    return stateObj();
  }, null),

  capture: guard(function () { return currentLocator(); }, null),

  restore: guard(function (loc) { var ok = restore(loc); report(); return ok; }, false),

  gotoFragment: guard(function (id) {
    if (!id) { return false; }
    var el = null;
    try { el = doc.getElementById(id) || doc.querySelector('[name="' + CSS.escape(id) + '"]'); }
    catch (e) { el = doc.getElementById(id); }
    if (!el) { return false; }
    if (st.mode === 'fixed') { return true; }
    var r = el.getBoundingClientRect();
    if (!r.width && !r.height) {
      var t = doc.createRange(); t.selectNodeContents(el);
      var rcs = t.getClientRects();
      if (rcs.length) { r = rcs[0]; }
    }
    if (st.mode === 'scroll') { scrollToRect(r); flushNav(); return true; }
    var p = pageOfRect(r);
    if (p < 0) { return false; }
    gotoPageInternal(p);
    return true;
  }, false),

  /* The cross-engine invariant hook: this string must equal
     epublib.plain_text(zip_name) character for character. */
  flatText: guard(function () {
    if (!st.flat) { buildFlat(); }
    return st.flat ? st.flat.text : '';
  }, '', false),

  flatTextInfo: guard(function () {
    if (!st.flat) { buildFlat(); }
    if (!st.flat) { return null; }
    var t = st.flat.text;
    return {
      utf16Length: t.length,
      codePointLength: flatLen(),
      astralPairs: st.flat.cp ? st.flat.cp.pairs.length : 0,
      nodes: st.flat.nodes.length,
      head: t.slice(0, 64),
      tail: t.slice(-64)
    };
  }, null, false),

  search: guard(function (q) {
    var hits = searchFlat(q);
    st.matches = hits.map(function (h) { return { gpos: h.gpos, length: h.length }; });
    st.matchActive = -1;
    paintMatches();
    return st.matches.map(function (m) { return { gpos: m.gpos, length: m.length }; });
  }, []),

  showMatches: guard(function (ranges, active) {
    st.matches = (ranges || []).map(function (r) {
      var g = Number(r.gpos) || 0;
      var len = (r.length != null) ? Number(r.length) : ((Number(r.end) || 0) - g);
      return { gpos: g | 0, length: Math.max(0, len | 0) };
    });
    st.matchActive = (typeof active === 'number' && active >= 0 && active < st.matches.length) ? active : -1;
    paintMatches();
    if (st.matchActive >= 0) {
      var m = st.matches[st.matchActive];
      var r2 = rangeOfGpos(m.gpos, m.gpos + m.length);
      var rc = r2 ? (r2.getClientRects()[0] || rectAtOrAfter(fromCP(m.gpos))) : null;
      if (rc) {
        if (st.mode === 'scroll') { scrollToRect(rc); flushNav(); }
        else if (st.mode === 'paginated') {
          var p = pageOfRect(rc);
          if (p >= 0) { gotoPageInternal(p); }
        }
      }
    }
    return { count: st.matches.length, active: st.matchActive, page: st.page, pages: st.pages };
  }, null),

  clearMatches: guard(function () {
    st.matches = []; st.matchActive = -1;
    hlClear('er-find'); hlClear('er-find-active');
    return true;
  }, true),

  applyHighlights: guard(function (list) {
    st.highlights = (list || []).map(function (h) {
      return {
        id: h.id, start: Number(h.start) | 0, end: Number(h.end) | 0,
        color: h.color || 'yellow', style: h.style || 'fill',
        note: h.note || '', anchor_state: 'exact'
      };
    });
    paintHighlights();
    var lost = 0;
    for (var i = 0; i < st.highlights.length; i++) { if (st.highlights[i].anchor_state === 'lost') { lost++; } }
    return { applied: st.highlights.length - lost, lost: lost,
             states: st.highlights.map(function (h) { return { id: h.id, state: h.anchor_state }; }) };
  }, null),

  selectionInfo: guard(function () { return selectionInfo(); }, null),

  clearSelection: guard(function () {
    var s = window.getSelection();
    if (s) { try { s.removeAllRanges(); } catch (e) { /* ignore */ } }
    return true;
  }, true),

  zoomImage: guard(function (on) {
    if (on === false) { st.zoomOn = false; de.setAttribute('data-er-zoom', 'off'); closeZoom(); return false; }
    if (on === true || on === undefined) { st.zoomOn = true; de.setAttribute('data-er-zoom', 'on'); return true; }
    /* zoomImage(<selector or element>) opens that image immediately */
    var el = (typeof on === 'string') ? doc.querySelector(on) : on;
    if (el && /^img$/i.test(el.tagName || '')) { return openZoom(el); }
    return false;
  }, false),

  /* --- extras the Qt side may use; not named by the contract ------------ */
  relayout: guard(function () {
    var loc = st.lastLoc;
    relayout();
    if (loc) { restore(loc); }
    report();
    return stateObj();
  }, null),

  setMode: guard(function (m) {
    var s = Object.assign({}, st.settings || {}, { layout: (m === 'scroll' || m === 'scrolled') ? 'scroll' : 'paged' });
    return applySettings(s, false);
  }, null),

  clearMatchesAndSelection: guard(function () {
    st.matches = []; st.matchActive = -1;
    hlClear('er-find'); hlClear('er-find-active');
    var s = window.getSelection(); if (s) { s.removeAllRanges(); }
    return true;
  }, true),

  hideNote: guard(function () { hideNote(); return true; }, true),
  closeZoom: guard(function () { return closeZoom(); }, false),

  /* polling fallback for hosts without a QWebChannel bridge */
  drainEvents: function () { return eventLog.splice(0); },

  /* diagnostics used by the owner-C harness; harmless in production */
  debug: guard(function () {
    return {
      W: st.W, H: st.H, gap: st.gap, half: st.half, colW: st.colW, step: st.step,
      padT: st.padT, padB: st.padB, pageH: st.pageH,
      pages: st.pages, page: st.page, mode: st.mode, rtl: st.rtl,
      vertical: st.vertical, verticalRL: st.verticalRL,
      fxl: st.fxl, fxlScale: st.fxlScale, fxlOffset: st.fxlOffset,
      scrollLeft: SE().scrollLeft, scrollTop: SE().scrollTop,
      scrollWidth: SE().scrollWidth, clientWidth: SE().clientWidth,
      scrollHeight: SE().scrollHeight, clientHeight: SE().clientHeight,
      sheets: BOOK_SHEETS.length, chars: flatLen(), nodes: st.flat ? st.flat.nodes.length : 0,
      highlightAPI: hlSupported(), lastLoc: st.lastLoc, tokens: st.tokens,
      queued: queue.length, inited: st.inited, booted: st.booted
    };
  }, null, false)
};

window.epubReader = API;
installInput();

/* Nothing else happens until Python calls epubReader.init(). */
})();
