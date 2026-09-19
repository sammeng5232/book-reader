/* ===========================================================================
   reader.js  —  reference implementation of the in-page reading layer.
   Injected once per chapter document, AFTER the book's own CSS has parsed.
   Exposes window.RDR for the Qt host to drive via runJavaScript().
   =========================================================================== */
(function () {
'use strict';
if (window.RDR) return;

const doc = document, de = doc.documentElement, body = doc.body;
const NS_OPS = 'http://www.idpf.org/2007/ops';

/* ---------- 0. layer order: declared FIRST so our !important wins ---------- */
(function order() {
  const s = doc.createElement('style');
  s.setAttribute('data-rdr', 'order');
  s.textContent = '@layer rdr-safety, rdr-theme;';
  de.insertBefore(s, de.firstChild);          // before <head> even
})();
function mkSheet(id) {
  const s = doc.createElement('style');
  s.setAttribute('data-rdr', id);
  (doc.head || de).appendChild(s);
  return s;
}
const shGeom = mkSheet('geom'), shTheme = mkSheet('theme'), shChrome = mkSheet('chrome');

/* ---------- 1. state ---------- */
const st = {
  mode: 'paginated',          // 'paginated' | 'scrolled' | 'fixed'
  gap: 64, padT: 44, padB: 44, maxTextWidth: 0,   // 0 = fill
  W: 0, H: 0, colW: 0, pageH: 0, step: 0, pad: 0,
  pages: 1, page: 0,
  rtl: false, vertical: false,
  fxl: null,
  flat: null,                 // {text, nodes, starts}
  ranges: [], cur: -1,
  lastLoc: null,              // locator refreshed on every settled page turn
};

/* ---------- 2. geometry ---------- */
function measure() {
  st.W = de.clientWidth || innerWidth;
  st.H = de.clientHeight || innerHeight;
  st.pad = Math.round(st.gap / 2);
  st.colW = st.maxTextWidth
    ? Math.min(st.W - st.gap, st.maxTextWidth)
    : st.W - st.gap;
  st.pageH = st.H - st.padT - st.padB;
  st.step = st.W;
  st.rtl = getComputedStyle(de).direction === 'rtl';
}
function geomCSS() {
  const { W, H, colW, gap, pad, padT, padB, pageH } = st;
  // when maxTextWidth clamps the column, pad the extra space symmetrically
  const sidePad = Math.round((W - colW) / 2);
  return `
@layer rdr-safety{
  html{margin:0!important;padding:0!important;border:0!important;
       width:${W}px!important;height:${H}px!important;
       overflow:hidden!important;position:static!important;
       column-count:auto!important;column-width:auto!important;
       background:var(--rdr-bg)!important;color:var(--rdr-fg)!important;
       transform:none!important;zoom:1!important;}
  html::after{content:''!important;display:block!important;position:absolute!important;
       top:0!important;left:0!important;width:var(--rdr-tail,0px)!important;height:1px!important;
       visibility:hidden!important;pointer-events:none!important;}
  body{box-sizing:border-box!important;margin:0!important;border:0!important;float:none!important;
       width:${W}px!important;height:${H}px!important;
       min-width:0!important;max-width:none!important;min-height:0!important;max-height:none!important;
       padding:${padT}px ${sidePad}px ${padB}px!important;
       column-width:${colW}px!important;column-count:auto!important;
       column-gap:${gap}px!important;column-fill:auto!important;column-rule:none!important;
       overflow:visible!important;position:static!important;display:block!important;
       background:transparent!important;transform:none!important;zoom:1!important;
       writing-mode:horizontal-tb!important;}
  /* replaced content can never exceed one page, and never distorts */
  img,svg,video,canvas,object,embed,iframe,picture{
       max-width:100%!important;max-height:${pageH}px!important;
       width:auto!important;height:auto!important;object-fit:contain!important;
       box-sizing:border-box!important;position:static!important;}
  /* nothing may declare itself monolithic: a box taller than a column would bleed */
  :where(figure,table,pre,blockquote,div,section,article,aside,li,p,tr,td,th,
         h1,h2,h3,h4,h5,h6,dl,dd,ol,ul){break-inside:auto!important;}
  /* long code lines must wrap, not overflow the column into the next page */
  pre{white-space:pre-wrap!important;overflow-wrap:break-word!important;
      word-break:break-word!important;overflow:visible!important;max-width:100%!important;}
  table{max-width:100%!important;width:auto!important;table-layout:auto!important;}
  td,th{white-space:normal!important;word-break:break-word!important;}
  .__rdr-hscroll{max-width:100%!important;overflow-x:auto!important;overflow-y:hidden!important;}
}`;
}
function scrolledCSS() {
  const { W, gap, padT, padB, colW } = st;
  const sidePad = Math.round((W - colW) / 2);
  return `
@layer rdr-safety{
  html{margin:0!important;padding:0!important;width:${W}px!important;height:auto!important;
       overflow-x:hidden!important;overflow-y:auto!important;position:static!important;
       column-count:auto!important;column-width:auto!important;
       background:var(--rdr-bg)!important;color:var(--rdr-fg)!important;
       scrollbar-width:thin!important;}
  html::after{content:none!important;}
  body{box-sizing:border-box!important;margin:0!important;width:${W}px!important;
       height:auto!important;max-height:none!important;
       padding:${padT}px ${sidePad}px ${padB * 3}px!important;
       column-count:1!important;column-width:auto!important;
       overflow:visible!important;position:static!important;display:block!important;
       background:transparent!important;}
  img,svg,video,canvas,object,embed,iframe,picture{
       max-width:100%!important;height:auto!important;width:auto!important;object-fit:contain!important;}
  pre{white-space:pre-wrap!important;overflow-wrap:break-word!important;}
  table{max-width:100%!important;}
}`;
}

/* ---------- 3. theme (loses to book CSS on purpose — lives in a layer) ---------- */
const THEMES = {
  light : { bg:'#fbf8f3', fg:'#26211c', link:'#1a5fb4', sel:'#b7dcff', scheme:'light' },
  sepia : { bg:'#f4ecd8', fg:'#3b2f1e', link:'#8a4b1a', sel:'#e3cfa3', scheme:'light' },
  gray  : { bg:'#d6d3cd', fg:'#22201d', link:'#2a5d9f', sel:'#b9c9de', scheme:'light' },
  dark  : { bg:'#15181b', fg:'#d4d0c8', link:'#79b8ff', sel:'#2d4a6b', scheme:'dark' },
  black : { bg:'#000000', fg:'#b9b5ae', link:'#6fb1f0', sel:'#233247', scheme:'dark' },
};
const FONTS = {
  // Latin first, CJK second: Latin glyphs come from the Latin face,
  // Han/kana/CJK punctuation fall through to the CJK face.
  serif      : 'Georgia,"Sitka Text",Cambria,"Noto Serif SC","Source Han Serif SC",serif',
  sans       : '"Segoe UI Variable Text","Segoe UI","Noto Sans SC","Microsoft YaHei UI",sans-serif',
  songti     : '"Sitka Text",Georgia,"Noto Serif SC",SimSun,"宋体",serif',
  heiti      : '"Segoe UI","Noto Sans SC","Microsoft YaHei","微软雅黑",sans-serif',
  kaiti      : 'Georgia,KaiTi,"楷体",STKaiti,"华文楷体",serif',
  fangsong   : 'Georgia,FangSong,"仿宋",STFangsong,serif',
  mono       : 'Consolas,"Cascadia Mono","Noto Sans SC",monospace',
  bookOwn    : null,          // keep whatever the book asked for
};
function themeCSS() {
  return `
@layer rdr-theme{
  :root{ font-size: var(--rdr-fs,19px); color-scheme: var(--rdr-scheme,light); }
  html{ font-family: var(--rdr-ff);
        line-height: var(--rdr-lh,1.75);
        color: var(--rdr-fg); background: var(--rdr-bg);
        text-align: var(--rdr-align,start);
        text-spacing-trim: space-first;      /* CJK punctuation compression   */
        text-autospace: normal;              /* 1/4em between Han and Latin   */
        line-break: strict;                  /* correct CJK line-break rules  */
        overflow-wrap: break-word;
        hyphens: var(--rdr-hyphens,manual);
        text-wrap: pretty;
        -webkit-font-smoothing: antialiased; }
  body{ font-size: 1rem; }
  p,li,dd{ line-height: var(--rdr-lh,1.75); }
  h1,h2,h3,h4,h5,h6{ line-height:1.3; text-wrap:balance; }
  a{ color: var(--rdr-link); text-decoration-thickness:.06em; text-underline-offset:.15em; }
  code,pre,kbd,samp,tt{ font-family: var(--rdr-mono); font-size:.92em; }
  hr{ border:0; border-top:1px solid currentColor; opacity:.28; }
  ::selection{ background: var(--rdr-sel); }
}`;
}
function chromeCSS() {
  return `
::highlight(rdr-find){ background:#ffd54a; color:#111; }
::highlight(rdr-find-cur){ background:#ff7a1a; color:#fff; }
#__rdr-pop{ position:fixed; margin:0; inset:auto; z-index:2147483647;
  max-width:min(440px,78vw); max-height:44vh; overflow:auto;
  padding:12px 15px; border:1px solid color-mix(in srgb, var(--rdr-fg) 28%, transparent);
  border-radius:10px; background:var(--rdr-bg); color:var(--rdr-fg);
  box-shadow:0 10px 34px rgba(0,0,0,.34); font-size:.92rem; line-height:1.65;
  font-family:var(--rdr-ff); }
#__rdr-pop::backdrop{ background:transparent; }
#__rdr-pop p{ margin:.3em 0; text-indent:0; }
#__rdr-fade{ position:fixed; inset:0; z-index:2147483646; pointer-events:none;
  background:var(--rdr-bg); opacity:0; transition:opacity .10s linear; }
@media (prefers-reduced-motion: reduce){ #__rdr-fade{ transition:none; } }`;
}
shChrome.textContent = chromeCSS();

/* ---------- 4. book-stylesheet rewriting (the honest way to theme) ---------- */
const BOOK_SHEETS = [];
function collectSheets() {
  BOOK_SHEETS.length = 0;
  for (const sh of doc.styleSheets) {
    const on = sh.ownerNode;
    if (on && on.getAttribute && on.getAttribute('data-rdr') !== null) continue;
    try { void sh.cssRules; BOOK_SHEETS.push(sh); } catch (e) { /* cross-origin */ }
  }
}
/* NB: in Chromium >=112 a CSSStyleRule ALSO has .cssRules (CSS nesting),
   so you must test r.style FIRST and only recurse when .cssRules.length > 0. */
function eachStyleRule(rules, fn) {
  for (const r of rules) {
    if (r.style) fn(r);
    if (r.cssRules && r.cssRules.length) eachStyleRule(r.cssRules, fn);
  }
}
const ROOT_SEL = /(^|,)\s*(html|body|:root)\s*(,|$)/i;

/* 4a. px/pt font-size -> rem, so the user's size scales the book's hierarchy */
let fontSizesNormalised = false;
function normaliseFontSizes() {
  if (fontSizesNormalised) return 0;
  let n = 0;
  for (const sh of BOOK_SHEETS) eachStyleRule(sh.cssRules, r => {
    const v = r.style.getPropertyValue('font-size'); if (!v) return;
    const m = /^\s*([\d.]+)(px|pt)\s*$/.exec(v); if (!m) return;
    const px = m[2] === 'pt' ? parseFloat(m[1]) * 4 / 3 : parseFloat(m[1]);
    r.style.setProperty('font-size', (px / 16).toFixed(4) + 'rem',
                        r.style.getPropertyPriority('font-size'));
    n++;
  });
  fontSizesNormalised = true;
  return n;
}
/* 4b. lift the book's root font-family / line-height so the user's choice applies */
const famUndo = [];
function releaseRootTypography(on) {
  if (on) {
    if (famUndo.length) return;
    for (const sh of BOOK_SHEETS) eachStyleRule(sh.cssRules, r => {
      if (!ROOT_SEL.test(r.selectorText || '')) return;
      for (const p of ['font-family', 'line-height', 'text-align', 'font-size']) {
        const v = r.style.getPropertyValue(p);
        if (v) { famUndo.push([r, p, v, r.style.getPropertyPriority(p)]);
                 r.style.removeProperty(p); }
      }
    });
  } else {
    for (const [r, p, v, pr] of famUndo.splice(0).reverse()) r.style.setProperty(p, v, pr);
  }
}
/* 4c. colour remap for dark themes — keeps hue, flips lightness.
       This is what you do INSTEAD of `*{color:X!important}`. */
const COLOR_PROPS = ['color','background-color','border-color','border-top-color',
  'border-right-color','border-bottom-color','border-left-color','outline-color',
  'text-decoration-color','column-rule-color','caret-color','fill','stroke'];
const colorUndo = [];
const _probe = doc.createElement('span');
function resolveColor(v) {
  _probe.style.cssText = ''; _probe.style.setProperty('color', v);
  if (!_probe.style.color) return null;
  de.appendChild(_probe);
  const c = getComputedStyle(_probe).color;
  _probe.remove();
  return c;
}
function parseRGB(c) {
  const m = /^rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)(?:[,/\s]+([\d.%]+))?\s*\)$/.exec(c || '');
  if (!m) return null;
  const a = m[4] === undefined ? 1
          : String(m[4]).endsWith('%') ? parseFloat(m[4]) / 100 : +m[4];
  return [+m[1], +m[2], +m[3], a];
}
function flipLightness(css, darkness) {
  const p = parseRGB(resolveColor(css)); if (!p) return null;
  const [r, g, b, a] = p; if (a === 0) return null;
  const rr = r/255, gg = g/255, bb = b/255;
  const mx = Math.max(rr,gg,bb), mn = Math.min(rr,gg,bb), d = mx - mn;
  let h = 0, s = 0; const l = (mx + mn) / 2;
  if (d) {
    s = l > .5 ? d / (2 - mx - mn) : d / (mx + mn);
    h = mx === rr ? ((gg-bb)/d + (gg < bb ? 6 : 0)) : mx === gg ? ((bb-rr)/d + 2) : ((rr-gg)/d + 4);
    h *= 60;
  }
  const nl = darkness.hi - l * (darkness.hi - darkness.lo);   // flip + compress
  const ns = Math.min(s, .5);
  return `hsl(${h.toFixed(0)} ${(ns*100).toFixed(0)}% ${(nl*100).toFixed(0)}%` +
         (a < 1 ? ` / ${a.toFixed(2)}` : '') + ')';
}
function recolourBook(on) {
  if (!on) { for (const [r,p,v,pr] of colorUndo.splice(0).reverse()) r.style.setProperty(p,v,pr); return 0; }
  if (colorUndo.length) return 0;
  const D = { hi: .92, lo: .12 };
  let n = 0;
  for (const sh of BOOK_SHEETS) eachStyleRule(sh.cssRules, r => {
    /* CRITICAL: snapshot every declaration BEFORE writing any of them.
       Writing the `background` shorthand expands into `background-color`,
       so a naive read-modify-write loop flips the same colour twice. */
    const snap = [];
    const bg = r.style.getPropertyValue('background');
    if (bg) snap.push(['background', bg, r.style.getPropertyPriority('background')]);
    for (const p of COLOR_PROPS) {
      if (bg && (p === 'background-color')) continue;        // covered by the shorthand
      const v = r.style.getPropertyValue(p);
      if (!v || v === 'transparent' || v === 'currentcolor' || /var\(/i.test(v)) continue;
      snap.push([p, v, r.style.getPropertyPriority(p)]);
    }
    for (const [p, v, pr] of snap) {
      let nv;
      if (p === 'background' && /url\(/i.test(v)) {
        colorUndo.push([r, 'background', v, pr]);
        r.style.setProperty('background-color', 'transparent', pr); n++; continue;
      }
      nv = flipLightness(v, D);
      if (!nv) { if (p === 'background') { colorUndo.push([r, p, v, pr]);
                   r.style.setProperty(p, 'transparent', pr); n++; } continue; }
      colorUndo.push([r, p, v, pr]);
      r.style.setProperty(p, nv, pr);
      n++;
    }
  });
  return n;
}

/* ---------- 5. one-time DOM defences ---------- */
let defended = false;
function defend() {
  if (defended) return; defended = true;
  // 5a. position:fixed would float over every page
  doc.querySelectorAll('body *').forEach(el => {
    if (getComputedStyle(el).position === 'fixed')
      el.style.setProperty('position', 'absolute', 'important');
  });
  // 5b. Chromium ignores `break-*: page` inside multicol — translate the author's intent
  doc.querySelectorAll('body *').forEach(el => {
    const cs = getComputedStyle(el);
    if (cs.breakBefore === 'page') el.style.setProperty('break-before', 'column', 'important');
    if (cs.breakAfter  === 'page') el.style.setProperty('break-after',  'column', 'important');
  });
  // 5c. neutralise the book's own scripts' scroll hijacking
  try { de.style.setProperty('overflow-anchor', 'none'); } catch (e) {}
}
function tameWideBlocks() {           // run after every relayout
  doc.querySelectorAll('table:not(.__rdr-hscroll>table)').forEach(t => {
    if (t.parentElement && t.parentElement.classList.contains('__rdr-hscroll')) return;
    if (t.getBoundingClientRect().width > st.colW + 1) {
      const w = doc.createElement('div'); w.className = '__rdr-hscroll';
      t.parentNode.insertBefore(w, t); w.appendChild(t);
    }
  });
}

/* ---------- 6. layout / pagination ---------- */
function relayout() {
  measure();
  if (st.mode === 'fixed') { applyFXL(); return; }
  shGeom.textContent = st.mode === 'scrolled' ? scrolledCSS() : geomCSS();
  if (st.mode === 'scrolled') { st.pages = 1; st.page = 0; buildFlat(); return; }
  de.style.setProperty('--rdr-tail', '0px');
  void de.scrollWidth;                                   // force layout
  const raw = de.scrollWidth;
  st.pages = Math.max(1, Math.round((raw + st.pad) / st.step));
  de.style.setProperty('--rdr-tail', (st.pages * st.step) + 'px');
  void de.scrollWidth;
  tameWideBlocks();
  buildFlat();
  // keep the scroller on an exact page boundary after any relayout
  const p = Math.max(0, Math.min(st.pages - 1, Math.round(Math.abs(de.scrollLeft) / st.step)));
  de.scrollLeft = sgn() * p * st.step; st.page = p;
}
const sgn = () => (st.rtl ? -1 : 1);
function curPage() { return Math.round(Math.abs(de.scrollLeft) / st.step); }
function gotoPage(i, animate) {
  i = Math.max(0, Math.min(st.pages - 1, i | 0));
  const target = sgn() * i * st.step;
  if (animate) fade(() => { de.scrollLeft = target; });
  else de.scrollLeft = target;
  st.page = i;
  /* Pin the locator to the last USER navigation only. Re-capturing after a
     restore would re-anchor to the top of the restored page and drift a little
     further on every reflow. */
  if (!restoring) { const l = capture(); if (l) st.lastLoc = l; }
  report();
  return i;
}
let fadeEl = null, fadeT = 0;
function fade(fn) {
  if (matchMedia('(prefers-reduced-motion: reduce)').matches) { fn(); return; }
  if (!fadeEl) { fadeEl = doc.createElement('div'); fadeEl.id = '__rdr-fade'; de.appendChild(fadeEl); }
  fadeEl.style.opacity = '1';
  clearTimeout(fadeT);
  requestAnimationFrame(() => { fn(); requestAnimationFrame(() => { fadeEl.style.opacity = '0'; }); });
}

/* ---------- 7. position model: "CFI-lite" ---------- */
function buildFlat() {
  const tw = doc.createTreeWalker(body, NodeFilter.SHOW_TEXT, { acceptNode(n) {
    if (!n.nodeValue) return NodeFilter.FILTER_REJECT;
    const pe = n.parentElement; if (!pe) return NodeFilter.FILTER_REJECT;
    const t = pe.tagName;
    if (t === 'SCRIPT' || t === 'STYLE' || t === 'NOSCRIPT' || t === 'TEMPLATE')
      return NodeFilter.FILTER_REJECT;
    return NodeFilter.FILTER_ACCEPT;
  }});
  const nodes = [], starts = [], solid = []; let s = '', n;
  while ((n = tw.nextNode())) {
    starts.push(s.length);
    // whitespace-only nodes between block elements generate NO boxes; they would
    // return -1 from pageOfNode and silently break the binary search below.
    if (/\S/.test(n.nodeValue)) solid.push(nodes.length);
    nodes.push(n); s += n.nodeValue;
  }
  st.flat = { text: s, nodes, starts, solid };
}
function locate(i) {                       // flat char index -> [textNode, offset]
  const { starts, nodes } = st.flat;
  let lo = 0, hi = starts.length - 1, a = 0;
  while (lo <= hi) { const m = (lo + hi) >> 1; if (starts[m] <= i) { a = m; lo = m + 1; } else hi = m - 1; }
  return [nodes[a], i - starts[a]];
}
function firstRect(node, off, len) {
  const r = doc.createRange();
  const L = node.nodeValue.length;
  const a = Math.max(0, Math.min(off, L - 1));
  r.setStart(node, a); r.setEnd(node, Math.min(a + (len || 1), L));
  const rc = r.getClientRects();
  return rc.length ? rc[0] : null;
}
/* page index of a viewport rect, direction-aware */
function pageOfRect(rc) {
  if (!rc) return -1;
  // Use the EXACT scroll offset, never a rounded page index: right after a
  // relayout scrollLeft is generally not a multiple of the new step, and
  // `curPage() + ...` then drifts by up to half a page.
  const d = st.rtl ? (st.W - rc.right) : rc.left;   // distance from the start edge
  const docX = d + Math.abs(de.scrollLeft);         // position in document space
  return Math.floor((docX + 1) / st.step);
}
function pageOfChar(ci) { const [n, o] = locate(ci); return pageOfRect(firstRect(n, o)); }
function nodePages(node) {                 // [firstPage, lastPage] or null
  const r = doc.createRange(); r.selectNodeContents(node);
  const rc = r.getClientRects();
  if (!rc.length) return null;
  return [pageOfRect(rc[0]), pageOfRect(rc[rc.length - 1])];
}
/* capture: the first character actually visible on the current page */
function capture() {
  if (!st.flat) return null;                       // not laid out yet
  if (st.mode !== 'paginated') {
    const y = de.scrollTop;
    return { spineChar: null, scrollFrac: de.scrollHeight > de.clientHeight
             ? y / (de.scrollHeight - de.clientHeight) : 0, mode: st.mode };
  }
  const { nodes, starts, solid } = st.flat, cur = curPage();
  if (!solid.length) return null;
  /* Outer search on the node's LAST page, so a node that starts on an earlier
     page but spans onto `cur` is still selected. */
  let lo = 0, hi = solid.length - 1, ans = -1;
  while (lo <= hi) {                                   // pages are monotonic in doc order
    const m = (lo + hi) >> 1, pp = nodePages(nodes[solid[m]]);
    if (pp && pp[1] >= cur) { ans = m; hi = m - 1; } else lo = m + 1;
  }
  const ni = solid[ans < 0 ? solid.length - 1 : ans];
  const nd = nodes[ni];
  let l2 = 0, h2 = nd.nodeValue.length - 1, best = 0;   // binary search inside the node
  while (l2 <= h2) {
    const m = (l2 + h2) >> 1, rc = firstRect(nd, m);
    if (!rc) { l2 = m + 1; continue; }
    if (pageOfRect(rc) >= cur) { best = m; h2 = m - 1; } else l2 = m + 1;
  }
  const ci = starts[ni] + best;
  /* A page may contain no text at all (a full-page image). The located character
     then lives on a LATER page; remember the gap so restore does not creep forward. */
  const locPage = pageOfRect(firstRect(nd, best));
  return {
    char: ci,                                  // primary locator
    pageDelta: (locPage >= 0 ? cur - locPage : 0),
    total: st.flat.text.length,
    frac: st.flat.text.length ? ci / st.flat.text.length : 0,
    page: cur, pages: st.pages,
    path: pathOf(nd, best),                    // structural fallback
    text: st.flat.text.substr(ci, 32),         // human-readable fallback
  };
}
let restoring = false;
function restore(loc) {
  if (!loc || !st.flat) return 0;
  restoring = true;
  try { return restoreInner(loc); } finally { restoring = false; }
}
function restoreInner(loc) {
  if (st.mode !== 'paginated') {
    const f = loc.frac != null ? loc.frac : (loc.scrollFrac || 0);
    de.scrollTop = f * (de.scrollHeight - de.clientHeight);
    return 0;
  }
  let p = -1;
  if (typeof loc.char === 'number' && loc.char < st.flat.text.length) p = pageOfChar(loc.char);
  if (p < 0 && loc.path) { const n = resolvePath(loc.path); if (n) p = pageOfRect(firstRect(n, loc.path.offset || 0)); }
  if (p < 0 && loc.text) { const i = st.flat.text.indexOf(loc.text.slice(0, 16)); if (i >= 0) p = pageOfChar(i); }
  if (p < 0 && typeof loc.frac === 'number') p = Math.round(loc.frac * (st.pages - 1));
  if (p >= 0 && loc.pageDelta) p += loc.pageDelta;
  return gotoPage(Math.max(0, Math.min(st.pages - 1, p)), false);
}
/* structural path: /idx/idx/... up to the nearest id'd ancestor  (CFI-lite) */
function pathOf(node, offset) {
  const steps = []; let n = node, anchor = null;
  while (n && n !== body) {
    const parent = n.parentNode; if (!parent) break;
    steps.unshift(Array.prototype.indexOf.call(parent.childNodes, n));
    if (parent.nodeType === 1 && parent.id) { anchor = parent.id; break; }
    n = parent;
  }
  return { anchor, steps, offset };
}
function resolvePath(loc) {
  let n = loc.anchor ? doc.getElementById(loc.anchor) : body;
  if (!n) return null;
  for (const i of loc.steps) { if (!n.childNodes[i]) return null; n = n.childNodes[i]; }
  return n && n.nodeType === 3 ? n : null;
}

/* ---------- 8. search ---------- */
function normalise(s) { return s.toLowerCase().replace(/[‘’]/g, "'").replace(/[“”]/g, '"'); }
function find(q) {
  clearFind();
  if (!q) return 0;
  const hay = normalise(st.flat.text), needle = normalise(q);
  if (!needle) return 0;
  const out = [];
  let i = hay.indexOf(needle);
  while (i >= 0 && out.length < 4000) { out.push([i, i + needle.length]); i = hay.indexOf(needle, i + needle.length); }
  st.ranges = out.map(([a, b]) => {
    const r = doc.createRange();
    const [n1, o1] = locate(a), [n2, o2] = locate(b - 1);
    r.setStart(n1, o1); r.setEnd(n2, Math.min(o2 + 1, n2.nodeValue.length));
    return r;
  });
  if (st.ranges.length) CSS.highlights.set('rdr-find', new Highlight(...st.ranges));
  st.cur = -1;
  return st.ranges.length;
}
function findStep(delta) {
  if (!st.ranges.length) return null;
  st.cur = (st.cur + delta + st.ranges.length) % st.ranges.length;
  const r = st.ranges[st.cur];
  CSS.highlights.set('rdr-find-cur', new Highlight(r.cloneRange()));
  const rc = r.getClientRects()[0];
  const p = pageOfRect(rc);
  if (p >= 0) gotoPage(p, false);
  return { index: st.cur, total: st.ranges.length, page: st.page };
}
function clearFind() {
  CSS.highlights.delete('rdr-find'); CSS.highlights.delete('rdr-find-cur');
  st.ranges = []; st.cur = -1;
}

/* ---------- 9. footnote popovers ---------- */
function epubType(el) {
  return el.getAttributeNS(NS_OPS, 'type') || el.getAttribute('epub:type') || '';
}
function isNoteref(a) {
  return /\bnoteref\b/.test(epubType(a)) || a.getAttribute('role') === 'doc-noteref';
}
let pop = null;
function showNote(anchor) {
  const href = anchor.getAttribute('href') || '';
  if (!href.startsWith('#')) return false;
  const tgt = doc.getElementById(decodeURIComponent(href.slice(1)));
  if (!tgt) return false;
  hideNote();
  pop = doc.createElement('div');
  pop.id = '__rdr-pop'; pop.setAttribute('popover', 'manual');
  const clone = tgt.cloneNode(true);
  clone.removeAttribute('id');
  clone.querySelectorAll('[id]').forEach(e => e.removeAttribute('id'));
  clone.querySelectorAll('a[href^="#"]').forEach(e => e.replaceWith(...e.childNodes)); // kill back-links
  pop.appendChild(clone);
  de.appendChild(pop);                       // NOT body-in-flow: it is position:fixed
  try { pop.showPopover(); } catch (e) { pop.style.display = 'block'; }
  const a = anchor.getBoundingClientRect(), pr = pop.getBoundingClientRect();
  const x = Math.max(8, Math.min(st.W - pr.width - 8, a.left - 12));
  const y = (a.bottom + 10 + pr.height <= st.H) ? a.bottom + 10 : Math.max(8, a.top - 10 - pr.height);
  pop.style.left = Math.round(x) + 'px'; pop.style.top = Math.round(y) + 'px';
  return true;
}
function hideNote() {
  if (!pop) return;
  try { pop.hidePopover(); } catch (e) {}
  pop.remove(); pop = null;
}

/* ---------- 10. fixed layout ---------- */
function detectFXL() {
  const m = doc.querySelector('meta[name="viewport"]');
  if (m) {
    const c = m.getAttribute('content') || '';
    const w = /(?:^|[;,\s])width\s*=\s*([\d.]+)/.exec(c);
    const h = /(?:^|[;,\s])height\s*=\s*([\d.]+)/.exec(c);
    if (w && h) return { w: +w[1], h: +h[1], src: 'meta' };
  }
  const svg = doc.querySelector('body > svg[viewBox]');
  if (svg) { const v = svg.getAttribute('viewBox').split(/[\s,]+/).map(Number);
             if (v.length === 4 && v[2] && v[3]) return { w: v[2], h: v[3], src: 'svg' }; }
  return null;
}
function applyFXL() {
  const f = st.fxl; if (!f) return;
  const s = Math.min(st.W / f.w, st.H / f.h);
  const tx = Math.round((st.W - f.w * s) / 2), ty = Math.round((st.H - f.h * s) / 2);
  shGeom.textContent = `
@layer rdr-safety{
  html{margin:0!important;padding:0!important;width:${st.W}px!important;height:${st.H}px!important;
       overflow:hidden!important;background:var(--rdr-bg)!important;}
  html::after{content:none!important;}
  body{margin:0!important;padding:0!important;
       width:${f.w}px!important;height:${f.h}px!important;
       min-width:0!important;max-width:none!important;min-height:0!important;max-height:none!important;
       columns:auto!important;column-count:auto!important;column-width:auto!important;
       overflow:hidden!important;position:relative!important;
       transform-origin:0 0!important;
       transform:translate(${tx}px,${ty}px) scale(${s.toFixed(6)})!important;}
  img,svg,video,canvas{max-width:none!important;max-height:none!important;}
}`;
  st.pages = 1; st.page = 0;
}

/* ---------- 11. input ---------- */
const KEYMAP = {
  'ArrowRight': () => turn(1), 'ArrowDown': () => turn(1), 'PageDown': () => turn(1),
  ' ': () => turn(1), 'Enter': () => turn(1),
  'ArrowLeft': () => turn(-1), 'ArrowUp': () => turn(-1), 'PageUp': () => turn(-1),
  'Home': () => gotoPage(0, true), 'End': () => gotoPage(st.pages - 1, true),
};
function turn(d) {
  const want = st.page + d;
  if (want < 0)            { host('prevChapter'); return; }
  if (want > st.pages - 1) { host('nextChapter'); return; }
  gotoPage(want, true);
}
function host(name, data) {
  // Qt side installs window.__rdrHost (QWebChannel) or polls; console fallback:
  if (window.__rdrHost) { try { window.__rdrHost(name, data == null ? null : JSON.stringify(data)); } catch (e) {} }
  else document.title = 'rdr:' + name + ':' + JSON.stringify(data == null ? null : data);
}
function report() { host('pos', { page: st.page, pages: st.pages, loc: st.lastLoc }); }

function installInput() {
  // capture phase + stopImmediatePropagation => the book's own handlers never see it
  doc.addEventListener('keydown', e => {
    if (e.ctrlKey || e.altKey || e.metaKey) return;
    const t = e.target;
    if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
    const fn = KEYMAP[e.key]; if (!fn) return;
    e.preventDefault(); e.stopImmediatePropagation();
    if (e.key === ' ' && e.shiftKey) turn(-1); else fn();
  }, true);

  let acc = 0, wheelT = 0;
  doc.addEventListener('wheel', e => {
    if (st.mode !== 'paginated') return;
    e.preventDefault(); e.stopImmediatePropagation();
    acc += (Math.abs(e.deltaY) >= Math.abs(e.deltaX) ? e.deltaY : e.deltaX);
    const now = performance.now();
    if (Math.abs(acc) >= 40 && now - wheelT > 110) { turn(acc > 0 ? 1 : -1); acc = 0; wheelT = now; }
  }, { capture: true, passive: false });

  let px = 0, py = 0, pid = null, moved = false, downT = 0;
  doc.addEventListener('pointerdown', e => {
    pid = e.pointerId; px = e.clientX; py = e.clientY; moved = false; downT = performance.now();
  }, true);
  doc.addEventListener('pointermove', e => {
    if (e.pointerId !== pid) return;
    if (Math.abs(e.clientX - px) > 8 || Math.abs(e.clientY - py) > 8) moved = true;
  }, true);
  doc.addEventListener('pointerup', e => {
    if (e.pointerId !== pid) return; pid = null;
    const dx = e.clientX - px, dy = e.clientY - py, dt = performance.now() - downT;
    if (pop && !pop.contains(e.target)) { hideNote(); return; }
    // swipe
    if (moved && Math.abs(dx) > 55 && Math.abs(dx) > Math.abs(dy) * 1.6 && dt < 700) {
      e.preventDefault(); e.stopImmediatePropagation(); turn(dx < 0 ? 1 : -1); return;
    }
    if (moved) return;
    // noteref / link
    const a = e.target && e.target.closest && e.target.closest('a[href]');
    if (a) {
      if (isNoteref(a) && showNote(a)) { e.preventDefault(); e.stopImmediatePropagation(); return; }
      const href = a.getAttribute('href') || '';
      e.preventDefault(); e.stopImmediatePropagation();
      host('link', { href, internal: href.startsWith('#') });
      return;
    }
    if (getSelection && String(getSelection()).length) return;   // don't turn while selecting
    // click zones: 22% / 56% / 22%
    const z = e.clientX / st.W;
    if (z < .22) turn(-1); else if (z > .78) turn(1); else host('tapCentre');
  }, true);

  doc.addEventListener('contextmenu', e => {
    e.preventDefault();
    host('context', { x: e.clientX, y: e.clientY, selection: String(getSelection() || '') });
  }, true);

  // the book's scripts may try to scroll; snap back
  doc.addEventListener('scroll', () => {
    if (st.mode !== 'paginated') return;
    if (de.scrollTop !== 0) de.scrollTop = 0;
    const p = curPage();
    if (p !== st.page && !restoring) {
      st.page = p; const l = capture(); if (l) st.lastLoc = l; report();
    }
  }, { passive: true });

  /* NB: a ResizeObserver on <html> or <body> is USELESS here — we pin both to
     explicit px, so their observed box never changes. Listen to the window. */
  let rzT = 0;
  function onResize() {
    /* Do NOT capture here. By the time a resize event is delivered the document
       has already reflowed (book CSS using vh/vw, media queries, :root font
       metrics), so a capture taken now describes the NEW layout while our page
       arithmetic still describes the old one. Use the locator stored at the last
       settled page turn instead. */
    clearTimeout(rzT);
    rzT = setTimeout(() => {
      const loc = st.lastLoc;
      relayout();
      if (loc) restore(loc);
      report();
    }, 120);
  }
  addEventListener('resize', onResize);
  if (window.visualViewport) visualViewport.addEventListener('resize', onResize);
}

/* ---------- 12. public API (called from Qt via runJavaScript) ---------- */
const API = {
  init(opts) {
    Object.assign(st, opts || {});
    collectSheets();
    st.fxl = (opts && opts.forceFXL) ? (opts.fxl || detectFXL()) : (opts && opts.layout === 'pre-paginated' ? detectFXL() : null);
    if (st.fxl) st.mode = 'fixed';
    const wm = getComputedStyle(body).writingMode || '';
    if (wm.startsWith('vertical')) st.mode = 'scrolled';     // known limitation
    normaliseFontSizes();
    defend();
    measure();
    shGeom.textContent = geomCSS();
    this.setTheme((opts && opts.theme) || {});
    installInput();
    report();
    return { mode: st.mode, pages: st.pages, fxl: st.fxl, vertical: wm.startsWith('vertical') };
  },
  setTheme(t) {
    const th = THEMES[t.name || 'light'] || THEMES.light;
    const set = (k, v) => de.style.setProperty(k, v);
    set('--rdr-bg', t.bg || th.bg); set('--rdr-fg', t.fg || th.fg);
    set('--rdr-link', t.link || th.link); set('--rdr-sel', t.sel || th.sel);
    set('--rdr-scheme', th.scheme);
    if (t.font !== undefined) {
      const ff = FONTS[t.font] || t.font;
      releaseRootTypography(!!ff);
      if (ff) set('--rdr-ff', ff);
    }
    set('--rdr-mono', FONTS.mono);
    if (t.fontSize)   set('--rdr-fs', t.fontSize + 'px');
    if (t.lineHeight) set('--rdr-lh', String(t.lineHeight));
    if (t.align)      set('--rdr-align', t.align);
    if (t.hyphens)    set('--rdr-hyphens', t.hyphens);
    if (t.gap != null)   st.gap = t.gap;
    if (t.maxWidth != null) st.maxTextWidth = t.maxWidth;
    if (t.padT != null) st.padT = t.padT;
    if (t.padB != null) st.padB = t.padB;
    shTheme.textContent = themeCSS();
    recolourBook(false);
    if (th.scheme === 'dark') recolourBook(true);
    const loc = st.lastLoc || capture();      // stored: taken before any var changed
    relayout(); if (loc) restore(loc);
    return { pages: st.pages, page: st.page };
  },
  setMode(m) { const loc = st.lastLoc || capture(); st.mode = m; relayout(); if (loc) restore(loc); return st.pages; },
  goto: (i) => gotoPage(i, false),
  turn, page: () => st.page, pages: () => st.pages,
  capture, restore, find, findStep, clearFind, relayout,
  gotoAnchor(id) {
    const el = doc.getElementById(id) || doc.getElementsByName(id)[0];
    if (!el) return -1;
    const r = el.getBoundingClientRect();
    return gotoPage(Math.max(0, pageOfRect(r)), false);
  },
  progress() {
    const c = capture() || {};
    return { page: st.page, pages: st.pages, frac: c.frac != null ? c.frac : 0, chars: st.flat ? st.flat.text.length : 0 };
  },
};
window.RDR = API;
})();
