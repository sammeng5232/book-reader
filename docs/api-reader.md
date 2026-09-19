# `assets/reader.js` + `assets/reader.css` — in-page reading engine (owner C)

The engine runs inside every book document. `webhost.py` injects `reader.js` at
DocumentCreation in ApplicationWorld and `reader.css` as `<style id="__er_reader">`.
Python drives it only through `window.epubReader` (examples use
`webhost.BookHost.call_reader`, which JSON-encodes arguments and decodes the return value).
The page talks back only through `window.epubReaderHost`.

## 1. Units and invariants

- **gpos is measured in Unicode code points** (Python `str` indices into
  `epublib.plain_text(zip_name)`), **not** UTF-16 code units. That applies to
  `locator.gpos`, `search()` results, `showMatches()` ranges, `applyHighlights()`
  start/end, `selectionInfo()` start/end and `state().gpos`. Verified with U+20BB7 and U+2A6A5.
- `flatText()` equals `epublib.plain_text()` character for character (CONTRACT §2.1):
  text nodes under `<body>` in document order, raw `nodeValue` concatenated, text inside
  `script`/`style`/`noscript` rejected at any depth and in any namespace, `display:none` text kept.
  Verified against all 134 spine documents of the user's three books.
- Offsets are chapter-local. For whole-book percent, pass `book: {offset, total}` to `init()`
  (`offset` = code points in earlier spine items, `total` = the whole book).
- Page turns are instant. There is no animation and no setting for one.
- Chapters without a DOCTYPE (most real EPUBs start with `<?xml ...?>`) render in
  quirks mode. The engine measures and scrolls through `document.scrollingElement`,
  so both modes paginate the same way. Verified on both kinds of chapter.

## 2. `window.epubReader` (CONTRACT §4)

Every call is safe **before DOMContentLoaded and before `init()`**. Until the engine can
run the call, it is queued, replayed in order, and returns the "not ready" value
(`null`, `false`, `''` or `[]`). Every call is idempotent: injecting the script twice,
`init()` twice, or `applySettings(s)` twice leaves one engine, one set of
`<style data-er>` elements, one set of listeners and the same state.

### `init(config) -> state & {restored, viewport}`
`config` fields (all optional):

| key | meaning |
|---|---|
| `settings` | the `reader` settings dict from `Store.reader_settings()` (keys below) |
| `mode` | `'paginated'` \| `'scroll'`. Overrides `settings.layout` |
| `locator` | a locator from a previous `capture()`, or just `{gpos}` |
| `book` | `{offset, total}` in code points, for whole-book `percent` |
| `fixedLayout` | `true` for OPF `rendition:layout=pre-paginated` (otherwise detected from `<meta name=viewport>`) |
| `viewport` | `{w, h}` page size for fixed layout, when the document does not declare one |
| `cssText` / `cssHref` | only for hosts that do not inject `reader.css` themselves |
| `hostPayload` | `'object'` (default: the bridge facade stringifies) or `'json'` |
```python
host.call_reader("init", {"settings": store.reader_settings(bid), "mode": "paginated",
                          "locator": saved_loc, "book": {"offset": 18230, "total": 194766}},
                 callback=on_state)
```

`settings` keys (store.py `reader` block): `theme` (`day|paper|night`, also `light|sepia|dark`;
resolve `system` in Python), `colors` (optional token dict, see §4), `layout` (`paged|scroll`),
`font_cjk`, `font_latin`, `use_book_fonts`, `font_size_px`, `font_weight` (0 = keep the book's),
`line_height`, `para_spacing_em` and `text_indent_ch` (`null` keeps the book's paragraph
design; a number overrides `p` rules), `text_align`, `page_margin_px`,
`max_measure_ch` (0 = fill the window), `image_click_zoom`, `invert_images_in_dark`,
`highlight_colors` (store.py format, used as palette defaults).

### `applySettings(settings) -> state`
Live. It re-derives the presentation and returns to the pinned position, so font, theme,
margin, layout-mode and window changes do not move the reader. Measured drift: 0 characters.
```python
host.call_reader("applySettings", {**settings, "font_size_px": 24})
```

### `state() -> {page, pages, gpos, percent, chapterPercent, mode, fixedLayout, rtl, vertical, chars, matches, matchActive}`
`mode` is `'paginated'` or `'scroll'`. A fixed-layout page reports `'paginated'` with
`fixedLayout: true` and `pages: 1`. A vertical-writing book reports `'scroll'`, `vertical: true`.
```python
host.call_reader("state", callback=lambda st: status.set_page(st["page"] + 1, st["pages"]))
```

### `nextPage() / prevPage() -> state`
Turns one page (scroll mode: one screen). Past the last or first page nothing moves.
The page emits `keyUnhandled('EpubReader.NextChapter')` or `('EpubReader.PrevChapter')`
so Python can load the next spine item. Fixed-layout pages always hand off this way.
```python
host.call_reader("nextPage")
```

### `gotoPage(n) -> state` / `gotoPercent(p) -> state`
`n` is 0-based and clamped. `p` is 0..1 of the chapter, measured in characters.
```python
host.call_reader("gotoPage", 0)
host.call_reader("gotoPercent", 0.5)
```

### `capture() -> locator`
`{gpos, snippet, before, path, page, pages, pageDelta, total, percent, chapterPercent, mode, scrollFrac}`.
This is the **pinned** locator: the last user navigation or explicit restore, with the
page fields refreshed. It is not a fresh read of the page top, because re-anchoring after
a relayout creeps. Persist it as-is.
```python
host.call_reader("capture", callback=lambda loc: store.save_book_state(bid, {**state, "position": loc}))
```

### `restore(locator) -> bool`
Resolution order: `gpos` (checked against `snippet`), then `snippet` search (with `before`),
then `path`, then clamped `gpos`, then `chapterPercent`. `true` means the position resolved exactly.
The position is pinned, so later relayouts keep it. `{gpos}` alone is accepted, for
example a search hit or a highlight.
```python
host.call_reader("restore", {"gpos": hit.gpos})
```

### `gotoFragment(id) -> bool`
Jumps to the page (or scroll position) of `#id`, falling back to `[name=id]`.
```python
host.call_reader("gotoFragment", fragment)
```

### `flatText() -> str`
The flattened string of §1. Its Python `len()` is the code-point length. Works without `init()`.
```python
host.call_reader("flatText", callback=lambda s: assert_equal(s, book.plain_text(zip_name)))
```

### `search(query) -> [{gpos, length}]`
In-document search. Case-folding keeps lengths the same, and curly quotes and dashes are
normalised. Phrases may span inline elements (`<em>`, `<b>`). All matches are painted.
At most 5000 matches.
```python
host.call_reader("search", "汇率", callback=show_hits)
```

### `showMatches(ranges, active) -> {count, active, page, pages}`
Paints `ranges` (`[{gpos, length}]` or `[{gpos, end}]`) through the CSS Custom Highlight API
and jumps to `ranges[active]` (pass -1 to paint without moving). No DOM mutation and
no reflow: measured 0 mutations and identical geometry with 575 ranges.
```python
host.call_reader("showMatches", [{"gpos": h.gpos, "length": h.length} for h in hits], 3)
```

### `applyHighlights(list) -> {applied, lost, states}`
`list = [{id, start, end, color, note, style}]`. `color` is `yellow|green|blue|pink` and
`style` is `fill|underline`. Replaces the whole set. Highlights survive `restore()`,
`applySettings()` and resizes (painting is redone after every relayout).
Clicking a highlight emits `noteRequested(id)`.
```python
host.call_reader("applyHighlights", [{"id": h["id"], "start": h["start"], "end": h["end"], "color": "yellow"}])
```

### `selectionInfo() -> {text, start, end, snippet, before, page, percent} | null`
```python
host.call_reader("selectionInfo", callback=lambda sel: sel and dock.offer_highlight(sel))
```

### `clearSelection() -> true`
```python
host.call_reader("clearSelection")
```

### `zoomImage(on) -> bool`
`true`/`false` turns click-to-zoom on or off. A CSS selector opens that image in the overlay.
```python
host.call_reader("zoomImage", False)
```

### Extras, not named by the contract
| call | meaning |
|---|---|
| `setMode('paginated'\|'scroll')` | `applySettings` with only `layout` changed |
| `relayout()` | re-measure now and keep the position |
| `clearMatches()` | remove search paint |
| `hideNote()` / `closeZoom()` | dismiss the footnote popover / image overlay |
| `flatTextInfo()` | `{utf16Length, codePointLength, astralPairs, nodes, head, tail}` |
| `drainEvents()` | signals that could not be delivered (no bridge yet) |
| `debug()` | geometry, tokens and queue state, for tests |

## 3. `window.epubReaderHost` (provided by webhost.py)

The engine calls these and passes **objects** (the facade stringifies them):

| call | when |
|---|---|
| `ready(state)` | end of `init()` |
| `positionChanged(state)` | after every navigation or relayout (batched per microtask) |
| `linkClicked(href)` | a link click, taken on `click` so the navigation is cancelled. Footnote links (`epub:type=noteref`, `role=doc-noteref`) open a popover instead |
| `selectionChanged(info\|null)` | selection changes (80 ms debounce), and on right-click |
| `noteRequested(id)` | a highlight was clicked |
| `keyUnhandled(desc)` | keys the page does not own: `Ctrl+F`, `Escape`, `A`, `F1`… plus the synthetic `EpubReader.NextChapter`, `EpubReader.PrevChapter`, `EpubReader.TapCentre` |

Input is captured on `window`, in the capture phase, from the moment the script is
injected. Verified with real key presses: the page turns and a book's own
window, document and body listeners never see the key. Keys the page does not own
still reach the book. The page owns Right/Down/PageDown/Space/Enter (next),
Left/Up/PageUp/Shift+Space (previous), Home/End, the mouse wheel (one page per notch,
40 px with a 110 ms cooldown), horizontal swipes, and 22%/56%/22% tap zones.

## 4. Theme tokens (`reader.css`)

The page is coloured only through `--er-bg --er-fg --er-secondary --er-accent --er-border
--er-selection --er-hl-yellow --er-hl-green --er-hl-blue --er-hl-pink --er-find-bg
--er-find-fg --er-find-active-bg --er-find-active-fg`.
Precedence, highest first:
1. `settings.colors` passed to `init()` or `applySettings()`. Keys may be `theme.Theme.tokens()` names
   (`bg`, `hl_yellow`, `find_bg`…), `css_variables()` names (`--er-bg`…) or product-spec names
   (`muted`, `link`, `sel`, `ui_line`). Optional `scheme: 'dark'|'light'`.
2. `theme.css_text(theme)` injected by `webhost.set_theme_css()`. This is the normal path.
3. The engine's built-in palette for `settings.theme`, with `settings.highlight_colors` merged in.
4. `reader.css` defaults.

Levels 3 and 4 sit in `@layer er-theme` under `:where(:root)`, so level 2 wins whatever the
`<style>` order. Dark or light is decided from the background that actually won, and it
drives the book-colour flip and the dimming of images.
```python
host.set_theme_css(theme.css_text(theme.resolve_theme("dark")))
host.call_reader("applySettings", {**settings, "theme": "night"})
```

Cascade layers: `er-safety` holds geometry, uses `!important` and always wins. `er-theme` holds
typography and colour defaults, has no `!important`, and the book's CSS wins over it, so italics,
small caps, indents and semantic colours survive. Reader chrome (`::highlight`, popover, zoom) is unlayered.
CSS names use the `er-` prefix: layers `er-safety`/`er-theme`, class `er-hscroll`,
ids `er-pop`/`er-zoom`, attributes `data-er`, `data-er-fxl`, `data-er-zoom`, `data-er-imgdark`.

## 5. Known limits

- Vertical writing (`writing-mode: vertical-*`) always uses a horizontal scroll in the book's own
  writing mode. There is no paginated vertical mode. Verified for `vertical-rl`.
- Elements the book positions absolutely, or that were `position:fixed`, land on page 0.
- Right-to-left books paginate exactly (`scrollLeft = -page × W`, verified), but the harness
  covered only a `dir=rtl` fixture, not a real RTL book.
- Known §2.1 divergence on the Python side (owner A): the text inside an inline `<svg><style>` is
  rejected here, as the contract says, but `epublib.flatten_html` keeps it. None of the user's
  134 spine documents contain one.
