# Product decisions from the user (authoritative — override earlier research)

## 1. App name: "EPUB Reader"

The research phase specced the working name `Verso` throughout. The user chose **EPUB Reader**.
Rename pass required across every file:

| Thing | Value |
|---|---|
| Display name (window title, About, library screen, cheat sheet) | `EPUB Reader` |
| Window title while reading | `<书名> — EPUB Reader` |
| Executable | `EPUB Reader.exe` |
| State root | `%APPDATA%\EPUB Reader\` |
| Cache root | `%LOCALAPPDATA%\EPUB Reader\cache\` |
| ProgId (registry, no spaces allowed) | `EPUBReader.Epub.1` |
| Single-instance pipe | `epub-reader-single-instance` |
| Qt org / app name | `EPUB Reader` |
| Log file | `%APPDATA%\EPUB Reader\logs\epub-reader.log` |

Migration: if a `%APPDATA%\Verso\` directory exists from development builds, move it to the new root on
first launch rather than orphaning it, then never look again.

## 2. UI language: user-selectable, four languages

The user explicitly asked to **choose between 简体中文, 繁體中文, English, and 日本語**. This supersedes the
research recommendation of Chinese-only with no switcher.

Requirements:
- `strings.py` carries four complete tables with **identical key sets, no gaps**: `zh-Hans`, `zh-Hant`, `en`, `ja`.
- Setting `ui.language` accepts `auto | zh-Hans | zh-Hant | en | ja`. `auto` resolves from the Windows display
  language at first launch (`QLocale.system()`), falling back to `en` for anything unmatched.
- The selector lives in the settings panel (界面语言 / 介面語言 / Language / 表示言語) and is **live** — changing it
  retranslates the running UI immediately. No restart prompt; every widget gets a `retranslate_ui()`.
- The chosen language is remembered across restarts.
- Translations must be idiomatic, not mechanical:
  - `zh-Hant` is not a character-by-character conversion of `zh-Hans`. Use Taiwan/HK software conventions
    (檔案 not 文件 for files, 設定 not 设置, 書籤 not 书签→書簽 only where correct, 搜尋 not 搜索, 字型 not 字体).
  - `ja` follows Japanese UI convention: 開く, 設定, 目次, しおり, 検索, 表示, 全画面表示; katakana for loanwords;
    no Chinese-style phrasing carried over.
  - `en` is plain and verb-first: "Open Book…", "Back to Library", not "Book Open".
- Tone rules from the product spec still apply in every language: quiet, declarative, no exclamation marks,
  no emoji, errors state what happened and what to do next, never assign blame.
- The book's own language is independent of UI language and must never be assumed to match.

## 3. Unchanged from the research spec

Everything else in `docs/CONTRACT.md` and `docs/research/product-spec.md` stands: JSON persistence with atomic
writes, blake2b-128 book identity, the gpos locator, paginated-by-default with instant turns, the four-pane dock,
live-apply settings, files referenced in place and never copied, and no cloud/accounts/format-conversion.
