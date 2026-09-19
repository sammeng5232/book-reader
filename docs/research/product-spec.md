# Research: product-spec

## Summary

I specced the product and the persistence layer, and verified every load-bearing claim by running code on this machine against the user's real Chinese EPUBs (found 4 under Desktop\文件, all EPUB 2 with NCX TOCs). App identity: **Verso**, Chinese-only UI copy, window title `书名 — Verso`. Feature set is split MUST/SHOULD/LATER with an explicit rejected-as-bloat list; the big cuts are cross-library search, dictionary, TTS, and non-EPUB formats. Chrome is: no menu bar, auto-hiding 44px toolbar, one left dock with 4 tabs (目录/书签/批注/搜索), one right live-apply settings panel, 26px status bar, plus a F1 cheat-sheet overlay to replace menu discoverability. Persistence is JSON under `%APPDATA%\Verso\` split three ways (settings / library / per-book) with `blake2b-128` whole-file content hashes as book IDs, atomic `os.replace` writes, `.bak` rollback, corrupt-file quarantine, and integer `schema` migrations — I implemented the whole store and ran it through six failure modes, all of which recovered. The single most important design decision is the position/annotation **locator**: a flattened-document character offset (`gpos`) plus a text snippet plus an element path. I built three versions in a real QtWebEngine page and only this one survives style injection, highlight `<mark>` wrapping, and edits to the book itself. Two traps will silently ruin the app if the implementer does the obvious thing: lxml mis-decodes these books' XHTML as latin-1 (mojibake, 0 CJK chars recovered), and SQLite FTS5's default tokenizer cannot match a 2-character Chinese query at all.


## Verified facts

1. Environment confirmed by running it: Python 3.14.6 at C:\Users\mengz\AppData\Local\Programs\Python\Python314\python.exe, PySide6 6.11.1 / Qt 6.11.1, sqlite3 3.50.4, APPDATA=C:\Users\mengz\AppData\Roaming, LOCALAPPDATA=C:\Users\mengz\AppData\Local.

2. QStandardPaths.AppConfigLocation returns C:/Users/mengz/AppData/Local/<Org>/<App> on Windows — i.e. LOCAL, not Roaming. Only AppDataLocation gives Roaming. Proven by running QStandardPaths.writableLocation for all five location enums. Compute the config root from os.environ['APPDATA'] explicitly instead.

3. The user's real books are EPUB 2.0 with NCX, not EPUB 3 nav. Read ops/content.opf out of 从此岸到彼岸: `<package ... version="2.0">`, cover declared as EPUB2-style `<meta name="cover" content="my_cover_image"/>`, TOC via .ncx. Supporting only EPUB 3 nav.xhtml would fail on every book this user owns.

4. CRITICAL ENCODING TRAP, reproduced: ops/chapter3.xhtml in the user's book begins `<html xmlns=...>` with NO XML declaration and NO <meta charset>. lxml.html.fromstring(raw_bytes) then decodes it as latin-1 and yields 51,738 chars with 0 CJK chars (pure mojibake: 'é\x87\x91è\x9e\x8d'). Decoding raw.decode('utf-8') first and parsing the STRING yields 23,125 chars with 12,615 CJK chars, correct. Same fix works via lxml.html.HTMLParser(encoding='utf-8').

5. CRITICAL LOCATOR FINDING, proven in a live QWebEngineView: a DOM-child-index path breaks when anything is inserted into <body> (path [5,0] silently retargeted after a <style> insert), and a text-node-local snippet fallback ALSO fails whenever the snippet spans an inline element such as <em> or <b> — both my first two locator designs returned 'FAILED'. The flattened-document-text design (walk text nodes with SCRIPT/STYLE/NOSCRIPT rejected, concatenate, store global char offset) resolved 'exact@gpos' after (a) injecting <style> into <head>, (b) injecting <style> as body's FIRST child, and (c) wrapping a highlight <mark> across a text node; after simulating an edit to the book itself it recovered via snippet search with drift +12.

6. CRITICAL CJK SEARCH FINDING: sqlite FTS5 with the default unicode61 tokenizer returns ZERO rows for MATCH '"汇率"' and zero for '"改革开放"' against Chinese text that contains both, while matching English 'reform' fine. tokenize="trigram" matches 4-char CJK queries but still returns 0 for 2-char queries via MATCH; `WHERE body LIKE '%汇率%'` on a trigram table does return the row. porter unicode61 fails the same way as unicode61.

7. In-book full-text search needs NO index. Parsing every spine document of the 13MB, 24-doc Chinese book with lxml takes 13ms total and re.findall of '汇率' across the whole book takes 0.27ms (2,371 hits). The 30-doc finance textbook: 37ms parse, 0.16ms search. Decode+parse once on open, keep the plain text in memory, search with str.find/re.

8. Whole-file blake2b-128 hashing is cheap enough to be the book identity: 32.8ms for 6.36MB, 73.1ms for 13.08MB (measured on the user's actual files). A first-256KB+last-256KB partial hash takes ~3ms but is not needed for single-book opens.

9. Atomic persistence verified end to end by implementing and running the store: mkstemp in the target dir → write → flush → os.fsync → os.replace overwrites an existing file on NTFS with no temp-file leftovers and preserves UTF-8 Chinese content. Six failure modes all handled: clean roundtrip, .bak creation, truncated primary → recovered from backup, primary AND backup trashed → quarantined to .json.corrupt-<epoch> and reset, schema 0→1 migration applied, schema 99 detected and flagged READ_ONLY_FUTURE_SCHEMA.

10. JSON scales fine at realistic sizes, measured: an 800-book library.json is 472KB and writes in 40.0ms / reads in 24.7ms; a per-book file with 3,000 highlights is 1,599KB and writes in 59.8ms. sqlite is not needed for the state of record.

11. Single-instance file-association handoff works: QLocalServer.listen('verso-single-instance') binds \\.\pipe\verso-single-instance, a second process connects via QLocalSocket and forwards 'OPEN <path>' to the primary, which received it correctly. Verified by having the script spawn a copy of itself.

12. EPUB error-state classification verified against eight synthesized files: valid → OK; Adobe ADEPT (encryption.xml + rights.xml) → DRM(ADEPT); Readium LCP (encryption.xml + license.lcpl) → DRM(LCP); IDPF font obfuscation (Algorithm=http://www.idpf.org/2008/embedding) → OK, correctly NOT treated as DRM; missing container.xml → NO_CONTAINER; non-zip bytes → BadZipFile; truncated zip → BadZipFile; missing mimetype entry → OK. The font-obfuscation case is the one naive implementations get wrong and it locks users out of legitimate books.

13. Fonts actually installed on this machine (QFontDatabase.families() under the real windows platform plugin, 393 families): CJK = SimSun, NSimSun, SimHei, KaiTi, FangSong, STKaiti, STFangsong, Microsoft YaHei (+UI/Light), DengXian (+Light), Microsoft JhengHei. Latin = Georgia, Cambria, Constantia, Palatino Linotype, Segoe UI (+Variable), Times New Roman. There is NO Source Han Serif/Sans and NO Noto CJK — do not reference them. Note: under QT_QPA_PLATFORM=offscreen QFontDatabase returns 0 families, so never probe fonts headless.

14. Display is 1920x1200, devicePixelRatio 1.0, logicalDpi 96, physicalDpi 161 — the user runs a 161-PPI panel at 100% Windows scaling, so everything renders physically small. This is why the default body size is 21px, not the usual 16-18px.

15. Cover extraction + thumbnailing is fast and small: Pillow LANCZOS to 360px wide then JPEG q86 gave 18KB (54ms) and 27KB (10ms) for the two real books; both covers are ~570x800. At ~25KB each an 800-book cover cache is ~20MB.

16. Reading-time estimation is credible with units = CJK chars + Latin words: the two real books measure 145,962 and 162,499 units, giving 8.1h and 9.0h at 300 units/min — believable for these titles. With the mojibake bug present the first book measured 0 CJK chars and estimated 0.5h, which is how you would notice the encoding bug from the UI alone.


## Pitfalls

1. Do NOT parse EPUB XHTML from raw bytes. lxml will guess latin-1 for these books (no XML decl, no meta charset) and produce mojibake with zero CJK characters. Always: detect BOM (UTF-8 BOM → strip; UTF-16 BOM → decode utf-16), else decode('utf-8'), and parse the resulting STRING. EPUB 3 mandates UTF-8/UTF-16, so never fall back to a charset sniffer.

2. The SAME trap applies to the custom URL scheme handler feeding Chromium. The QWebEngineUrlRequestJob reply MUST set Content-Type: application/xhtml+xml; charset=utf-8 (and text/css; charset=utf-8). If you serve without an explicit charset, Chromium repeats the latin-1 guess and the book renders as mojibake even though your Python side parsed it correctly. Hand this to the renderer agent.

3. Never anchor positions or highlights with DOM child indices alone — verified to silently retarget when anything is inserted into <body>. And never use a per-text-node snippet as the fallback: a snippet that crosses an <em>/<b> boundary exists in no single text node, so the fallback also fails. Use the flattened-text gpos design in the snippet below.

4. The flattened-text walker MUST reject SCRIPT/STYLE/NOSCRIPT text nodes. That rejection is precisely what makes locators survive a <style> injected into <body>; without it, injected CSS text shifts every offset in the document.

5. Inject theming CSS into <head>, never into <body>, and never wrap existing body children. Highlight <mark> wrapping is safe because it does not change textContent (verified).

6. SQLite FTS5 cannot do Chinese with its default tokenizer — MATCH '"汇率"' returns nothing. If cross-library search is ever built (LATER), use tokenize="trigram" and query with LIKE '%q%' for queries under 3 characters. For in-book search (v1 MUST) do not use sqlite at all; plain str.find over pre-decoded spine text is exact and takes 0.2ms.

7. QStandardPaths.AppConfigLocation is Roaming on Linux/macOS but LOCAL on Windows. Using it would put the user's library and annotations in AppData\Local. Build the path from os.environ['APPDATA'] instead.

8. Font obfuscation (Algorithm 'http://www.idpf.org/2008/embedding' or Adobe's RC4 variant) puts META-INF/encryption.xml in a perfectly readable book. Treating 'encryption.xml exists' as DRM will refuse to open legitimate files. Only treat it as DRM when an EncryptionMethod Algorithm outside the obfuscation set is present.

9. Do not probe QFontDatabase under QT_QPA_PLATFORM=offscreen — it reports 0 families and you will conclude the machine has no fonts. Use the real windows platform plugin.

10. Never populate the font dropdowns from a hardcoded wishlist. Source Han Serif and Noto CJK are NOT on this machine; a menu offering them produces silent fallback to a font the user did not choose. Intersect your preferred list with QFontDatabase.families() at startup.

11. The .bak holds the PREVIOUS version, so a crash between write and the next write loses exactly one generation. That is why annotations and bookmarks must be flushed immediately (never debounced) while position may be debounced 2s — losing 2s of scroll position is invisible, losing a highlight is not.

12. Never drop a highlight whose anchor cannot be resolved. Mark it anchor_state:"lost", keep it in the file forever, and show it greyed in the 批注 panel with its saved text. Silently deleting a user's note is the single worst thing a reader can do.

13. The app must never copy, move, modify or delete the user's .epub files. Reference them in place. There must be no 'delete file' item anywhere in the UI — only 从书架移除.

14. Do not present a page number as if it were authoritative. Pagination is a function of font size and window width; show percentage and time-left, and label any page count as 本屏 rather than 页码.

15. Do not bind single-letter shortcuts (j/k/n/N//) unconditionally — gate them on the book view having focus and no QLineEdit/text input being focused, or typing in the search box will page the book.

16. Don't hash on every library scan. Cache the hash keyed by (path, size, mtime_ns) in library.json; only rehash when size or mtime_ns changes. 800 books x 5MB unconditionally is ~12s of I/O per scan.


## Recommendations

1. === 1. WHAT THE GOOD READERS ACTUALLY GET RIGHT (and what to steal) ===
Five things make reading pleasant, and every one of them is cheap:
(1) TYPOGRAPHY YOU CONTROL, APPLIED OVER THE BOOK'S CSS — Apple Books and Foliate both normalize the publisher's stylesheet and let you set family/size/leading/measure. Calibre can do it but buries it three panels deep. Steal: a live-apply side panel, no OK button.
(2) POSITION RESTORE TO THE PARAGRAPH — Kindle and Apple Books put you back on the sentence. Thorium and most web readers put you back at the chapter. This is the difference between 'my book' and 'a file I opened'.
(3) INSTANT PAGE TURNS — KOReader and Foliate repaint immediately. Kindle for PC animates and feels sluggish. Ship with NO page-flip animation and no option for one.
(4) KEYBOARD-FIRST — calibre's viewer is the reference (Space/arrows/Ctrl+F/Ctrl+T, ~50 bindings, all remappable). You should never need the mouse to read.
(5) CHROME THAT DISAPPEARS — Apple Books hides everything while reading; Thorium keeps a permanent heavy toolbar and feels like software. Auto-hide after 3s.
Six things make readers feel cheap:
(a) Mojibake on CJK books (I reproduced exactly this bug against the user's own library — see pitfalls).
(b) Restoring only to chapter start.
(c) Fake authoritative page numbers that change with font size.
(d) A 'managed library' that copies your files into an opaque folder — calibre's most-complained-about behaviour. Reference files in place.
(e) Half-done CJK: Latin leading on Chinese text, mixed Song/Hei within one line from bad fallback, no paragraph indent option.
(f) Modal dialogs and a separate settings window that covers the text you are adjusting.
Readest and Koodo are the cautionary tale in the other direction: both are competent but spend their surface area on cloud sync, accounts, and eight file formats. This app has one user, one format, one machine. That is an advantage — spend it on the reading surface.

2. === 2. V1 FEATURE SET ===
MUST — the app is not a reader without these:
 M1. Open .epub via: drag-drop onto the window, Ctrl+O dialog, command-line argv[1], Windows file association, double-click in the library. Second launch forwards to the running instance over QLocalServer (verified working).
 M2. EPUB 2 (NCX) AND EPUB 3 (nav.xhtml) spine + TOC. The user's four real books are all EPUB 2 — NCX is not optional.
 M3. Correct UTF-8 for CJK end to end (Python parse side AND the URL-scheme-handler Content-Type).
 M4. Paginated reading via CSS multi-column, instant turns, no animation.
 M5. TOC panel with live current-chapter tracking and jump.
 M6. Per-book position memory, accurate to the sentence, using the verified gpos locator.
 M7. Typography: 中文字体 / 西文字体 chosen separately, size, line-height, paragraph spacing, first-line indent, alignment, page margin, max measure.
 M8. Themes 日 / 纸 / 夜 / 跟随系统, applied to both the Qt chrome and the book.
 M9. Full keyboard control with a conflict-free map and an F1 cheat sheet.
 M10. In-book full-text search with a result list and jump (needs no index — 0.2ms verified).
 M11. Library/start screen: covers, 继续阅读 row, progress bars, sort, filter. References files in place.
 M12. Bookmarks.
 M13. Window geometry/maximized/fullscreen/dock-width memory.
 M14. Fullscreen (F11).
 M15. Text selection + Ctrl+C.
 M16. Progress %, 本章剩余 and 全书剩余 in minutes, from measured units (CJK chars + Latin words) and a learned reading speed.
 M17. Specific, actionable error states for corrupt / DRM / missing / structurally-broken files.
 M18. Atomic, versioned, human-readable persistence.
SHOULD — ship now because the MUST work already paid for them:
 S1. Highlights in 4 colors + notes. The locator primitive is already built for M6; highlights are the same object with a start and an end. This is the single highest value-per-line feature in the app.
 S2. Scroll mode toggle (Ctrl+M) — one CSS switch.
 S3. Export notes to Markdown — ~20 lines once S1 exists.
 S4. Moved-file self-repair via content hash — the hash already exists for M18.
 S5. Per-book typography override (an econ textbook and a novel want different settings).
 S6. 专注模式 (Ctrl+Shift+F): fullscreen + chrome never appears + wider margin.
 S7. Session timer feeding the speed estimate in M16.
 S8. Ctrl+G go-to-percent.
 S9. Click an image to zoom it full-window. Non-negotiable in practice for this user: the finance books here contain 208 embedded figures.
 S10. Ctrl+Shift+C = copy with source citation (《书名》第 N 章).
LATER — deliberately not v1:
 L1. Cross-library full-text search (needs an FTS5 trigram index; feasible, verified, but it is a subsystem with its own invalidation problem).
 L2. Dictionary lookup — no dictionary data is available offline and no packages may be installed. Reserve Ctrl+L and ship nothing.
 L3. Read-aloud/TTS. L4. PDF/MOBI/AZW3. L5. Fixed-layout EPUB (comics) — a different renderer. L6. Two-page spread. L7. Vertical writing mode 竖排. L8. Custom font import. L9. Collections/tags/ratings/metadata editing. L10. Screen-reader/ARIA work.
REJECTED AS BLOAT — do not build, ever:
 Cloud sync or accounts. OPDS catalogs or a built-in store. Format conversion (that is calibre's job). Social sharing. A CSS-injection/theme-editor UI (three good presets beat a color picker). Page-flip animation. Reading streaks, badges, statistics dashboards. A menu bar.

3. === 3. APPLICATION CHROME ===
WINDOW: QMainWindow, default 1280x900, min 720x520. A QStackedWidget with two pages: 0 = 书架 (library), 1 = 阅读 (reader). No menu bar anywhere.
READER PAGE LAYOUT:
  toolbar   : 44px, full width, auto-hides after behavior.auto_hide_chrome_ms (default 3000ms) of no mouse movement; reappears on mouse within 60px of the top edge or on any chrome shortcut.
  left dock : QDockWidget, 280px default, draggable 200-480px, persisted. Non-floatable, non-closable-by-X (toggled by shortcut only).
  center    : QWebEngineView, fills remaining space.
  status bar: 26px, auto-hides with the toolbar.
TOOLBAR, left to right: [≡ 目录 Ctrl+T] [◀ Alt+←] [▶ Alt+→] — stretch — center label `书名 · 当前章节` (elided middle, not clickable) — stretch — [Aa 排版 Ctrl+,] [🔍 搜索 Ctrl+F] [★ 书签 Ctrl+D] [⛶ 全屏 F11] [… 更多]. Icon-only buttons, 24px icons, tooltip = label + shortcut.
The '…' overflow menu replaces the menu bar and contains, in order: 打开书籍… (Ctrl+O), 回到书架 (Ctrl+Shift+L), ─, 书籍信息, 导出批注… , ─, 快捷键 (F1), 设置, 关于 Verso, ─, 退出 (Ctrl+Q). Every item shows its shortcut. Discoverability is carried by F1, not by a menu bar.
STATUS BAR: left = `第三章 汇率的锚`; center = `34%`; right = `本章剩余 12 分钟`. Right-click the right cell to cycle it between 本章剩余 / 全书剩余 / 已读时长. No progress slider in the status bar — Ctrl+G is the jump affordance.
LEFT DOCK: a 4-segment control at the top — 目录 | 书签 | 批注 | 搜索 — switching a QStackedWidget. One dock, four panes, never four docks.
  目录: QTreeView from NCX/nav, expanded to depth 1. Current chapter row is bold with a 3px left accent bar and auto-scrolls into view — EXCEPT if the user manually scrolled the tree within the last 5s. Single click jumps and keeps focus in the tree (so ↑↓+Enter works); double-click jumps and returns focus to the book. Esc closes the dock and focuses the book. If the book has no TOC, show the spine flat, labelled from each document's <title> or first h1-h3, under a one-line note 「本书未提供目录，以下为章节文件」.
  书签: flat list, newest first, each row = chapter title, 3-line text preview, percentage, date. Enter/double-click jumps. Delete removes (no confirmation; Ctrl+Z is not supported, so show a 3s status-bar undo link instead).
  批注: grouped by chapter, colored left bar per highlight color, quoted text + note underneath. Filter chips for the four colors + 仅显示有笔记. Rows with anchor_state 'lost' render greyed with a 「无法定位」 badge and are NOT clickable to jump, but ARE exportable. Button at the bottom: [导出为 Markdown…].
  搜索: a QLineEdit at the top plus result rows (chapter · …context <mark>query</mark> context…). Enter and n/F3 walk results; the current result is scrolled to and flashed. Shows 「共 2371 处」. Cap the rendered list at 500 rows with 「仅显示前 500 处」.
SETTINGS PANEL: a right-side QFrame, 320px, non-modal, slides in, live-apply, NO OK/Cancel/Apply buttons. Sections top to bottom:
  主题    : segmented [日][纸][夜][跟随系统]
  字体    : 中文字体 combo · 西文字体 combo · [使用书籍自带字体] checkbox · 字号 slider 14-32px with a numeric spinbox · 字重 [常规][细]
  排版    : 行距 1.2-2.4 · 段间距 0-1.5em · 段首缩进 [无][两字符] · 对齐 [左对齐][两端对齐] · 页边距 24-160px · 单行字数上限 28-60
  翻页    : [分页][滚动]
  footer  : [ ] 仅用于本书   ·   恢复默认
When 仅用于本书 is checked, every change writes to books/<id>.json `overrides` instead of settings.json `reader`. Unchecking it deletes the override block and the book snaps back to global. Font combos are built by intersecting a preferred list with QFontDatabase.families() at startup — never hardcoded.
DEFAULTS for this machine (161 PPI at 100% scaling, so bigger than usual): 中文 Microsoft YaHei, 西文 Georgia, 21px, 行距 1.9, 段间距 0.6em, 段首缩进 无, 左对齐, 页边距 64px, 单行 40 字, 分页, 跟随系统. Offer SimSun for readers who want 宋体, but do not default to it — at 161 PPI/dpr 1.0 it renders thin. There is no Source Han Serif on this machine; do not list one.
LIBRARY / START SCREEN (shown at launch when behavior.restore_last_book_on_launch is false or no last book; always reachable via Ctrl+Shift+L):
  Top bar: [+ 打开书籍…] [+ 添加文件夹…] — stretch — [search field: 搜索书名、作者] [sort combo: 最近阅读 / 添加时间 / 书名 / 作者 / 阅读进度] [view toggle 封面|列表].
  First section 「继续阅读」: up to 6 books with 0 < progress < 1, most recent first. Omitted entirely when empty — no empty placeholder row.
  Second section 「全部书籍」: flow grid, cover boxes 160x240 logical, title (2 lines max, elided) and author (1 line, secondary color) underneath, and a 3px progress bar flush to the cover's bottom edge shown only when progress > 0.
  Missing covers get a GENERATED card, not a generic icon: flat background whose hue = hash(book_id) mod 360 at fixed low saturation, the title set in the CJK font and vertically centered, author small at the bottom.
  Books with missing:true render at 55% opacity with a small 「文件缺失」 badge.
  Right-click a cover: 打开 / 在文件夹中显示 / 复制文件路径 / 书籍信息 / ─ / 从书架移除. There is NO 删除文件 item, ever.
  Drag-and-drop of .epub files or folders anywhere on this screen adds and (for a single file) opens.
EMPTY LIBRARY: centered, three lines, nothing else — 「书架是空的」 / 「把 EPUB 文件拖到这里，或者」 / [打开书籍…] [添加文件夹…]. No illustration, no tips, no carousel.
ERROR STATES — all rendered as an in-pane centered card (title 20px semibold, body 14px secondary, buttons below), never a modal QMessageBox, except 'relocate' which legitimately opens a file dialog:
  (a) CORRUPT / NOT A ZIP → 标题「无法打开这本书」 / 正文「文件可能已损坏，或者不是 EPUB 格式。」 + the full path on its own selectable line + a collapsed 「技术细节」 disclosure showing the exception repr. Buttons [在文件夹中显示] [从书架移除] [关闭].
  (b) DRM → 标题「这本书有 DRM 保护」 / 正文「检测到 {Adobe ADEPT|Readium LCP|未知} 加密。Verso 不解密受保护的文件，请用购买它的官方应用打开。」 Buttons [在文件夹中显示] [关闭]. Tone: a stated limitation, not an apology, and never phrased as the user's mistake. Books whose encryption.xml only lists font obfuscation must NOT reach this screen.
  (c) MISSING FILE → do not show an error first. On ENOENT, silently search (i) the last known folder and (ii) every folder already present in library.json for a file whose size AND content hash match; on a hit, repair `path`, push the old value onto `path_history`, open the book, and show a 3s status note 「文件已移动，位置已更新」. Only on failure show 标题「找不到这个文件」 / 正文「《书名》原来在：」 + old path. Buttons [重新定位…] [从书架移除] [关闭]. If the user picks a file whose hash differs, confirm inline: 「这似乎是另一个文件，仍要关联吗？」 [仍要关联] [取消].
  (d) STRUCTURALLY BROKEN (no container.xml / unreadable OPF) → 标题「这个 EPUB 结构不完整」 / 正文「缺少 META-INF/container.xml，无法确定书籍内容。」 Buttons as (a).
  (e) ONE SPINE DOCUMENT FAILS → never fail the whole book. Render an in-flow placeholder block 「本节内容无法显示」 and keep TOC, search and navigation fully working.
  (f) FIRST-RUN ASSOCIATION: do not prompt to become the default .epub handler at launch. Put it in 设置 as a single button [设为默认 EPUB 阅读器].

4. === 3b. KEYBOARD MAP (complete, conflict-audited) ===
GATING RULE: the single-letter bindings (j, k, n, N, /) fire ONLY when the QWebEngineView has focus and no text input is focused. All Ctrl bindings are application-scoped. Ctrl+C is never intercepted when a selection exists.
READING
  Space / PageDown / → / ↓        下一页  (↓ scrolls by a line in 滚动模式; Space always moves a full screen in both modes)
  Shift+Space / PageUp / ← / ↑    上一页
  j / k                            下一页 / 上一页
  Ctrl+→ / Ctrl+PageDown           下一章
  Ctrl+← / Ctrl+PageUp             上一章
  Home / End                       本章开头 / 本章末尾
  Ctrl+Home / Ctrl+End             全书开头 / 全书末尾
  Alt+← / Alt+→                    跳转后退 / 跳转前进 (jump history, NOT page turns)
  Ctrl+G                           跳转到…
  F3 / n                           下一个搜索结果
  Shift+F3 / N                     上一个搜索结果
PANELS
  Ctrl+T   目录        Ctrl+B   书签列表      Ctrl+E   批注列表
  Ctrl+F   在本书中搜索  ( / also focuses it when the book has focus )
  Ctrl+,   排版设置
  Escape   priority order: 1 close the settings panel → 2 close the left dock → 3 exit 专注模式 → 4 exit 全屏 → 5 clear the text selection → 6 do nothing. Never quits, never closes the book.
  F1 / Ctrl+/   快捷键 overlay
ACTIONS
  Ctrl+D          添加/移除书签 (toggles on the current position)
  Ctrl+C          复制所选
  Ctrl+Shift+C    复制所选并附出处
  Ctrl+1..4       用黄/绿/蓝/粉高亮所选
  Delete          删除当前所选中的高亮 (only when a highlight is the active selection)
  Ctrl+M          切换 分页 / 滚动
  Ctrl+= / Ctrl+-  字号增大 / 减小  (Ctrl+滚轮 does the same)
  Ctrl+0          恢复默认字号
  Ctrl+Shift+D    快速切换 日 / 夜
WINDOW & APP
  F11             全屏
  Ctrl+Shift+F    专注模式
  Ctrl+O          打开书籍…
  Ctrl+Shift+L    回到书架
  Ctrl+W          关闭当前书并回到书架；在书架时关闭窗口
  Ctrl+Q          退出
  Ctrl+P          intentionally UNBOUND — printing a reflowed EPUB produces a lie
LIBRARY SCREEN
  ↑ ↓ ← →   移动选择        Enter   打开
  Ctrl+F or /   聚焦搜索框   Escape  清空搜索 / 取消选择
  Delete    从书架移除（不删除文件，需确认）
  Ctrl+O    打开书籍…       F5      重新扫描已添加的文件夹
CONFLICT AUDIT: Ctrl+B is the bookmark PANEL and Ctrl+D ADDS a bookmark, matching browser muscle memory — do not swap them. Ctrl+E is 批注 (calibre uses it for metadata, but v1 has no metadata editor). Ctrl+W closes the BOOK when a book is open; this is the one binding that may surprise, so the status bar flashes 「已回到书架」 for 2s after it fires. Ctrl+0 is font reset, not highlight removal — highlight removal is Delete, which cannot collide because it only fires with a highlight selected. No binding is assigned twice. All Ctrl+digit bindings 5-9 are left free.

5. === 4. PERSISTENCE — LOCATIONS, FORMAT, POLICY ===
DECISION: JSON, not sqlite, for the state of record. The requirement is explicitly 'human-readable/repairable', and I measured that JSON is fast enough at 5x realistic scale (800 books = 472KB/40ms; 3,000 highlights = 1.6MB/60ms). sqlite appears exactly once, LATER, as a disposable derived FTS5 index.
ROOT: %APPDATA%\Verso\  =  C:\Users\mengz\AppData\Roaming\Verso\  — computed from os.environ['APPDATA'], NOT from QStandardPaths (verified: AppConfigLocation returns Local on Windows).
  settings.json              app settings + window state + theme + typography defaults
  library.json               the shelf, one entry per known book
  books\<book_id>.json       position, bookmarks, highlights, per-book overrides, stats
  books\<book_id>.json.bak   previous good generation
  logs\verso.log             rotating 1MB x 3
CACHE ROOT: %LOCALAPPDATA%\Verso\cache\  (Local, because it is regenerable and should not roam)
  covers\<book_id>.jpg       long edge <= 720px, JPEG q86 (~25KB measured; 800 books ~= 20MB)
  index.sqlite               LATER only. FTS5 tokenize="trigram". Safe to delete at any time.
BOOK IDENTITY: book_id = blake2b-128 hex (32 chars) of the ENTIRE file. Measured 73ms for the largest real book, so it is cheap enough to compute on every open. Do NOT use the OPF dc:identifier — the user's book declares its ISBN as the unique-identifier, which collides across editions and printings. Do NOT use the path. Cache the hash keyed by (path, size, mtime_ns) in library.json and only recompute when size or mtime_ns changes.
ATOMIC WRITE (verified, see snippet): copy2 existing → .bak, mkstemp in the SAME directory, write, flush, os.fsync, os.replace. Leaves no temp files and is atomic on NTFS.
RESILIENT READ (verified through 6 failure modes): try primary → on any exception try .bak → if both fail, rename the primary to <name>.json.corrupt-<epoch> and start from a default. Never delete a file the user might want recovered.
VERSIONING: every file carries a top-level integer `schema`. Loader: schema > CURRENT → open READ-ONLY and tell the user to update (verified detected); schema < CURRENT → apply MIGRATIONS[v] in sequence and write back (verified 0→1); no migration registered → fall back to defaults rather than guessing.
WRITE POLICY (this is what 'survives being killed' actually means):
  settings.json    — debounced 500ms after any change, plus on quit.
  books\<id>.json  — position debounced 2000ms, plus IMMEDIATELY on chapter change, panel open, book close, window deactivate and quit. Bookmarks and highlights are flushed IMMEDIATELY and never debounced. Rationale: .bak holds the previous generation, so a crash costs one generation — 2s of scroll position is invisible, a lost highlight is not.
  library.json     — on add/remove/metadata change, on book close, and every 60s while dirty.
  All writes go through ONE serialized writer thread so the UI never blocks on the 40-60ms fsync.
MOVED-FILE SURVIVAL: on ENOENT, scan the last known folder and every folder already referenced in library.json for a candidate whose size matches, then confirm by hash. On a match, rewrite `path`, append the old value to `path_history`, and open the book with only a 3s status note. Only surface the error card when no match is found.

6. === 5. IDENTITY, TITLE, AND UI LANGUAGE ===
NAME: **Verso** — the left-hand page of an open spread. Five ASCII letters, so it is safe as Verso.exe, %APPDATA%\Verso, the ProgId `Verso.Epub.1`, and the named pipe `verso-single-instance` (verified working). It is a reader's word rather than a generic one, and it does not need translating — product names generally should not be (微信 keeps WeChat). If the user wants a Chinese display name later, 「读页」 or 「静读」 both work and ONLY the window-title and About strings change; every filesystem and registry identifier stays `Verso`.
WINDOW TITLE: `书名 — Verso` while reading (em dash, spaces around it), `Verso` on the library screen. The book comes FIRST so that Alt+Tab and the taskbar tooltip show what you are reading rather than what app you are in. Do not put the chapter or the percentage in the title — it churns on every page turn and makes the taskbar flicker.
UI LANGUAGE: **Simplified Chinese only.** Justification: the user is Chinese on a Chinese Windows 11 build, and the books in their library are Chinese. A bilingual UI ('打开 Open') is the compromise that satisfies nobody and doubles every layout's width. A language switcher is a v1 tax with exactly zero users. Keep all strings in one `strings.py` dict so adding an `en` table later is a single file and no code change. Two carve-outs stay ASCII: the app name, and shortcut labels (Ctrl+F, F11) — translating modifier keys is worse than useless. The book's own language is independent: Chinese chrome around an English book is completely normal and must be handled without comment.
TONE OF UI COPY: quiet, plain, declarative. No exclamation marks anywhere. No cute particles (哦/呀/啦), no emoji in copy, no marketing adjectives. Actions are verb-first (打开书籍, not 书籍打开). Errors say what happened and what the user can do next, never assign blame, and never show a traceback unless 技术细节 is expanded. Numbers, percentages and units are half-width with a space before Chinese text (34%, 12 分钟). Filenames and paths always go on their own selectable line rather than being inlined into a sentence with quotes. Never use 「请稍候」 for anything under 300ms — just do it.

7. === 5b. THE ACTUAL STRINGS (zh-Hans) ===
See the strings.py code snippet for the literal dict. Key copy decisions worth calling out: the DRM screen says Verso 不解密受保护的文件 rather than apologizing; the missing-file screen leads with the book title, not the error; 从书架移除 is never 删除 because the app never touches the user's files; the library empty state is three lines and stops. Progress reads 本章剩余 12 分钟 rather than a page count, and the time source is a per-user learned speed (EWMA over sessions >= 90s with no idle gap > 120s, clamped to 120-1200 units/min, seeded at 300 which produced believable 8.1h/9.0h totals for the user's two real books).

8. === IMPLEMENTATION ORDER (build in this sequence; each step is independently testable) ===
1. store.py — the persistence layer from the snippet. It is fully specified and verified; build it first so everything else has somewhere to put state.
2. epub.py — open, classify (the verified error taxonomy), read container/OPF/NCX+nav, decode XHTML with the verified UTF-8 helper, extract cover, count units.
3. The URL scheme handler + a bare QWebEngineView that renders one spine document with the correct charset header. Verify a Chinese page renders without mojibake BEFORE building any chrome.
4. locator.js — paste the verified implementation; wire position save/restore and prove it survives a font-size change and an app restart.
5. Pagination + keyboard map + TOC dock.
6. Typography/theme panel.
7. Library screen + covers.
8. Search, bookmarks, highlights, export.
Do not start on the library screen before step 4 works. A reader that renders and remembers is useful with no library at all; a library over a reader that loses your place is not.


## Open questions

1. App name: I chose **Verso** and specced everything around it. If the user prefers a Chinese display name, 「读页」 and 「静读」 both work — only `title.library`, `title.book` and `menu.about` change; the exe, %APPDATA% folder, ProgId and named pipe must stay ASCII `Verso` regardless. Worth one question to the user before the first build, since renaming the %APPDATA% folder later orphans their library.

2. Default CJK body font: I recommend Microsoft YaHei because on this exact display (161 PPI at 100% Windows scaling, dpr 1.0) SimSun renders thin. But many Chinese readers strongly prefer 宋体 for long-form. This is a one-line default and a 30-second A/B once the renderer exists — get the user to look at both before freezing it. There is no Source Han Serif on this machine, so the usual third option does not exist.

3. Ctrl+W closing the BOOK rather than the WINDOW is the only binding I expect to surprise. I mitigated it with a 2s status flash, but if the user reports it feels wrong, swap to Ctrl+W = close window and Ctrl+Shift+W = back to library.

4. Whether to auto-import the four EPUBs already on the machine on first run, or start with an empty shelf. I lean toward starting empty with the folder-add button prominent — silently scanning Desktop\文件 would be presumptuous — but a one-time offer 「在 Desktop\文件 中发现 4 本书，添加到书架？」 is defensible.

5. Fixed-layout EPUBs are LATER, but the app should still detect them (OPF `rendition:layout` = pre-paginated) and say so rather than rendering them badly. I specced detection into library.json's `layout` field; the renderer agent should decide whether v1 shows an error card or a degraded best-effort render.


## Verified code snippets


### settings.json — LITERAL schema. Lives at %APPDATA%\Verso\settings.json. Written debounced 500ms + on quit.

```json
{
 "schema": 1,
 "app": "Verso",
 "version": "1.0.0",
 "updated": "2026-09-16T14:02:11+08:00",

 "window": {
  "geometry": {"x": 240, "y": 120, "w": 1280, "h": 900},
  "maximized": false,
  "fullscreen": false,
  "zen": false,
  "screen": "\\\\.\\DISPLAY1",
  "dock_visible": true,
  "dock_width": 300,
  "dock_tab": "toc",
  "settings_panel_visible": false,
  "library_view": "grid",
  "library_sort": "recent",
  "last_route": {"kind": "book", "book_id": "3f8a1c0b9d2e4f6a7b8c9d0e1f2a3b4c"}
 },

 "reader": {
  "theme": "system",
  "layout": "paged",
  "font_cjk": "Microsoft YaHei",
  "font_latin": "Georgia",
  "use_book_fonts": false,
  "font_size_px": 21,
  "font_weight": 400,
  "line_height": 1.9,
  "para_spacing_em": 0.6,
  "text_indent_ch": 0,
  "text_align": "left",
  "page_margin_px": 64,
  "max_measure_ch": 40,
  "image_click_zoom": true,
  "invert_images_in_dark": false
 },

 "themes": {
  "day":   {"bg": "#FFFFFF", "fg": "#1A1A1A", "muted": "#6B6B6B", "link": "#1155CC", "sel": "#B4D5FE", "ui_bg": "#F3F3F3", "ui_line": "#E0E0E0"},
  "paper": {"bg": "#F6F0E4", "fg": "#33302B", "muted": "#7A7268", "link": "#8A5A22", "sel": "#E3D3AE", "ui_bg": "#EFE8DA", "ui_line": "#DFD5C0"},
  "night": {"bg": "#16181C", "fg": "#C9CCD1", "muted": "#7E848E", "link": "#7FB4F5", "sel": "#2E4763", "ui_bg": "#101216", "ui_line": "#262A31"}
 },

 "highlight_colors": {
  "yellow": {"day": "#FFF08A", "paper": "#EFDF9A", "night": "#6A5A18"},
  "green":  {"day": "#BDEBBD", "paper": "#C7DEBB", "night": "#2F5433"},
  "blue":   {"day": "#B9DCF7", "paper": "#C2D6E2", "night": "#27455C"},
  "pink":   {"day": "#F8C6D8", "paper": "#E8C3C9", "night": "#5C2C3D"}
 },

 "behavior": {
  "restore_last_book_on_launch": true,
  "confirm_remove_from_shelf": true,
  "auto_hide_chrome_ms": 3000,
  "reading_speed_units_per_min": 300,
  "reading_speed_learned": true,
  "idle_timeout_s": 120,
  "single_instance": true,
  "watch_folders": ["C:\\Users\\mengz\\Desktop\\文件\\Econ Books"]
 },

 "shortcut_overrides": {}
}
```

### library.json — LITERAL schema. %APPDATA%\Verso\library.json. One entry per known book; files are referenced in place and never copied.

```json
{
 "schema": 1,
 "updated": "2026-09-16T14:02:11+08:00",
 "books": [
  {
   "id": "3f8a1c0b9d2e4f6a7b8c9d0e1f2a3b4c",
   "hash_algo": "blake2b-128",
   "size": 13082119,
   "mtime_ns": 1723456789012345600,
   "path": "C:\\Users\\mengz\\Desktop\\文件\\Econ Books\\Econ Books_China\\从此岸到彼岸_人民币汇率如何实现清洁浮动_缪延亮.epub",
   "path_history": [],
   "verified_at": "2026-09-16T13:58:02+08:00",
   "missing": false,

   "title": "从此岸到彼岸",
   "subtitle": "人民币汇率如何实现清洁浮动",
   "authors": ["缪延亮"],
   "publisher": "ZHE JIANG PUBLISHING UNITED GROUP",
   "pubdate": "2019-10",
   "language": "zh-CN",
   "identifiers": {"ISBN": "9787522001494"},
   "epub_version": "2.0",
   "toc_source": "ncx",
   "layout": "reflowable",
   "drm": null,

   "spine_count": 24,
   "units_total": 145962,
   "units_cjk": 136498,
   "units_latin": 9464,

   "cover": "covers/3f8a1c0b9d2e4f6a7b8c9d0e1f2a3b4c.jpg",
   "cover_w": 720,
   "cover_h": 1002,

   "added_at": "2026-09-14T21:03:00+08:00",
   "opened_at": "2026-09-16T13:58:02+08:00",
   "finished_at": null,
   "progress": 0.3412,
   "seconds_read": 7412,
   "tags": []
  }
 ]
}
```

### books/<book_id>.json — LITERAL schema. Every anchor is the verified 5-field locator. `drm`, when non-null, is one of "adept" | "lcp" | "unknown".

```json
{
 "schema": 1,
 "book_id": "3f8a1c0b9d2e4f6a7b8c9d0e1f2a3b4c",
 "title": "从此岸到彼岸",
 "updated": "2026-09-16T14:02:11+08:00",

 "position": {
  "spine_index": 7,
  "spine_href": "ops/chapter3.xhtml",
  "locator": {
   "gpos": 4820,
   "path": [12],
   "offset": 133,
   "text": "以外汇储备为代表的官方资本改变了全球资本流动和金融市场格局。随着各国资本",
   "before": "本节讨论的核心问题是"
  },
  "doc_progress": 0.271,
  "book_progress": 0.3412,
  "chapter_title": "第三章 汇率的锚",
  "at": "2026-09-16T14:02:11+08:00"
 },

 "history": [
  {"spine_index": 3, "gpos": 900, "at": "2026-09-16T13:40:00+08:00"}
 ],

 "bookmarks": [
  {
   "id": "b_01a0a8cd0efd41c2",
   "spine_index": 7,
   "spine_href": "ops/chapter3.xhtml",
   "locator": {"gpos": 4820, "path": [12], "offset": 133,
                "text": "以外汇储备为代表的官方资本改变了全球资本流动", "before": "本节讨论的核心问题是"},
   "chapter_title": "第三章 汇率的锚",
   "book_progress": 0.3412,
   "label": "",
   "created_at": "2026-09-16T14:02:11+08:00"
  }
 ],

 "highlights": [
  {
   "id": "h_01a0a8cd0ef7d30c",
   "spine_index": 7,
   "spine_href": "ops/chapter3.xhtml",
   "start": {"gpos": 4820, "path": [12], "offset": 133,
              "text": "以外汇储备为代表的官方资本改变了全球资本流动", "before": "本节讨论的核心问题是"},
   "end":   {"gpos": 4874, "path": [12], "offset": 187,
              "text": "金融市场格局。随着各国资本账户的开放", "before": "改变了全球资本流动和"},
   "text": "以外汇储备为代表的官方资本改变了全球资本流动和金融市场格局。",
   "note": "对照第 5 章的三元悖论讨论",
   "color": "yellow",
   "style": "fill",
   "chapter_title": "第三章 汇率的锚",
   "book_progress": 0.3412,
   "created_at": "2026-09-16T14:02:11+08:00",
   "updated_at": "2026-09-16T14:05:40+08:00",
   "anchor_state": "exact"
  }
 ],

 "overrides": {
  "font_size_px": 23,
  "line_height": 2.0
 },

 "stats": {
  "seconds_read": 7412,
  "sessions": 9,
  "units_read": 49800,
  "last_session_at": "2026-09-16T14:02:11+08:00",
  "speed_samples": [312, 287, 341]
 }
}
```

### store.py — the persistence layer. VERIFIED: ran clean roundtrip, .bak creation, truncated-primary recovery, both-files-trashed quarantine, 0→1 migration, and future-schema detection. Copy as-is.

```python
import os, json, time, tempfile, shutil, secrets, hashlib, datetime

SCHEMA = 1

def app_dir():
    # Do NOT use QStandardPaths.AppConfigLocation: on Windows it returns LOCALAPPDATA.
    return os.path.join(os.environ["APPDATA"], "Verso")

def cache_dir():
    return os.path.join(os.environ["LOCALAPPDATA"], "Verso", "cache")

def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")

def new_id(prefix):                     # time-sortable, stdlib only
    return f"{prefix}_{int(time.time()*1000):011x}{secrets.token_hex(3)}"

def book_id(path):                      # 73ms for 13MB, measured
    h = hashlib.blake2b(digest_size=16)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

def write_json(path, obj):
    obj["schema"] = SCHEMA
    obj["updated"] = now_iso()
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    if os.path.exists(path):
        try: shutil.copy2(path, path + ".bak")
        except OSError: pass
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)           # atomic on NTFS, verified
    except BaseException:
        try: os.unlink(tmp)
        except OSError: pass
        raise

MIGRATIONS = {}                          # {from_version: callable(obj) -> obj}

def read_json(path, default_factory):
    """Returns (obj, source_tag). Tries primary, then .bak, then quarantines."""
    for cand, tag in ((path, "primary"), (path + ".bak", "backup")):
        if not os.path.exists(cand):
            continue
        try:
            with open(cand, encoding="utf-8") as f:
                obj = json.load(f)
        except Exception:
            continue
        v = obj.get("schema", 0)
        if v > SCHEMA:
            return obj, f"{tag}:READ_ONLY_FUTURE_SCHEMA({v})"
        while v < SCHEMA:
            fn = MIGRATIONS.get(v)
            if fn is None:
                return default_factory(), f"{tag}:UNMIGRATABLE({v})"
            obj = fn(obj); v += 1; obj["schema"] = v
        return obj, tag
    if os.path.exists(path):
        os.replace(path, f"{path}.corrupt-{int(time.time())}")
        return default_factory(), "reset:CORRUPT_QUARANTINED"
    return default_factory(), "new"
```

### locator.js — position and highlight anchoring. VERIFIED in a live QWebEngineView to resolve 'exact@gpos' after head-style injection, body-style injection, and highlight <mark> wrapping, and to recover via snippet after the book content itself changed. The two earlier designs I tried both returned FAILED. The SCRIPT/STYLE/NOSCRIPT rejection is load-bearing.

```javascript
let IDX = null;

function buildIndex() {
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
    acceptNode(n) {
      const p = n.parentElement;
      if (!p) return NodeFilter.FILTER_REJECT;
      const t = p.tagName;
      // REQUIRED: without this, CSS we inject shifts every offset in the document.
      if (t === 'SCRIPT' || t === 'STYLE' || t === 'NOSCRIPT') return NodeFilter.FILTER_REJECT;
      return n.data.length ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
    }
  });
  const nodes = [], starts = [];
  let text = '', n;
  while ((n = w.nextNode())) { starts.push(text.length); nodes.push(n); text += n.data; }
  return (IDX = { nodes, starts, text });
}

function nodeAt(gpos) {                       // global char offset -> DOM point
  const { nodes, starts } = IDX;
  let lo = 0, hi = starts.length - 1, best = 0;
  while (lo <= hi) { const m = (lo + hi) >> 1;
    if (starts[m] <= gpos) { best = m; lo = m + 1; } else hi = m - 1; }
  return { node: nodes[best], offset: gpos - starts[best] };
}

function elemPath(el) {
  const p = []; let n = el;
  while (n && n !== document.body && n.parentElement) {
    let i = 0, s = n;
    while ((s = s.previousElementSibling)) i++;
    p.unshift(i); n = n.parentElement;
  }
  return p;
}
function elemFromPath(p) {
  let n = document.body;
  for (const i of p) { n = n.children[i]; if (!n) return null; }
  return n;
}

function makeLocator(gpos) {                  // -> the object stored in JSON
  if (!IDX) buildIndex();
  const t = IDX.text, pt = nodeAt(gpos);
  return {
    gpos,
    path: elemPath(pt.node.parentElement),
    offset: pt.offset,
    text: t.substr(gpos, 40),
    before: t.substr(Math.max(0, gpos - 20), Math.min(20, gpos))
  };
}

// Returns {gpos, state} where state is 'exact' | 'shifted' | 'lost'.
function resolveLocator(loc) {
  buildIndex();                               // always rebuild against current DOM
  const t = IDX.text;
  if (t.substr(loc.gpos, 40) === loc.text) return { gpos: loc.gpos, state: 'exact' };
  const key = loc.text.substr(0, 20);
  if (key) {                                  // nearest occurrence wins; `before` breaks ties
    const hits = []; let i = -1;
    while ((i = t.indexOf(key, i + 1)) >= 0) hits.push(i);
    if (hits.length) {
      let best = hits[0];
      for (const h of hits) {
        const better = loc.before
          ? (t.substr(Math.max(0, h - loc.before.length), loc.before.length) === loc.before)
          : false;
        if (better) { best = h; break; }
        if (Math.abs(h - loc.gpos) < Math.abs(best - loc.gpos)) best = h;
      }
      return { gpos: best, state: 'shifted' };
    }
  }
  const el = elemFromPath(loc.path || []);    // element path as a third chance
  if (el) {
    const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let n, seen = 0;
    while ((n = w.nextNode())) { if (el.contains(n)) return { gpos: seen, state: 'shifted' }; seen += n.data.length; }
  }
  return { gpos: Math.min(loc.gpos, Math.max(0, t.length - 1)), state: 'lost' };
}

function rangeFor(startLoc, endLoc) {         // for painting a highlight
  const a = resolveLocator(startLoc), b = resolveLocator(endLoc);
  if (a.state === 'lost' || b.state === 'lost') return null;
  const p1 = nodeAt(a.gpos), p2 = nodeAt(b.gpos);
  const r = document.createRange();
  r.setStart(p1.node, p1.offset); r.setEnd(p2.node, p2.offset);
  return r;
}
```

### epub_open.py — UTF-8 decode helper and the verified error/DRM classifier. The decode() function fixes the mojibake bug I reproduced on the user's own book; classify() was validated against 8 synthesized files including the font-obfuscation false positive.

```python
import os, zipfile
from lxml import etree, html as lhtml

CONT_NS = "{urn:oasis:names:tc:opendocument:xmlns:container}"
ENC_NS  = "{http://www.w3.org/2001/04/xmlenc#}"
# Font obfuscation is NOT DRM. Books using it must open normally.
OBFUSCATION_ALGS = {
    "http://www.idpf.org/2008/embedding",
    "http://ns.adobe.com/pdf/enc#RC4",
}

def decode(raw: bytes) -> str:
    """EPUB content docs are UTF-8 or UTF-16. NEVER hand raw bytes to lxml:
    the user's files carry no XML decl and no <meta charset>, so lxml guesses
    latin-1 and yields mojibake with zero CJK characters (reproduced)."""
    if raw[:3] == b"\xef\xbb\xbf":
        return raw[3:].decode("utf-8", "replace")
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", "replace")
    return raw.decode("utf-8", "replace")

def doc_text(raw: bytes) -> str:
    """Plain text of one spine document, for search and unit counting.
    13ms for a whole 24-document book, measured."""
    d = lhtml.fromstring(decode(raw))          # STRING, not bytes
    for bad in d.xpath("//script|//style"):
        bad.getparent().remove(bad)
    return d.text_content()

def classify(path):
    """-> ('ok', zf) | ('not_a_zip', detail) | ('drm', 'adept'|'lcp'|'unknown')
        | ('no_container', None) | ('bad_opf', detail)"""
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        return ("not_a_zip", str(e))
    except OSError as e:
        return ("not_a_zip", f"{type(e).__name__}: {e}")
    names = set(z.namelist())
    if "META-INF/encryption.xml" in names:
        try:
            e = etree.fromstring(z.read("META-INF/encryption.xml"))
            algs = {m.get("Algorithm") for m in e.iter(ENC_NS + "EncryptionMethod")}
        except Exception:
            algs = {"unknown"}
        if algs - OBFUSCATION_ALGS:            # real encryption, not font mangling
            kind = ("lcp"   if "META-INF/license.lcpl" in names else
                    "adept" if "META-INF/rights.xml"   in names else "unknown")
            return ("drm", kind)
    if "META-INF/container.xml" not in names:
        return ("no_container", None)
    try:
        c = etree.fromstring(z.read("META-INF/container.xml"))
        opf = c.find(f".//{CONT_NS}rootfile").get("full-path")
        z.read(opf)
    except Exception as e:
        return ("bad_opf", f"{type(e).__name__}: {e}")
    return ("ok", z)

def zip_path(base_entry, href):
    """Resolve an href relative to the OPF, as a zip entry name."""
    return os.path.normpath(
        os.path.join(os.path.dirname(base_entry), href)
    ).replace("\\", "/")
```

### strings.py — the literal Simplified-Chinese UI copy for every surface described in the chrome spec. One dict so an 'en' table is a drop-in later.

```python
APP_NAME = "Verso"          # never translated; also the exe / %APPDATA% / ProgId name

S = {
 # ---- window / global ----
 "title.book":            "{title} — Verso",
 "title.library":         "Verso",

 # ---- toolbar & overflow menu ----
 "tb.toc":                "目录",
 "tb.back":               "后退",
 "tb.forward":            "前进",
 "tb.typography":         "排版",
 "tb.search":             "搜索",
 "tb.bookmark":           "添加书签",
 "tb.bookmark.remove":    "移除书签",
 "tb.fullscreen":         "全屏",
 "tb.more":               "更多",
 "menu.open":             "打开书籍…",
 "menu.library":          "回到书架",
 "menu.bookinfo":         "书籍信息",
 "menu.export":           "导出批注…",
 "menu.shortcuts":        "快捷键",
 "menu.settings":         "设置",
 "menu.about":            "关于 Verso",
 "menu.quit":             "退出",

 # ---- left dock ----
 "dock.toc":              "目录",
 "dock.bookmarks":        "书签",
 "dock.notes":            "批注",
 "dock.search":           "搜索",
 "toc.none":              "本书未提供目录，以下为章节文件",
 "bookmarks.empty":       "还没有书签。按 Ctrl+D 添加。",
 "notes.empty":           "还没有批注。选中文字后按 Ctrl+1 至 Ctrl+4 高亮。",
 "notes.filter.noted":    "仅显示有笔记",
 "notes.lost":            "无法定位",
 "notes.export":          "导出为 Markdown…",
 "search.placeholder":    "在本书中搜索",
 "search.count":          "共 {n} 处",
 "search.capped":         "仅显示前 {n} 处",
 "search.none":           "没有找到「{q}」",

 # ---- settings panel ----
 "set.theme":             "主题",
 "set.theme.day":         "日",
 "set.theme.paper":       "纸",
 "set.theme.night":       "夜",
 "set.theme.system":      "跟随系统",
 "set.font":              "字体",
 "set.font.cjk":          "中文字体",
 "set.font.latin":        "西文字体",
 "set.font.book":         "使用书籍自带字体",
 "set.font.size":         "字号",
 "set.font.weight":       "字重",
 "set.weight.normal":     "常规",
 "set.weight.light":      "细",
 "set.layout":            "排版",
 "set.line_height":       "行距",
 "set.para_spacing":      "段间距",
 "set.indent":            "段首缩进",
 "set.indent.none":       "无",
 "set.indent.two":        "两字符",
 "set.align":             "对齐",
 "set.align.left":        "左对齐",
 "set.align.justify":     "两端对齐",
 "set.margin":            "页边距",
 "set.measure":           "单行字数上限",
 "set.paging":            "翻页",
 "set.paging.paged":      "分页",
 "set.paging.scroll":     "滚动",
 "set.thisbook":          "仅用于本书",
 "set.reset":             "恢复默认",
 "set.assoc":             "设为默认 EPUB 阅读器",

 # ---- status bar ----
 "status.percent":        "{p}%",
 "status.left.chapter":   "本章剩余 {m} 分钟",
 "status.left.book":      "全书剩余 {h} 小时 {m} 分钟",
 "status.read.time":      "已读 {h} 小时 {m} 分钟",
 "status.moved":          "文件已移动，位置已更新",
 "status.back.library":   "已回到书架",
 "status.copied":         "已复制",
 "status.bookmark.added": "已添加书签",
 "status.undo":           "撤销",

 # ---- library ----
 "lib.open":              "打开书籍…",
 "lib.add_folder":        "添加文件夹…",
 "lib.search":            "搜索书名、作者",
 "lib.sort.recent":       "最近阅读",
 "lib.sort.added":        "添加时间",
 "lib.sort.title":        "书名",
 "lib.sort.author":       "作者",
 "lib.sort.progress":     "阅读进度",
 "lib.section.continue":  "继续阅读",
 "lib.section.all":       "全部书籍",
 "lib.empty.title":       "书架是空的",
 "lib.empty.body":        "把 EPUB 文件拖到这里，或者",
 "lib.ctx.open":          "打开",
 "lib.ctx.reveal":        "在文件夹中显示",
 "lib.ctx.copypath":      "复制文件路径",
 "lib.ctx.info":          "书籍信息",
 "lib.ctx.remove":        "从书架移除",
 "lib.badge.missing":     "文件缺失",
 "lib.remove.confirm":    "从书架移除《{title}》？文件不会被删除。",
 "lib.remove.yes":        "移除",

 # ---- error cards ----
 "err.corrupt.title":     "无法打开这本书",
 "err.corrupt.body":      "文件可能已损坏，或者不是 EPUB 格式。",
 "err.drm.title":         "这本书有 DRM 保护",
 "err.drm.body":          "检测到 {kind} 加密。Verso 不解密受保护的文件，请用购买它的官方应用打开。",
 "err.drm.adept":         "Adobe ADEPT",
 "err.drm.lcp":           "Readium LCP",
 "err.drm.unknown":       "未知",
 "err.missing.title":     "找不到这个文件",
 "err.missing.body":      "《{title}》原来在：",
 "err.structure.title":   "这个 EPUB 结构不完整",
 "err.structure.body":    "缺少 META-INF/container.xml，无法确定书籍内容。",
 "err.section":           "本节内容无法显示",
 "err.details":           "技术细节",
 "err.btn.reveal":        "在文件夹中显示",
 "err.btn.relocate":      "重新定位…",
 "err.btn.remove":        "从书架移除",
 "err.btn.close":         "关闭",
 "err.relocate.mismatch": "这似乎是另一个文件，仍要关联吗？",
 "err.relocate.yes":      "仍要关联",
 "err.relocate.no":       "取消",

 # ---- shortcuts overlay ----
 "keys.title":            "快捷键",
 "keys.group.reading":    "阅读",
 "keys.group.panels":     "面板",
 "keys.group.actions":    "操作",
 "keys.group.window":     "窗口",
 "keys.group.library":    "书架",
 "keys.close":            "按 Esc 关闭",
}
```

### Reading-speed learning + time-left, the formula behind 本章剩余. Seeded at 300 units/min, which produced credible 8.1h and 9.0h totals for the user's two real books (145,962 and 162,499 units).

```python
import re

CJK   = re.compile(r"[㐀-䶿一-鿿豈-﫿\U00020000-\U0002ebef]")
LATIN = re.compile(r"[A-Za-z0-9’'\-]+")

def count_units(text):
    """One 'unit' = one CJK character OR one Latin word. Mixing them this way
    keeps the estimate stable for the bilingual books in this library."""
    cjk = len(CJK.findall(text))
    lat = len(LATIN.findall(text))
    return cjk + lat, cjk, lat

MIN_SPEED, MAX_SPEED, ALPHA = 120.0, 1200.0, 0.25

def update_speed(current, units_advanced, active_seconds):
    """EWMA. Only call for sessions >= 90s with no idle gap > behavior.idle_timeout_s."""
    if active_seconds < 90 or units_advanced <= 0:
        return current
    sample = units_advanced / (active_seconds / 60.0)
    sample = max(MIN_SPEED, min(MAX_SPEED, sample))
    return (1 - ALPHA) * current + ALPHA * sample

def minutes_left(units_remaining, speed):
    return max(0, round(units_remaining / max(speed, MIN_SPEED)))

def fmt_left(minutes, scope="chapter"):
    if scope == "chapter":
        return f"本章剩余 {minutes} 分钟"
    h, m = divmod(minutes, 60)
    return f"全书剩余 {h} 小时 {m} 分钟" if h else f"全书剩余 {m} 分钟"
```

## Files written

- `C:\Users\mengz\AppData\Local\Temp\claude\C--Users-mengz-AppData-Roaming-Claude-scratch-workspaces-e0856583-45bd-4638-bcde-2436fa733254-106bf0b0-5deb-47b4-9742-47c535dfb989-scratch-2026-09-16-61ae22\7fd24e9e-f1ca-47c4-a147-c0cd4754ccf9\scratchpad\probe_locator.py`

- `C:\Users\mengz\AppData\Local\Temp\claude\C--Users-mengz-AppData-Roaming-Claude-scratch-workspaces-e0856583-45bd-4638-bcde-2436fa733254-106bf0b0-5deb-47b4-9742-47c535dfb989-scratch-2026-09-16-61ae22\7fd24e9e-f1ca-47c4-a147-c0cd4754ccf9\scratchpad\probe_epub_error_states.py`

- `C:\Users\mengz\AppData\Local\Temp\claude\C--Users-mengz-AppData-Roaming-Claude-scratch-workspaces-e0856583-45bd-4638-bcde-2436fa733254-106bf0b0-5deb-47b4-9742-47c535dfb989-scratch-2026-09-16-61ae22\7fd24e9e-f1ca-47c4-a147-c0cd4754ccf9\scratchpad\probe_xhtml_encoding.py`

- `C:\Users\mengz\AppData\Local\Temp\claude\C--Users-mengz-AppData-Roaming-Claude-scratch-workspaces-e0856583-45bd-4638-bcde-2436fa733254-106bf0b0-5deb-47b4-9742-47c535dfb989-scratch-2026-09-16-61ae22\7fd24e9e-f1ca-47c4-a147-c0cd4754ccf9\scratchpad\probe_store.py`
