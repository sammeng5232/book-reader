# Research: reading-ux-engine

## Summary

I built a Qt WebEngine probe harness in C:\Users\mengz\epub-reader\_proto2 and empirically settled every question against the actual Chromium 140.0.7339.225 that ships in PySide6 6.11.1, then wrote and iteratively debugged a complete reference implementation (reader.js, ~36 KB) that passes all tests. PAGINATION: CSS multi-column with the invariant `column-width = W - gap`, `column-gap = gap`, `body padding-inline = gap/2`, `column-fill:auto`, `html{overflow:hidden}` — this makes page step exactly == viewport width; `documentElement.scrollLeft` is still settable under `overflow:hidden` and is 300x faster than transform-based paging on long chapters (1.1 ms vs 343 ms for 60 turns on a 159,268 px-wide layout). The last page cannot be reached by scrolling (max scroll is short by gap/2); I fixed that with a hidden `html::after` absolute spacer of width N*step, verified exact. THEMING: I verified CSS Cascade 5 layer semantics in this engine — an `!important` declaration in the FIRST-declared `@layer` beats an unlayered `!important` from the book, while a NORMAL declaration in a layer loses to unlayered book CSS. That single fact gives the whole architecture: a `rdr-safety` layer with `!important` for box-model/pagination that the book can never override, and a `rdr-theme` layer that the book freely overrides so italics, small-caps, poetry indents and semantic colours survive. Dark mode is done by rewriting the book's own stylesheets through CSSOM (hue-preserving lightness flip), never `*{color:X!important}` — measured contrast went from 1.13 (broken) to 11.55. Font size is made to scale the book's hierarchy by rewriting px/pt font-sizes to rem via CSSOM; the h1/body ratio stayed exactly 2.000 across 16→26 px. POSITION: a "CFI-lite" locator (flat character offset + structural path + text snippet + pageDelta) round-trips all 7 pages of a hostile fixture exactly, survives light→dark→sepia and a 15→19→24→30→19 px font sweep with byte-identical anchor, and survives four real window resizes (1500x950, 560x1000, 1100x620, back to 900x700) returning to the exact same character. SEARCH: the CSS Custom Highlight API IS available and causes zero reflow. Fixed-layout scaling, footnote popovers (native Popover API), and input capture all verified.


## Verified facts

1. ENGINE: PySide6 6.11.1 reports QtWebEngine 6.11.1 / Chromium 140.0.7339.225 (proven by running `QtWebEngineCore.qWebEngineChromiumVersion()`). Page UA string says Chrome/140.0.0.0.

2. CSS CUSTOM HIGHLIGHT API IS AVAILABLE — proven by running: `typeof Highlight === 'function'`, `'highlights' in CSS` === true, `CSS.supports('selector(::highlight(x))')` === true, and a live `CSS.highlights.set('probe', new Highlight(range))` that visibly styled text via `::highlight(probe){background:#ff0}`. Applying 2,403 highlight ranges over a 340,000-char document took 8.8 ms and left `documentElement.scrollWidth` unchanged (ZERO reflow, so page numbers do not shift). Use it instead of wrapping in <mark>.

3. CASCADE LAYERS (the key architectural fact): I ran a probe with `@layer L1, L2;` plus an `!important` rule in L1, an unlayered `!important` rule, and an `!important` rule in L2, all on the same element. Computed value was L1's. So an !important declaration in the FIRST-declared layer beats an unlayered !important declaration from the book. Separately, a NORMAL declaration in a layer LOST to an unlayered normal declaration. Both confirmed on the real book fixture: book CSS had `body{overflow:hidden!important}` and our layered `overflow:visible!important` won (computed 'visible'), while book `font-family:"Times New Roman"` beat our layered theme font.

4. PAGINATION GEOMETRY IS EXACT: with `body{box-sizing:border-box; width:W; height:H; padding-inline:gap/2; column-width:W-gap; column-gap:gap; column-fill:auto}` and `html{width:W;height:H;overflow:hidden}`, the measured column step is exactly W. At W=900, gap=64: colW=836, columns landed at document x = 32, 932, 1832, 2732, 3632, 4532 — i.e. p*900 + 32 exactly. scrollWidth was 5368 = N*step - gap/2 exactly for N=6.

5. `documentElement.scrollLeft` IS settable and honoured even with `html{overflow:hidden!important}` — set 2700, read back 2700 (and `window.scrollX` tracked it). `overflow:hidden` suppresses user scrolling but keeps the scrollable overflow region; do NOT use `overflow:clip`.

6. LAST-PAGE CLAMP AND ITS FIX: max scrollLeft = scrollWidth - clientWidth = N*step - gap/2 - W, so the final page is short by gap/2 (requested 4500, got 4468; the text then touched the right edge). Adding `html::after{content:'';position:absolute;left:0;top:0;width:var(--rdr-tail);height:1px;visibility:hidden}` with `--rdr-tail: N*step` raised scrollWidth 5368 -> 5400 and made scrollLeft 4500 reachable; the last element then sat at left=32, identical to every other page. I also tested `html{padding-right}` — it does NOT extend the scroll range (still 4468).

7. PAGE COUNT FORMULA: `N = max(1, round((scrollWidth + gap/2) / step))` measured with the tail spacer zeroed. Verified against ceil() and round() variants (all agreed at 6) and against the true last element's page. Column boxes are always exactly colW wide even when the last column is nearly empty, so this is exact, not an estimate.

8. scrollLeft PAGING IS ~300x FASTER THAN TRANSFORM AT SCALE: on a 1,200-paragraph / 340k-char chapter (scrollWidth 159,268 px, 177 pages), 60 page turns took 1.1 ms via `de.scrollLeft` vs 343.4 ms via `body{transform:translateX()}`. Transform DOES land every page exactly (no clamp) and DOES contain position:fixed descendants, but the huge composited layer makes it unusable. Decision: scrollLeft + tail spacer.

9. `break-before:page` IS SILENTLY IGNORED inside a multicol container in this Chromium. Probe: an element's document x was 3632 with no break, still 3632 with `break-before:page!important`, and moved to 4532 with `break-before:column!important`. `getComputedStyle(el).breakBefore` correctly reports 'page', so a JS sweep can translate the author's intent to 'column'.

10. IMAGE ASPECT RATIO BUG AND FIX: `img{max-width:100%;max-height:pageH;height:auto}` DISTORTS images whose book CSS sets `width:100%` (a 900x1400 image became 836x604, ratio 1.384 instead of 0.643). Adding `width:auto!important` alongside `height:auto!important` restored it to 388x604, ratio 0.643 exactly matching 900/1400. Chromium's replaced-element sizing preserves ratio only when BOTH width and height are auto.

11. position:fixed FLOATS OVER EVERY PAGE: the badge stayed at viewport left=778 at page 0 and page 2. A JS sweep setting `position:absolute!important` on every element whose computed position is 'fixed' fixed it — after conversion it was at left=778 on page 0 and left=-1022 (off-screen) on page 2.

12. SCROLL EVENTS FIRE ON `document` AND `window`, NOT ON `documentElement` OR `body`. Probe counted: document=1, window=1, documentElement=0, body=0 after one programmatic `de.scrollLeft=` assignment. `scrollend` also fires on document (count 1) even for instant programmatic scrolls.

13. ResizeObserver ON documentElement/body IS USELESS HERE — because the safety layer pins `html{width:Wpx;height:Hpx}`, their observed boxes never change, so the callback never fires. Proven: across four `view.resize()` calls the page count stayed frozen at 57. Switching to `window.addEventListener('resize')` made page counts correctly become 27 / 50 / 51 / 57.

14. CAPTURING POSITION INSIDE A RESIZE HANDLER IS TOO LATE: book CSS using vh units (`.tallbox{height:150vh}`) reflows before the resize event is delivered, so a capture taken then describes the NEW layout while the page arithmetic still uses the old step. Traced: the captured char was 8855 instead of 9619. Fix is to keep a locator refreshed only on user navigation and reuse it after reflow.

15. POSITION MODEL ROUND-TRIPS EXACTLY. On a 7-page hostile fixture, goto(i) -> capture() -> goto(0) -> restore() returned i for all i in 0..6. A light->dark->sepia->light cycle stayed on page 3 with identical char 1719. A font sweep 15->19->24->30->19 px (page counts 7,7,10,12,7) kept the anchor on page 3 with startChar === endChar === 1719. Four real window resizes (1500x950, 560x1000, 1100x620, 900x700) kept the captured character on screen every time (assert: rect fully inside the viewport) and returned to page 14 / char 9619, identical to the original capture.

16. px->rem FONT-SIZE REWRITE PRESERVES THE BOOK'S HIERARCHY: rewriting book `font-size:16px` to `1rem` via CSSOM and driving `:root{font-size:var(--rdr-fs)}` gave body/h1/h2/pre = 18/36/27/15.3 px at 19px base and 26/52/39/22.1 px at 26px base; h1:body ratio stayed exactly 2.000. text-indent:2em scaled 36px -> 52px automatically.

17. DARK MODE WITHOUT COLOUR REMAP IS BROKEN: with only `html{background:#15171a!important; color:#d6d3cd!important}`, the book's `body{color:#222}` still won and measured contrast was 1.13:1. After the CSSOM hue-preserving lightness flip (20 declarations rewritten) contrast was 11.55:1, while `font-style:italic`, `font-variant-caps:small-caps` and `text-indent:36px` were all preserved. The rewrite is fully reversible — restoring the saved declarations returned body colour to rgb(34,34,34) and pre background to #eee.

18. CSSOM WALKER PITFALL (I hit this): in Chromium >= 112 a CSSStyleRule ALSO exposes `.cssRules` (CSS nesting), so `if (r.cssRules) { recurse; continue; }` skips EVERY style rule. My first rewrite pass silently matched 0 rules. Correct form: call the visitor when `r.style` exists, and only recurse when `r.cssRules && r.cssRules.length`.

19. DOUBLE-FLIP PITFALL (I hit this): writing the `background` shorthand expands it into `background-color`, so a read-modify-write loop over [background, background-color, ...] flips the same colour twice — `pre{background:#eee}` came out rgb(201,201,201) (light) in dark mode. Fix: snapshot all declarations of a rule BEFORE writing any of them, and skip `background-color` when the `background` shorthand is present. After the fix: rgb(43,43,43).

20. BINARY-SEARCH PITFALL (I hit this): whitespace-only text nodes between block elements generate NO client rects, so they return -1 from a page lookup; treating that as 'search right' discards half the array and the capture lands on arbitrary content (page 0 returned a char that was on page 4). Fix: index only text nodes matching /\S/ for the search, while keeping all nodes in the flat text for search-string continuity.

21. PAGE-ARITHMETIC PITFALL (I hit this): computing an element's page as `currentPageIndex + floor(rect.left/step)` drifts by up to half a page right after a relayout, because scrollLeft is then generally not a multiple of the NEW step (e.g. 12600 with step 1500). Use the exact offset: `floor((rect.left + Math.abs(scrollLeft) + 1) / step)`.

22. FIXED-LAYOUT DETECTION AND SCALING WORK: `<meta name="viewport" content="width=1200, height=1600">` parsed to {1200,1600}; scale = min(W/w, H/h) = 0.4375 at 900x700 with tx=188, ty=0. Verified at 900x700, 1600x600, 500x1200 and 1200x1500 — body always fitted (fits:true), was always centred (centred:true), and scrollWidth/scrollHeight never exceeded the viewport. Hit-testing (`elementFromPoint`) and `Range.getClientRects()` both return correct post-transform viewport coordinates.

23. NATIVE POPOVER API IS AVAILABLE: `'popover' in HTMLElement.prototype` and `showPopover()` both present; CSS anchor positioning (`position-anchor`, `position-area`) also supported. A `position:fixed` popover appended to the document caused ZERO reflow of the paginated book (scrollWidth unchanged).

24. epub:type SELECTOR: when the XHTML is parsed by the HTML parser the attribute name is literally `epub:type`, so `[epub\:type~="noteref"]` matches (returned 1). It will NOT match if the document is parsed as XML — then you need `getAttributeNS('http://www.idpf.org/2007/ops','type')`. Use a helper that tries `getAttributeNS` then `getAttribute('epub:type')` then `role="doc-noteref"`.

25. INPUT CAPTURE BEATS BOOK SCRIPTS: with a 'book' listener on document (bubble) and ours on document (capture) calling `stopImmediatePropagation()` + `preventDefault()`, our handler ran (1) and the book's never fired (0), with `defaultPrevented === true`.

26. SEARCH ACROSS INLINE BOUNDARIES WORKS with a flat text index: the phrase 'emphasis plus strong' spanning three text nodes was found, the reconstructed Range spanned nodes (crossesNodes:true) and resolved to the correct page. CJK search ('江月') works with plain indexOf — no word boundaries needed.

27. CJK TYPOGRAPHY SUPPORT in this Chromium: `text-spacing-trim` supported (and visibly compresses full-width punctuation in the rendered screenshot), `text-autospace` supported and measurably inserts ~0.25em between Han and Latin (string width 246.9 -> 251.9 px) — Chromium's DEFAULT is no-autospace, so you must opt in. `line-break:strict` and `text-wrap:pretty` supported. `hanging-punctuation` is NOT supported. `font-language-override` is NOT supported.

28. INSTALLED CJK FONTS (enumerated via QFontDatabase, 393 families): Noto Serif SC and Noto Sans SC are present in full weight ranges (Thin..Black) — these are the best choices. Also Microsoft YaHei / YaHei UI, DengXian, SimSun/NSimSun, SimHei, KaiTi, FangSong, YouYuan, STSong/STKaiti/STFangsong/STZhongsong, and Microsoft JhengHei for Traditional. 'Source Han Serif SC' is NOT installed (it measured identically to the generic fallback). Good Latin faces present: Georgia, Sitka (Text/Small/Heading/...), Cambria, Constantia, Palatino Linotype, Consolas, Cascadia Mono, Segoe UI Variable.

29. MIXED LATIN+CJK STACK IS SAFE: `Georgia, "Noto Serif SC", serif` rendered CJK punctuation (，。「」；：？！) at exactly the same width (90 px) as `"Noto Serif SC", serif` alone, so Georgia contributes no mismatched CJK glyphs and Han correctly falls through. Han characters measure exactly 1em wide in every CJK stack tested.

30. QtWebEngine DEFAULTS TO `prefers-color-scheme: dark` ON THIS MACHINE (matchMedia returned dark:true, light:false with no flags), which would silently activate a book's own dark-mode media rules. Setting the environment variable QTWEBENGINE_CHROMIUM_FLAGS=--blink-settings=preferredColorScheme=1 before importing QtWebEngine forces light (verified: dark:false, light:true). Value 2 produced neither true, so only 1 is usable.

31. TOUCH EVENTS ARE ABSENT on this desktop ('ontouchstart' in window === false, maxTouchPoints === 0), but Pointer Events are present. Build swipe/tap on `pointerdown/move/up`, not touch events.

32. RTL BOOKS SCROLL NEGATIVE: with `dir="rtl"` on <html>, scrollLeft ranges from 0 down to -9868 (scrollHi=0, scrollLo=-9868). Page i is at `scrollLeft = -i*step`. The `left:0` tail spacer does not extend the RTL range (max stayed 9868 vs 9900).

33. VERTICAL WRITING MODE BREAKS THE MODEL: with `writing-mode:vertical-rl` the multicol columns stack along the block axis and the document scrolls VERTICALLY (scrollHeight 5996, scrollWidth 900, N computed as 1). Detect it and fall back to scrolled mode.

34. EDGE CASES ARE SAFE: an empty <body> and a one-paragraph document both yield N=1, max scroll 0, and scrollHeight === clientHeight (no stray vertical scroll).

35. ALL THE HOSTILE CONTENT WAS TAMED by the safety layer, verified by a full `getClientRects()` audit over every element: a 1400px-wide table shrank to 730px, a non-wrapping <pre> wrapped to 90px tall, a 150vh div fragmented cleanly across two columns, a float worked normally, and only position:fixed/absolute elements escaped their column (count 2 of ~40 elements, both handled by the JS sweep).


## Pitfalls

1. Do NOT use `overflow:clip` on <html>; it kills programmatic scrollLeft. `overflow:hidden` is required (verified working).

2. Do NOT use `body{transform:translateX()}` for paging. It is exact and gives free position:fixed containment, but it was 343 ms vs 1.1 ms for 60 turns on a 159,268 px-wide layout. Chromium tries to composite a gigantic layer.

3. Do NOT use `scroll-behavior:smooth` / `scrollTo({behavior:'smooth'})` for page turns on long chapters — smoothly scrolling a 160,000 px multicol scroller repaints new columns every frame. In my offscreen harness the animation did not even run (completed in 1 frame), so its cost could not be measured reliably; treat it as unverified and risky. Use an instant jump (2.3 ms) plus an optional ~100 ms opacity crossfade on a position:fixed viewport-sized overlay (setup cost 8.3 ms, layer is viewport-sized so it is cheap), gated on `prefers-reduced-motion`.

4. Do NOT compute an element's page as `currentPage + floor(rect.left/step)`. Immediately after a relayout, scrollLeft is not a multiple of the new step and this drifts by up to half a page. Always use `floor((rect.left + Math.abs(scrollLeft) + 1) / step)`.

5. Do NOT capture the reading position inside the resize handler. Book CSS with vh units reflows before the event is delivered; I traced a capture returning char 8855 when the true anchor was 9619. Keep `lastLoc` refreshed only on user navigation and reuse it after any reflow. The same applies to setTheme: read the stored locator BEFORE mutating any CSS variable.

6. Do NOT re-capture after a restore. Re-anchoring to the top of the restored page loses a fraction of a page each cycle; after four resizes my anchor had drifted from char 9619 to 6283. Pin the locator to the last user navigation.

7. Do NOT walk CSSOM with `if (r.cssRules) { recurse; continue; }`. CSSStyleRule has `.cssRules` in Chromium >= 112 because of CSS nesting, so this skips every style rule (my first pass silently rewrote 0 declarations). Test `r.style` first; recurse only when `r.cssRules.length > 0`.

8. Do NOT read-modify-write colour longhands and the `background` shorthand in the same loop. Writing `background` expands into `background-color`, double-flipping the colour (#eee came out light grey in dark mode). Snapshot every declaration of a rule before writing any of it.

9. Do NOT include whitespace-only text nodes in the array you binary-search for page position. They produce no client rects and silently corrupt the search. Keep them in the flat text string (so search phrases still join across elements) but index only nodes matching /\S/.

10. Do NOT attach a ResizeObserver to documentElement or body — the safety layer pins their px size so they never change. Use `window.addEventListener('resize')` (plus visualViewport).

11. Do NOT listen for scroll/scrollend on documentElement or body. Root-scroller scroll events are dispatched at `document` and `window` only.

12. Do NOT force `height:auto` on images without also forcing `width:auto`. A book's `img{width:100%}` plus our `max-height` distorts the image (900x1400 became 836x604). Both must be auto for Chromium to preserve the aspect ratio.

13. Do NOT write `* { color: X !important }` for dark mode. It destroys link colour, heading colour, code colour and any semantic colouring. Rewrite the book's own stylesheets through CSSOM instead.

14. Do NOT rely on `page-break-before:always` / `break-before:page` doing anything — Chromium ignores page breaks inside multicol. Sweep computed styles and rewrite to `break-before:column`.

15. Do NOT put your theme rules in an `@layer` and then expect them to override the book. Layered NORMAL declarations always lose to unlayered book CSS. Only `!important` inside the FIRST-declared layer wins over everything (including the book's own !important).

16. Do NOT assume the viewport is light. QtWebEngine reported `prefers-color-scheme: dark` by default on this machine, which would fire the book's own dark media rules underneath your light theme. Force it with QTWEBENGINE_CHROMIUM_FLAGS=--blink-settings=preferredColorScheme=1.

17. Do NOT wrap the book's body children in a scroller div — book CSS commonly uses `body > x` selectors. Make <body> itself the multicol container and <html> the scroller.

18. `caretRangeFromPoint` at a page's top-left corner is unreliable for capture: if the point lands in the inter-column gap it falls back to the start of the document (my page-5 probe returned page-0 text). Use the deterministic text-node binary search instead; keep caretRangeFromPoint only for user-initiated hit-testing.

19. `[epub\:type~="noteref"]` only works if the XHTML was parsed by the HTML parser. Serve chapter documents as text/html from the scheme handler, or use getAttributeNS with the OPS namespace.

20. A page containing no text at all (a full-page image) has no text locator; capture then anchors to the next page and the position creeps forward on every reflow. Store a signed `pageDelta` (currentPage - locatedCharPage) and add it back on restore. This took my page-2 round-trip from wrong (got 3) to exact.

21. Vertical writing mode (`writing-mode:vertical-rl`, common in classical Chinese/Japanese books) makes multicol stack along the block axis and scroll vertically. Detect it and fall back to scrolled mode; the horizontal page model does not apply.

22. RTL documents scroll with NEGATIVE scrollLeft, and the `left:0` tail spacer does not extend the RTL scroll range. Page i is at `-i*step`; the last-page alignment fix needs a mirrored spacer.


## Recommendations

1. 1. PAGINATE WITH COLUMNS + scrollLeft, not continuous scroll and not transforms. Offer scrolled mode as a user preference only (and as the automatic fallback for vertical writing modes).

2. 2. USE THE THREE-LAYER CASCADE ARCHITECTURE. Inject `<style>@layer rdr-safety, rdr-theme;</style>` as the very first child of documentElement. Put box-model/pagination rules in `rdr-safety` with `!important` (they beat everything the book can do, verified). Put typography/colour defaults in `rdr-theme` WITHOUT !important (the book beats them, so italics/small-caps/indents/semantic colour survive). Put reader chrome (::highlight, popover, fade overlay) unlayered.

3. 3. ADD THE `html::after` TAIL SPACER so the last page aligns. On every relayout: set --rdr-tail to 0px, read scrollWidth, compute N = max(1, round((scrollWidth + gap/2)/step)), then set --rdr-tail to N*step. Two layouts, only on relayout — negligible.

4. 4. RUN A ONE-TIME DOM DEFENCE SWEEP on load: convert computed position:fixed to absolute!important; convert computed break-before/after:page to column!important; wrap any table still wider than the column in a `.__rdr-hscroll` div. Everything else is handled declaratively by the safety layer.

5. 5. FOR IMAGES use `img,svg,video,canvas,object,embed,iframe,picture{max-width:100%!important; max-height:<pageContentH>px!important; width:auto!important; height:auto!important; object-fit:contain!important}`. Both width AND height must be auto or aspect ratio is destroyed.

6. 6. SCALE FONT SIZE BY REWRITING THE BOOK'S CSS, not by overriding it. Walk document.styleSheets once at load, convert every `font-size` in px/pt to rem against a 16px base, then drive `:root{font-size: var(--rdr-fs)}`. This keeps the book's h1/h2/code size relationships intact (verified ratio 2.000 preserved) and makes em-based indents and margins scale too.

7. 7. FOR THE USER'S FONT FAMILY, remove `font-family`/`line-height`/`text-align` only from rules whose selector is html/body/:root, then set them from the theme layer. This lets the user's choice apply to body text while leaving `pre{font-family:monospace}`-style intentional choices alone. Keep the originals so the change is reversible (a 'use the book's fonts' option).

8. 8. FOR DARK/SEPIA, rewrite the book's colour declarations through CSSOM with a hue-preserving HSL lightness flip: newL = 0.92 - L*0.80, saturation clamped to <= 0.5. Save every original declaration so switching back to light is exact (verified reversible). Keep `background-image:url()` but override background-color to transparent. Also set `:root{color-scheme: dark}` so form controls and scrollbars follow.

9. 9. CJK DEFAULTS FOR THIS MACHINE: serif = `Georgia,"Sitka Text",Cambria,"Noto Serif SC",serif`; sans = `"Segoe UI Variable Text","Segoe UI","Noto Sans SC","Microsoft YaHei UI",sans-serif`; mono = `Consolas,"Cascadia Mono","Noto Sans SC",monospace`. Always Latin-face-first so Latin text uses the Latin design and Han falls through. Offer 楷体 (`Georgia,KaiTi,STKaiti,serif`) and 仿宋 as extras. Default line-height 1.75 for Latin, 1.85-1.95 for CJK-heavy text; letter-spacing 0 (do not add tracking to Han). Turn ON `text-autospace:normal` (Chromium defaults to no-autospace; it adds the correct ~0.25em Han/Latin gap) and `text-spacing-trim:space-first` plus `line-break:strict`. Do not set `hanging-punctuation` (unsupported).

10. 10. POSITION MODEL: store {char, pageDelta, path:{anchor,steps,offset}, text, frac, total} per spine item. `char` is the offset into the concatenation of all text nodes; `path` is child-index steps up to the nearest id'd ancestor (a CFI-lite); `text` is a 32-char snippet. Restore tries char -> path -> text search -> frac, then adds pageDelta. Refresh the stored locator ONLY on user navigation; reuse it verbatim across every relayout.

11. 11. WHOLE-BOOK PROGRESS: weight by character count, not by rendered pages or file size. Precompute per-spine-item character counts once in Python (cheap, layout-independent, stable across devices and font sizes); whole-book fraction = (sum of chars before this item + item.char) / totalChars. Use rendered pages only for the in-chapter 'page 12 of 42' readout, which legitimately changes with font size. My measurements show the char fraction stayed 0.2232 vs a page fraction wobbling 0.2895-0.2927 across window sizes — the char fraction is the stable one.

12. 12. SEARCH WITH THE CSS CUSTOM HIGHLIGHT API. Build a flat text index (concatenated text nodes + start offsets) so phrases spanning inline elements are found; map match char ranges back to Ranges via binary search; register two highlights, `rdr-find` (all matches) and `rdr-find-cur` (current). Zero DOM mutation means zero reflow and stable page numbers. Step to a match by computing its page from `getClientRects()[0]`.

13. 13. FOOTNOTES: on pointerup, if the target's nearest `a[href^="#"]` has epub:type ~= noteref (or role=doc-noteref) and the target id resolves in the same document, clone the target content into a `position:fixed` div with the native `popover="manual"` attribute, strip ids and back-links from the clone, position it under the anchor (flip above if it would overflow), and dismiss on the next outside pointerup or Escape. Verified to cause no reflow.

14. 14. FIXED LAYOUT: trust the OPF `rendition:layout` / `rendition:spread` metadata from the Python side, and in-page read `<meta name="viewport" content="width=W,height=H">` (fall back to `body > svg[viewBox]`). scale = min(vw/W, vh/H); tx = (vw - W*scale)/2; ty = (vh - H*scale)/2; apply `body{width:Wpx; height:Hpx; transform-origin:0 0; transform:translate(tx,ty) scale(s)}` with html overflow hidden. Verified to fit and centre at four very different aspect ratios. If crisper text matters, you can instead use QWebEngineView.setZoomFactor(s) and centre with margins — but the CSS transform is the one I verified end to end.

15. 15. INPUT: register every handler on `document` in the CAPTURE phase and call `preventDefault()` + `stopImmediatePropagation()`; this reliably beats the book's own listeners (verified). Keymap: Right/Down/PageDown/Space/Enter = next page, Left/Up/PageUp/Shift+Space = previous, Home/End = first/last page of chapter, and let the Qt chrome own Ctrl-modified keys (Ctrl+F search, Ctrl+T TOC, Ctrl+/- zoom, Ctrl+D theme) by returning early when ctrlKey/metaKey/altKey is set. Skip the keymap when the target is an input/textarea/contenteditable. Wheel: accumulate deltaY and turn once per ~40px with a ~110 ms cooldown. Pointer: swipe > 55px horizontal within 700 ms turns a page; a tap with no movement and no selection turns via 22%/56%/22% click zones; a tap in the centre zone toggles the Qt chrome. Always bail out if `getSelection()` is non-empty.

16. 16. PAGE-TURN ANIMATION: default to instant. If you want polish, use a `position:fixed` full-viewport overlay in the page background colour, opacity 0 -> 1 -> 0 over ~100 ms around the instant jump, disabled under `prefers-reduced-motion:reduce`. Do not animate the scroll itself.

17. 17. LAUNCH QtWebEngine WITH `os.environ['QTWEBENGINE_CHROMIUM_FLAGS'] = '--blink-settings=preferredColorScheme=1'` BEFORE importing QtWebEngine, so a book's own prefers-color-scheme rules never fight your theme. Also set `QWebEngineSettings.ShowScrollBars = False`.

18. 18. SERVE CHAPTER XHTML AS text/html from the custom scheme handler so the HTML parser is used (makes `[epub\:type]` selectors work) and so book stylesheets are same-origin and readable via `sheet.cssRules` — the whole theming strategy depends on that. Register the scheme with LocalAccessAllowed/CorsEnabled. Wrap every `sheet.cssRules` access in try/catch and fall back to re-injecting the CSS text as a style element you own.

19. 19. CALL SEQUENCE for the host: inject reader.js once per document at DocumentReady, then `RDR.init({layout, theme:{...}})`. On every user action call the matching API and read the returned page/pages. Listen for position reports (QWebChannel, or the document.title sentinel fallback in the reference implementation) and persist `loc` per spine item.

20. 20. KNOWN LIMITATIONS TO ACCEPT FOR v1: vertical-rl books fall back to scrolled mode; RTL books need the mirrored tail spacer for exact last-page alignment (paging itself works with `scrollLeft = -i*step`); a book's absolutely positioned decorations land on page 0 rather than following their text.


## Open questions

1. Smooth native scrolling could not be measured honestly: in the offscreen harness `scrollTo({behavior:'smooth'})` completed within a single frame, so I have no real jank number for it. My recommendation (instant jump + fixed-overlay crossfade) is based on the measured cost of transform/large-layer work rather than a direct measurement of native smooth scroll. Worth a 5-minute check in the real windowed app before finalising the animation choice.

2. Whether `sheet.cssRules` is readable for stylesheets served through your custom URL scheme handler. It worked for an inline <style> and for file:// in my harness, but the scheme's LocalAccessAllowed/CorsEnabled flags decide this. The whole theming strategy depends on it — verify early, and keep the fallback (fetch the CSS text yourself and re-inject it as a style element you own, which you can do anyway since you are the one serving it).

3. RTL last-page alignment: paging works with `scrollLeft = -i*step`, but the `left:0` tail spacer does not extend the RTL scroll range, so RTL books will still have the gap/2 shortfall on the final page. A mirrored spacer (`left: calc(-1 * var(--rdr-tail))`) should work but I did not verify it.

4. Vertical writing mode (vertical-rl) currently falls back to scrolled mode. A proper vertical paginated mode is possible (swap the pagination axis to scrollTop and mirror the arithmetic) but I did not build or test it. Relevant only if the user reads classical vertical-typeset Chinese EPUBs.

5. Dark-mode image treatment: I did not settle on a default. My suggestion is `filter: brightness(.86) contrast(1.05)` for images in dark themes (never `invert`), ideally skipped for images detected as line art / transparent PNGs, exposed as a user toggle.

6. Standalone images inside `figure`/`p` render flush-left when the book sets `display:block` without `margin:auto` (visible in my dark-mode screenshot). Consider a 'centre standalone images' preference implemented in the safety layer, but it does override author intent, so it should be opt-in.

7. The 4000-match cap in `find()` and the 8-node outward probe limits are arbitrary; tune against a real multi-megabyte chapter.

8. Whether the host should use QWebEngineView.setZoomFactor() instead of a CSS transform for fixed-layout pages. zoomFactor re-rasters text at the target scale so it is crisper, but it also changes innerWidth, so the two modes must not be mixed. I verified only the CSS-transform path.


## Verified code snippets


### PAGINATION CORE — the exact safety-layer CSS. Emit this as a single <style> whose text is regenerated on every relayout. W/H are measured px (never vw/vh). The step invariant is: content box width == W, column-width == W-gap, column-gap == gap, padding-inline == gap/2 => step == W exactly.

```css
/* injected as the FIRST child of <html>, once: */
@layer rdr-safety, rdr-theme;

/* regenerated on every relayout, W/H/gap/padT/padB are numbers in px: */
@layer rdr-safety{
  html{margin:0!important;padding:0!important;border:0!important;
       width:${W}px!important;height:${H}px!important;
       overflow:hidden!important;              /* NOT overflow:clip - that kills scrollLeft */
       position:static!important;
       column-count:auto!important;column-width:auto!important;
       background:var(--rdr-bg)!important;color:var(--rdr-fg)!important;
       transform:none!important;zoom:1!important;}

  /* the tail spacer: makes the LAST page reachable and exactly aligned.
     Without it maxScroll = N*step - gap/2 - W and the final column sits
     gap/2 px too far right. --rdr-tail is set to N*step after measuring. */
  html::after{content:''!important;display:block!important;position:absolute!important;
       top:0!important;left:0!important;width:var(--rdr-tail,0px)!important;height:1px!important;
       visibility:hidden!important;pointer-events:none!important;}

  body{box-sizing:border-box!important;margin:0!important;border:0!important;float:none!important;
       width:${W}px!important;height:${H}px!important;
       min-width:0!important;max-width:none!important;min-height:0!important;max-height:none!important;
       padding:${padT}px ${gap/2}px ${padB}px!important;
       column-width:${W - gap}px!important;column-count:auto!important;
       column-gap:${gap}px!important;column-fill:auto!important;column-rule:none!important;
       overflow:visible!important;position:static!important;display:block!important;
       background:transparent!important;transform:none!important;zoom:1!important;
       writing-mode:horizontal-tb!important;}

  /* replaced content: never taller than one page, never distorted.
     BOTH width:auto and height:auto are required for aspect preservation. */
  img,svg,video,canvas,object,embed,iframe,picture{
       max-width:100%!important;max-height:${H - padT - padB}px!important;
       width:auto!important;height:auto!important;object-fit:contain!important;
       box-sizing:border-box!important;position:static!important;}

  /* nothing may declare itself monolithic: a box taller than a column bleeds
     into the next page instead of fragmenting */
  :where(figure,table,pre,blockquote,div,section,article,aside,li,p,tr,td,th,
         h1,h2,h3,h4,h5,h6,dl,dd,ol,ul){break-inside:auto!important;}

  /* long code lines must wrap; horizontal overflow inside a column is far worse
     than a wrapped line because it paints over the next page */
  pre{white-space:pre-wrap!important;overflow-wrap:break-word!important;
      word-break:break-word!important;overflow:visible!important;max-width:100%!important;}
  table{max-width:100%!important;width:auto!important;table-layout:auto!important;}
  td,th{white-space:normal!important;word-break:break-word!important;}
  .__rdr-hscroll{max-width:100%!important;overflow-x:auto!important;overflow-y:hidden!important;}
}
```

### PAGINATION CORE — relayout, page count, and exact page jump. Verified: page count formula exact on every fixture; every page 0..N-1 reachable including the last.

```javascript
const de = document.documentElement;

function measure(){
  st.W = de.clientWidth || innerWidth;      // measured px, never vw/vh
  st.H = de.clientHeight || innerHeight;
  st.pad  = Math.round(st.gap / 2);
  st.colW = st.W - st.gap;
  st.step = st.W;                           // <- the invariant
  st.rtl  = getComputedStyle(de).direction === 'rtl';
}

function relayout(){
  measure();
  shGeom.textContent = geomCSS();           // the stylesheet above
  de.style.setProperty('--rdr-tail','0px'); // measure WITHOUT the spacer
  void de.scrollWidth;                      // force layout
  const raw = de.scrollWidth;
  // scrollWidth == N*step - gap/2 exactly (column boxes are always colW wide,
  // even when the last column is nearly empty), so this is exact, not a guess.
  st.pages = Math.max(1, Math.round((raw + st.pad) / st.step));
  de.style.setProperty('--rdr-tail', (st.pages * st.step) + 'px');
  void de.scrollWidth;
  tameWideBlocks();
  buildFlat();
  // always leave the scroller on an exact page boundary
  const p = Math.max(0, Math.min(st.pages-1, Math.round(Math.abs(de.scrollLeft)/st.step)));
  de.scrollLeft = sgn() * p * st.step; st.page = p;
}

const sgn       = () => (st.rtl ? -1 : 1);          // RTL scrolls negative
const curPage   = () => Math.round(Math.abs(de.scrollLeft) / st.step);

function gotoPage(i, animate){
  i = Math.max(0, Math.min(st.pages - 1, i|0));
  const target = sgn() * i * st.step;
  if (animate) fade(() => { de.scrollLeft = target; });
  else         de.scrollLeft = target;
  st.page = i;
  if (!restoring) { const l = capture(); if (l) st.lastLoc = l; }
  report();
  return i;
}
```

### PAGINATION CORE — mapping any DOM position to a page. This is the single primitive behind restore, search stepping and gotoAnchor. The exact-scrollLeft form is mandatory: the rounded form drifts up to half a page right after a relayout.

```javascript
function pageOfRect(rc){
  if (!rc) return -1;
  const d    = st.rtl ? (st.W - rc.right) : rc.left;  // distance from the start edge
  const docX = d + Math.abs(de.scrollLeft);           // exact document-space x
  return Math.floor((docX + 1) / st.step);            // +1 guards sub-pixel rounding
}

function firstRect(node, off, len){
  const L = node.nodeValue.length;
  const a = Math.max(0, Math.min(off, L - 1));
  const r = document.createRange();
  r.setStart(node, a); r.setEnd(node, Math.min(a + (len||1), L));
  const rc = r.getClientRects();
  return rc.length ? rc[0] : null;
}

function nodePages(node){                 // [firstPage, lastPage] or null
  const r = document.createRange(); r.selectNodeContents(node);
  const rc = r.getClientRects();
  if (!rc.length) return null;            // <- whitespace-only nodes land here
  return [pageOfRect(rc[0]), pageOfRect(rc[rc.length-1])];
}
```

### POSITION MODEL — the flat text index. Keep ALL text nodes in the string (so search phrases join across inline elements) but index only box-generating nodes for the binary search. Skipping this distinction corrupts capture silently.

```javascript
function buildFlat(){
  const tw = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, { acceptNode(n){
    if (!n.nodeValue) return NodeFilter.FILTER_REJECT;
    const pe = n.parentElement; if (!pe) return NodeFilter.FILTER_REJECT;
    const t = pe.tagName;
    if (t==='SCRIPT'||t==='STYLE'||t==='NOSCRIPT'||t==='TEMPLATE') return NodeFilter.FILTER_REJECT;
    return NodeFilter.FILTER_ACCEPT;
  }});
  const nodes = [], starts = [], solid = []; let s = '', n;
  while ((n = tw.nextNode())){
    starts.push(s.length);
    // Whitespace-only nodes between block elements generate NO boxes. They
    // return -1 from a page lookup, and treating that as "search right" throws
    // away half the array -> capture lands on arbitrary content.
    if (/\S/.test(n.nodeValue)) solid.push(nodes.length);
    nodes.push(n); s += n.nodeValue;
  }
  st.flat = { text: s, nodes, starts, solid };
}

function locate(i){                       // flat char index -> [textNode, offset]
  const { starts, nodes } = st.flat;
  let lo = 0, hi = starts.length - 1, a = 0;
  while (lo <= hi){ const m = (lo+hi)>>1; if (starts[m] <= i){ a = m; lo = m+1; } else hi = m-1; }
  return [nodes[a], i - starts[a]];
}
const pageOfChar = ci => { const [n,o] = locate(ci); return pageOfRect(firstRect(n,o)); };
```

### POSITION MODEL — capture and restore (the CFI-lite). Verified: exact round-trip on all 7 pages of a hostile fixture, stable across theme cycles, a 15/19/24/30/19 px font sweep, and four real window resizes.

```javascript
function capture(){
  if (!st.flat) return null;
  if (st.mode !== 'paginated')
    return { frac: de.scrollHeight > de.clientHeight
             ? de.scrollTop/(de.scrollHeight-de.clientHeight) : 0, mode: st.mode };

  const { nodes, starts, solid } = st.flat, cur = curPage();
  if (!solid.length) return null;

  // Outer search on the node's LAST page, so a node that starts on an earlier
  // page but spans onto `cur` is still selected. Pages are monotonic in doc order.
  let lo = 0, hi = solid.length - 1, ans = -1;
  while (lo <= hi){
    const m = (lo+hi)>>1, pp = nodePages(nodes[solid[m]]);
    if (pp && pp[1] >= cur){ ans = m; hi = m-1; } else lo = m+1;
  }
  const ni = solid[ans < 0 ? solid.length-1 : ans], nd = nodes[ni];

  // Inner binary search: first character of that node on page >= cur.
  let l2 = 0, h2 = nd.nodeValue.length - 1, best = 0;
  while (l2 <= h2){
    const m = (l2+h2)>>1, rc = firstRect(nd, m);
    if (!rc){ l2 = m+1; continue; }
    if (pageOfRect(rc) >= cur){ best = m; h2 = m-1; } else l2 = m+1;
  }
  const ci = starts[ni] + best;

  // A page may contain NO text at all (a full-page image). The located character
  // then lives on a LATER page; remember the gap or restore creeps forward on
  // every reflow. This is what took page-2 round-trip from wrong to exact.
  const locPage = pageOfRect(firstRect(nd, best));

  return { char: ci,
           pageDelta: (locPage >= 0 ? cur - locPage : 0),
           total: st.flat.text.length,
           frac:  st.flat.text.length ? ci/st.flat.text.length : 0,
           page: cur, pages: st.pages,
           path: pathOf(nd, best),                 // structural fallback
           text: st.flat.text.substr(ci, 32) };    // human-readable fallback
}

let restoring = false;
function restore(loc){
  if (!loc || !st.flat) return 0;
  restoring = true;                       // keeps gotoPage from re-anchoring
  try { return restoreInner(loc); } finally { restoring = false; }
}
function restoreInner(loc){
  if (st.mode !== 'paginated'){
    de.scrollTop = (loc.frac||0) * (de.scrollHeight - de.clientHeight); return 0;
  }
  let p = -1;
  if (typeof loc.char === 'number' && loc.char < st.flat.text.length) p = pageOfChar(loc.char);
  if (p < 0 && loc.path){ const n = resolvePath(loc.path); if (n) p = pageOfRect(firstRect(n, loc.path.offset||0)); }
  if (p < 0 && loc.text){ const i = st.flat.text.indexOf(loc.text.slice(0,16)); if (i >= 0) p = pageOfChar(i); }
  if (p < 0 && typeof loc.frac === 'number') p = Math.round(loc.frac * (st.pages - 1));
  if (p >= 0 && loc.pageDelta) p += loc.pageDelta;
  return gotoPage(Math.max(0, Math.min(st.pages-1, p)), false);
}

/* structural path: child-index steps up to the nearest id'd ancestor */
function pathOf(node, offset){
  const steps = []; let n = node, anchor = null;
  while (n && n !== document.body){
    const parent = n.parentNode; if (!parent) break;
    steps.unshift(Array.prototype.indexOf.call(parent.childNodes, n));
    if (parent.nodeType === 1 && parent.id){ anchor = parent.id; break; }
    n = parent;
  }
  return { anchor, steps, offset };
}
function resolvePath(loc){
  let n = loc.anchor ? document.getElementById(loc.anchor) : document.body;
  if (!n) return null;
  for (const i of loc.steps){ if (!n.childNodes[i]) return null; n = n.childNodes[i]; }
  return n && n.nodeType === 3 ? n : null;
}
```

### POSITION MODEL — reflow handling. The two rules that eliminated all drift: never capture inside a resize handler, and never re-capture after a restore.

```javascript
/* A ResizeObserver on <html>/<body> NEVER FIRES: the safety layer pins their px
   size. Listen to the window. */
let rzT = 0;
function onResize(){
  /* Do NOT capture here. By the time a resize event is delivered the document
     has already reflowed (book CSS using vh/vw, media queries), so a capture
     taken now describes the NEW layout while our page arithmetic still
     describes the old one. Use the locator stored at the last settled turn. */
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

/* st.lastLoc is written ONLY by gotoPage when !restoring, and by the scroll
   listener on a genuine user scroll. Re-capturing after a restore re-anchors to
   the top of the restored page and loses a fraction of a page every cycle. */
function report(){ host('pos', { page: st.page, pages: st.pages, loc: st.lastLoc }); }

/* setTheme must also read the STORED locator, before mutating any CSS var: */
setTheme(t){
  /* ... write --rdr-* custom properties, rebuild the theme sheet ... */
  const loc = st.lastLoc || capture();
  relayout(); if (loc) restore(loc);
  return { pages: st.pages, page: st.page };
}
```

### THEMING — the theme layer. Deliberately NOT !important and inside a layer, so unlayered book CSS wins and the book's italics, small-caps, poetry indents and semantic colours survive. Verified: book font-family/size/line-height/text-indent/font-style/font-variant all kept.

```css
@layer rdr-theme{
  :root{ font-size: var(--rdr-fs,19px); color-scheme: var(--rdr-scheme,light); }
  html{ font-family: var(--rdr-ff);
        line-height: var(--rdr-lh,1.75);
        color: var(--rdr-fg); background: var(--rdr-bg);
        text-align: var(--rdr-align,start);
        text-spacing-trim: space-first;   /* CJK punctuation compression - supported */
        text-autospace: normal;           /* ~0.25em Han/Latin gap; Chromium default is OFF */
        line-break: strict;               /* correct CJK line-breaking */
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
}

/* reader chrome, UNLAYERED so nothing in the book can touch it */
::highlight(rdr-find){ background:#ffd54a; color:#111; }
::highlight(rdr-find-cur){ background:#ff7a1a; color:#fff; }
#__rdr-fade{ position:fixed; inset:0; z-index:2147483646; pointer-events:none;
  background:var(--rdr-bg); opacity:0; transition:opacity .10s linear; }
@media (prefers-reduced-motion: reduce){ #__rdr-fade{ transition:none; } }
```

### THEMING — the CSSOM rewriter. Contains the two bugs I hit and fixed: CSSStyleRule now has .cssRules (CSS nesting), and writing the `background` shorthand expands into `background-color` causing a double flip.

```javascript
const BOOK_SHEETS = [];
function collectSheets(){
  BOOK_SHEETS.length = 0;
  for (const sh of document.styleSheets){
    const on = sh.ownerNode;
    if (on && on.getAttribute && on.getAttribute('data-rdr') !== null) continue;  // ours
    try { void sh.cssRules; BOOK_SHEETS.push(sh); } catch(e){ /* cross-origin */ }
  }
}

/* PITFALL: in Chromium >= 112 a CSSStyleRule ALSO has .cssRules (CSS nesting),
   so `if (r.cssRules) { recurse; continue; }` skips EVERY style rule. Test
   r.style first; recurse only when there are actually nested rules. */
function eachStyleRule(rules, fn){
  for (const r of rules){
    if (r.style) fn(r);
    if (r.cssRules && r.cssRules.length) eachStyleRule(r.cssRules, fn);  // @media/@supports/nesting
  }
}

/* 1. px/pt font-size -> rem, so the user's size scales the BOOK'S hierarchy.
      Verified: h1:body ratio stayed exactly 2.000 from 16px to 26px base. */
function normaliseFontSizes(){
  for (const sh of BOOK_SHEETS) eachStyleRule(sh.cssRules, r => {
    const v = r.style.getPropertyValue('font-size'); if (!v) return;
    const m = /^\s*([\d.]+)(px|pt)\s*$/.exec(v); if (!m) return;
    const px = m[2] === 'pt' ? parseFloat(m[1])*4/3 : parseFloat(m[1]);
    r.style.setProperty('font-size', (px/16).toFixed(4)+'rem',
                        r.style.getPropertyPriority('font-size'));
  });
}

/* 2. lift ONLY the book's root typography so the user's font choice applies to
      body text while `pre{font-family:monospace}` etc. survive. Reversible. */
const ROOT_SEL = /(^|,)\s*(html|body|:root)\s*(,|$)/i;
const famUndo = [];
function releaseRootTypography(on){
  if (on){ if (famUndo.length) return;
    for (const sh of BOOK_SHEETS) eachStyleRule(sh.cssRules, r => {
      if (!ROOT_SEL.test(r.selectorText||'')) return;
      for (const p of ['font-family','line-height','text-align','font-size']){
        const v = r.style.getPropertyValue(p);
        if (v){ famUndo.push([r,p,v,r.style.getPropertyPriority(p)]); r.style.removeProperty(p); }
      }});
  } else for (const [r,p,v,pr] of famUndo.splice(0).reverse()) r.style.setProperty(p,v,pr);
}

/* 3. dark mode: hue-preserving lightness flip. This is what you do INSTEAD of
      `* { color: X !important }`. Measured contrast 1.13 -> 11.55. */
function flipLightness(css, D){                    // D = {hi:.92, lo:.12}
  const p = parseRGB(resolveColor(css)); if (!p) return null;
  const [r,g,b,a] = p; if (a === 0) return null;
  const rr=r/255, gg=g/255, bb=b/255;
  const mx=Math.max(rr,gg,bb), mn=Math.min(rr,gg,bb), d=mx-mn;
  let h=0, s=0; const l=(mx+mn)/2;
  if (d){ s = l>.5 ? d/(2-mx-mn) : d/(mx+mn);
          h = mx===rr ? ((gg-bb)/d+(gg<bb?6:0)) : mx===gg ? ((bb-rr)/d+2) : ((rr-gg)/d+4); h*=60; }
  const nl = D.hi - l*(D.hi - D.lo);               // flip + compress
  const ns = Math.min(s, .5);                     // desaturate loud colours
  return `hsl(${h.toFixed(0)} ${(ns*100).toFixed(0)}% ${(nl*100).toFixed(0)}%`
       + (a<1 ? ` / ${a.toFixed(2)}` : '') + ')';
}

const COLOR_PROPS = ['color','background-color','border-color','border-top-color',
  'border-right-color','border-bottom-color','border-left-color','outline-color',
  'text-decoration-color','column-rule-color','caret-color','fill','stroke'];
const colorUndo = [];
function recolourBook(on){
  if (!on){ for (const [r,p,v,pr] of colorUndo.splice(0).reverse()) r.style.setProperty(p,v,pr); return; }
  if (colorUndo.length) return;
  const D = { hi:.92, lo:.12 };
  for (const sh of BOOK_SHEETS) eachStyleRule(sh.cssRules, r => {
    /* PITFALL: writing `background` expands into `background-color`, so a naive
       read-modify-write loop flips the same colour TWICE (#eee came out light
       grey in dark mode). Snapshot everything BEFORE writing anything. */
    const snap = [];
    const bg = r.style.getPropertyValue('background');
    if (bg) snap.push(['background', bg, r.style.getPropertyPriority('background')]);
    for (const p of COLOR_PROPS){
      if (bg && p === 'background-color') continue;       // covered by the shorthand
      const v = r.style.getPropertyValue(p);
      if (!v || v==='transparent' || v==='currentcolor' || /var\(/i.test(v)) continue;
      snap.push([p, v, r.style.getPropertyPriority(p)]);
    }
    for (const [p,v,pr] of snap){
      if (p === 'background' && /url\(/i.test(v)){        // keep the image, kill the tint
        colorUndo.push([r,'background',v,pr]);
        r.style.setProperty('background-color','transparent',pr); continue;
      }
      const nv = flipLightness(v, D);
      if (!nv){ if (p === 'background'){ colorUndo.push([r,p,v,pr]);
                  r.style.setProperty(p,'transparent',pr); } continue; }
      colorUndo.push([r,p,v,pr]);
      r.style.setProperty(p, nv, pr);
    }
  });
}
```

### DEFENCES — the one-time DOM sweep for what CSS alone cannot fix.

```javascript
function defend(){
  document.querySelectorAll('body *').forEach(el => {
    const cs = getComputedStyle(el);
    // position:fixed floats over EVERY page (verified: same viewport x on page 0
    // and page 2). As absolute it belongs to one page and scrolls away.
    if (cs.position === 'fixed') el.style.setProperty('position','absolute','important');
    // Chromium IGNORES break-before/after:page inside multicol (verified: x did
    // not move with `page`, moved a full column with `column`). Translate intent.
    if (cs.breakBefore === 'page') el.style.setProperty('break-before','column','important');
    if (cs.breakAfter  === 'page') el.style.setProperty('break-after', 'column','important');
  });
  document.documentElement.style.setProperty('overflow-anchor','none');
}

/* after every relayout: anything still wider than a column gets a scroller */
function tameWideBlocks(){
  document.querySelectorAll('table').forEach(t => {
    if (t.parentElement && t.parentElement.classList.contains('__rdr-hscroll')) return;
    if (t.getBoundingClientRect().width > st.colW + 1){
      const w = document.createElement('div'); w.className = '__rdr-hscroll';
      t.parentNode.insertBefore(w, t); w.appendChild(t);
    }
  });
}
```

### FIXED LAYOUT — detection and scale-to-fit. Verified at 900x700, 1600x600, 500x1200, 1200x1500: always fits, always centred, never overflows, hit-testing and Range rects correct through the transform.

```javascript
function detectFXL(){
  const m = document.querySelector('meta[name="viewport"]');
  if (m){
    const c = m.getAttribute('content') || '';
    const w = /(?:^|[;,\s])width\s*=\s*([\d.]+)/.exec(c);
    const h = /(?:^|[;,\s])height\s*=\s*([\d.]+)/.exec(c);
    if (w && h) return { w:+w[1], h:+h[1], src:'meta' };
  }
  const svg = document.querySelector('body > svg[viewBox]');   // SVG-wrapped scans
  if (svg){ const v = svg.getAttribute('viewBox').split(/[\s,]+/).map(Number);
            if (v.length === 4 && v[2] && v[3]) return { w:v[2], h:v[3], src:'svg' }; }
  return null;   // also trust OPF rendition:layout="pre-paginated" from Python
}

function applyFXL(){
  const f = st.fxl;
  const s  = Math.min(st.W / f.w, st.H / f.h);          // contain
  const tx = Math.round((st.W - f.w * s) / 2);
  const ty = Math.round((st.H - f.h * s) / 2);
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
```

### SEARCH — CSS Custom Highlight API with a flat index. Verified: finds phrases spanning inline element boundaries, works for CJK, steps to the correct page every time, zero reflow.

```javascript
function normalise(s){
  return s.toLowerCase().replace(/[‘’]/g,"'").replace(/[“”]/g,'"');
}

function find(q){
  clearFind(); if (!q) return 0;
  const hay = normalise(st.flat.text), needle = normalise(q);
  const out = []; let i = hay.indexOf(needle);
  while (i >= 0 && out.length < 4000){ out.push([i, i+needle.length]); i = hay.indexOf(needle, i+needle.length); }

  st.ranges = out.map(([a,b]) => {                    // char range -> live Range
    const r = document.createRange();
    const [n1,o1] = locate(a), [n2,o2] = locate(b-1);  // may be different nodes
    r.setStart(n1, o1); r.setEnd(n2, Math.min(o2+1, n2.nodeValue.length));
    return r;
  });
  if (st.ranges.length) CSS.highlights.set('rdr-find', new Highlight(...st.ranges));
  st.cur = -1;
  return st.ranges.length;
}

function findStep(delta){
  if (!st.ranges.length) return null;
  st.cur = (st.cur + delta + st.ranges.length) % st.ranges.length;
  const r = st.ranges[st.cur];
  CSS.highlights.set('rdr-find-cur', new Highlight(r.cloneRange()));   // separate registry
  const p = pageOfRect(r.getClientRects()[0]);
  if (p >= 0) gotoPage(p, false);
  return { index: st.cur, total: st.ranges.length, page: st.page };
}

function clearFind(){
  CSS.highlights.delete('rdr-find'); CSS.highlights.delete('rdr-find-cur');
  st.ranges = []; st.cur = -1;
}
```

### FOOTNOTES — namespace-safe noteref detection plus a native popover. Verified: popover shows, is positioned inside the viewport, and causes zero reflow of the paginated book.

```javascript
const NS_OPS = 'http://www.idpf.org/2007/ops';
const epubType = el => el.getAttributeNS(NS_OPS,'type')   // XML-parsed documents
                    || el.getAttribute('epub:type')       // HTML-parsed documents
                    || '';
const isNoteref = a => /\bnoteref\b/.test(epubType(a)) || a.getAttribute('role') === 'doc-noteref';

let pop = null;
function showNote(anchor){
  const href = anchor.getAttribute('href') || '';
  if (!href.startsWith('#')) return false;                 // cross-document: navigate instead
  const tgt = document.getElementById(decodeURIComponent(href.slice(1)));
  if (!tgt) return false;
  hideNote();

  pop = document.createElement('div');
  pop.id = '__rdr-pop'; pop.setAttribute('popover','manual');
  const clone = tgt.cloneNode(true);
  clone.removeAttribute('id');
  clone.querySelectorAll('[id]').forEach(e => e.removeAttribute('id'));   // no duplicate ids
  clone.querySelectorAll('a[href^="#"]').forEach(e => e.replaceWith(...e.childNodes)); // kill back-links
  pop.appendChild(clone);
  document.documentElement.appendChild(pop);   // position:fixed => out of the column flow
  try { pop.showPopover(); } catch(e){ pop.style.display = 'block'; }

  const a = anchor.getBoundingClientRect(), pr = pop.getBoundingClientRect();
  pop.style.left = Math.round(Math.max(8, Math.min(st.W - pr.width - 8, a.left - 12))) + 'px';
  pop.style.top  = Math.round(a.bottom + 10 + pr.height <= st.H
                              ? a.bottom + 10
                              : Math.max(8, a.top - 10 - pr.height)) + 'px';   // flip up
  return true;
}
function hideNote(){ if (!pop) return; try { pop.hidePopover(); } catch(e){} pop.remove(); pop = null; }
```

### INPUT — capture-phase handlers that reliably beat the book's own scripts (verified: our handler ran, the book's never fired, defaultPrevented true).

```javascript
const KEYMAP = {
  'ArrowRight':()=>turn(1),  'ArrowDown':()=>turn(1), 'PageDown':()=>turn(1),
  ' ':()=>turn(1),           'Enter':()=>turn(1),
  'ArrowLeft':()=>turn(-1),  'ArrowUp':()=>turn(-1),  'PageUp':()=>turn(-1),
  'Home':()=>gotoPage(0,true), 'End':()=>gotoPage(st.pages-1,true),
};

document.addEventListener('keydown', e => {
  if (e.ctrlKey || e.altKey || e.metaKey) return;      // leave Ctrl+F/T/D/+/- to the Qt chrome
  const t = e.target;
  if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
  const fn = KEYMAP[e.key]; if (!fn) return;
  e.preventDefault(); e.stopImmediatePropagation();     // <- beats the book's listeners
  if (e.key === ' ' && e.shiftKey) turn(-1); else fn();
}, true);

let acc = 0, wheelT = 0;
document.addEventListener('wheel', e => {
  if (st.mode !== 'paginated') return;
  e.preventDefault(); e.stopImmediatePropagation();
  acc += (Math.abs(e.deltaY) >= Math.abs(e.deltaX) ? e.deltaY : e.deltaX);
  const now = performance.now();
  if (Math.abs(acc) >= 40 && now - wheelT > 110){ turn(acc > 0 ? 1 : -1); acc = 0; wheelT = now; }
}, { capture:true, passive:false });

/* Pointer Events, not touch events: this desktop reports maxTouchPoints 0 and
   no ontouchstart, but pointer events cover mouse, pen and touch uniformly. */
let px=0, py=0, pid=null, moved=false, downT=0;
document.addEventListener('pointerdown', e => {
  pid=e.pointerId; px=e.clientX; py=e.clientY; moved=false; downT=performance.now();
}, true);
document.addEventListener('pointermove', e => {
  if (e.pointerId !== pid) return;
  if (Math.abs(e.clientX-px) > 8 || Math.abs(e.clientY-py) > 8) moved = true;
}, true);
document.addEventListener('pointerup', e => {
  if (e.pointerId !== pid) return; pid = null;
  const dx = e.clientX-px, dy = e.clientY-py, dt = performance.now()-downT;
  if (pop && !pop.contains(e.target)) { hideNote(); return; }
  if (moved && Math.abs(dx) > 55 && Math.abs(dx) > Math.abs(dy)*1.6 && dt < 700){   // swipe
    e.preventDefault(); e.stopImmediatePropagation(); turn(dx < 0 ? 1 : -1); return;
  }
  if (moved) return;
  const a = e.target && e.target.closest && e.target.closest('a[href]');
  if (a){
    if (isNoteref(a) && showNote(a)){ e.preventDefault(); e.stopImmediatePropagation(); return; }
    e.preventDefault(); e.stopImmediatePropagation();
    const href = a.getAttribute('href') || '';
    host('link', { href, internal: href.startsWith('#') });   // Qt resolves it
    return;
  }
  if (String(getSelection() || '').length) return;           // don't turn mid-selection
  const z = e.clientX / st.W;                                 // 22% / 56% / 22% zones
  if (z < .22) turn(-1); else if (z > .78) turn(1); else host('tapCentre');
}, true);

/* Page-turn transition: instant jump + a short crossfade on a viewport-sized
   fixed overlay. Do NOT animate the scroll itself on a 160,000px scroller. */
let fadeEl = null;
function fade(fn){
  if (matchMedia('(prefers-reduced-motion: reduce)').matches){ fn(); return; }
  if (!fadeEl){ fadeEl = document.createElement('div'); fadeEl.id = '__rdr-fade';
                document.documentElement.appendChild(fadeEl); }
  fadeEl.style.opacity = '1';
  requestAnimationFrame(() => { fn(); requestAnimationFrame(() => { fadeEl.style.opacity = '0'; }); });
}
```

### QT HOST SIDE — the startup flag that stops the book's own dark-mode media queries firing (QtWebEngine reported prefers-color-scheme:dark by default on this machine).

```python
import os
# MUST be set before any QtWebEngine import.
# Verified: no flag -> matchMedia('(prefers-color-scheme: dark)').matches == True
#           value 1  -> light:True,  dark:False   <- use this
#           value 2  -> neither matched; not usable
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = "--blink-settings=preferredColorScheme=1"

from PySide6.QtWebEngineCore import QWebEngineSettings
# ...
page.settings().setAttribute(QWebEngineSettings.WebAttribute.ShowScrollBars, False)

# Inject reader.js at DocumentReady (QWebEngineScript), then:
#   RDR.init({layout: 'reflowable'|'pre-paginated', theme: {...}})
# Public API surface of the reference implementation:
#   RDR.init(opts) -> {mode, pages, fxl, vertical}
#   RDR.setTheme({name,font,fontSize,lineHeight,gap,maxWidth,padT,padB,align,hyphens})
#   RDR.setMode('paginated'|'scrolled')
#   RDR.goto(i) / RDR.turn(+-1) / RDR.page() / RDR.pages()
#   RDR.gotoAnchor(id)            # for TOC links and internal hrefs
#   RDR.capture() / RDR.restore(loc)
#   RDR.find(q) -> count ; RDR.findStep(+-1) -> {index,total,page} ; RDR.clearFind()
#   RDR.progress() -> {page, pages, frac, chars}
#   RDR.relayout()
```

## Files written

- `C:\Users\mengz\epub-reader\_proto2\reader.js`

- `C:\Users\mengz\epub-reader\_proto2\book.html`

- `C:\Users\mengz\epub-reader\_proto2\fxl.html`

- `C:\Users\mengz\epub-reader\_proto2\harness.py`

- `C:\Users\mengz\epub-reader\_proto2\verify.py`

- `C:\Users\mengz\epub-reader\_proto2\idem.py`

- `C:\Users\mengz\epub-reader\_proto2\resize2.py`

- `C:\Users\mengz\epub-reader\_proto2\fxlcheck.py`
