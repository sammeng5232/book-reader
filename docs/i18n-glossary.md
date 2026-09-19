# EPUB Reader — i18n glossary

Canonical terms for every recurring concept, in the four UI languages. Translators of `i18n/zh_Hant.py` and
`i18n/ja.py` follow this file. `python strings.py --check` enforces part of it mechanically: script (no Simplified
glyphs in zh-Hant or ja, no Traditional glyphs in zh-Hans), the regional terms zh-Hant and ja must avoid, full-width
punctuation next to CJK text, no exclamation marks, no emoji, `…` rather than `...`, and identical `{placeholders}`.

Source of truth for zh-Hans copy: `docs/research/product-spec.md` §3, §3b and §5b. Language policy: `docs/DECISIONS.md` §2.

## 1. Core terms

A note after a pick explains any choice that is not obvious.

| Concept | 简体中文 (zh-Hans) | 繁體中文 (zh-Hant, Taiwan) | English (en) | 日本語 (ja) |
|---|---|---|---|---|
| book | 书 / 书籍 | 書 / 書籍 | book | 本 |
| library / shelf | 书架 | 書櫃 | Library | 本棚 |
| table of contents | 目录 | 目錄 | Contents | 目次 |
| bookmark | 书签 | 書籤 | Bookmark | しおり |
| highlight (noun and verb) | 高亮 | 畫線 | Highlight | ハイライト |
| annotation pane (批注) | 批注 | 畫線 | Highlights | ハイライト |
| note (text attached to a highlight) | 笔记 | 筆記 | Note | メモ |
| search | 搜索 | 搜尋 | Search | 検索 |
| settings | 设置 | 設定 | Settings | 設定 |
| theme | 主题 | 主題 | Theme | テーマ |
| font | 字体 | 字型 | Font | フォント |
| font size | 字号 | 字級 | Font size | 文字サイズ |
| line spacing | 行距 | 行距 | Line spacing | 行間 |
| margin | 页边距 | 邊界 | Margins | 余白 |
| paginated (mode) | 分页 | 分頁 | Paginated | ページめくり |
| scroll (mode) | 滚动 | 捲動 | Scrolling | スクロール |
| full screen | 全屏 | 全螢幕 | Full Screen | 全画面表示 |
| focus mode | 专注模式 | 專注模式 | Focus mode | 集中モード |
| reading progress | 阅读进度 | 閱讀進度 | Progress | 進捗 |
| time left | 剩余时间（本章剩余 / 全书剩余） | 剩餘時間（本章剩餘 / 全書剩餘） | time left | 残り時間 |
| chapter | 章 / 章节 | 章 / 章節 | Chapter (untitled spine item: section) | 章 |
| open | 打开 | 開啟 | Open | 開く |
| remove from library | 从书架移除 | 從書櫃移除 | Remove from Library | 本棚から外す |
| show in folder | 在文件夹中显示 | 在資料夾中顯示 | Show in Folder | ファイルの場所を開く |

Why the non-obvious picks:

- **書櫃** (zh-Hant library): Readmoo 讀墨 and 博客來, Taiwan's main e-book stores, call the reader's collection 書櫃. 書架 reads as mainland usage.
- **Library** (en): the user's own example "Back to Library" in DECISIONS.md. "Shelf" appears nowhere in English copy.
- **本棚** (ja library): honto, BookLive and BOOK☆WALKER use 本棚. It keeps the shelf image of 书架. Kindle's ライブラリ is a brand term.
- **しおり** (ja bookmark): DECISIONS.md requires it, and it is the native reading word. ブックマーク reads as a browser bookmark.
- **畫線** (zh-Hant highlight): the Readmoo term Taiwan readers know. 高亮 is mainland-only, and the checker rejects it.
- **Highlights / 畫線 / ハイライト for the 批注 pane**: every entry in that pane is a highlight, and a note is optional text on one. Naming the pane "Notes" would clash with the note inside each entry.
- **主題** (zh-Hant theme): Windows TW uses 佈景主題 for desktop themes. A reading theme is plainly 主題, and the label must fit a segmented control.
- **字級** (zh-Hant font size): the Taiwan reading-app term. Office's 字型大小 is too long for a slider label.
- **邊界** (zh-Hant margin): Word TW names page margins 邊界. 頁邊距 is mainland wording.
- **文字サイズ / 行間 / 余白** (ja): the labels Word JP and Kindle JP use.
- **ページめくり** (ja paginated): it reads naturally beside スクロール. ページ送り usually names a direction setting.
- **集中モード** (ja focus mode): Windows 11 JP's own name for its focus feature.
- **進捗** (ja progress): short enough for a list column and a sort menu. Use 読書の進捗 only in full sentences.
- **本棚から外す** (ja remove): 削除 would suggest the file itself is deleted. The app never deletes files, which is also why zh uses 移除 and never 删除 here.
- **ファイルの場所を開く** (ja show in folder): the label Windows JP uses for this exact action.
- **在資料夾中顯示** (zh-Hant): 資料夾 is the Taiwan term for folder. 文件夾 is mainland, and the checker rejects 文件.

## 2. Supporting terms

| Concept | 简体中文 | 繁體中文 | English | 日本語 |
|---|---|---|---|---|
| file | 文件 | 檔案 | file | ファイル |
| folder | 文件夹 | 資料夾 | folder | フォルダー |
| cover | 封面 | 封面 | cover | 表紙 |
| title | 书名 | 書名 | Title | タイトル |
| author | 作者 | 作者 | Author | 著者 |
| book details | 书籍信息 | 書籍資訊 | Book Details | 本の情報 |
| page / screen | 页 / 本屏 | 頁 / 本畫面 | page / screen | ページ / 画面 |
| selection | 所选文字 | 選取的文字 | selection | 選択したテキスト |
| copy | 复制 | 複製 | Copy | コピー |
| copy with citation | 复制并附出处 | 複製並附上出處 | Copy with Citation | 出典付きでコピー |
| export | 导出 | 匯出 | Export | エクスポート |
| delete | 删除 | 刪除 | Delete | 削除 |
| undo | 撤销 | 復原 | Undo | 元に戻す |
| exit (the app) | 退出 | 結束 | Exit | 終了 |
| close | 关闭 | 關閉 | Close | 閉じる |
| save | 保存 | 儲存 | Save | 保存 |
| default | 默认 | 預設 | default | 既定 |
| keyboard shortcuts | 快捷键 | 鍵盤快速鍵 | Keyboard Shortcuts | キーボードショートカット |
| interface language | 界面语言 | 介面語言 | Language | 表示言語 |
| follow system | 跟随系统 | 跟隨系統 | System default | システムに合わせる |
| themes 日 / 纸 / 夜 | 日 / 纸 / 夜 | 日 / 紙 / 夜 | Light / Sepia / Dark | ライト / セピア / ダーク |
| technical details | 技术细节 | 技術細節 | Technical Details | 技術的な詳細 |
| encryption | 加密 | 加密 | encryption | 暗号化 |
| locate (a moved file) | 重新定位 | 重新指定位置 | Locate | 場所を指定 |
| go to | 跳转 | 跳至 | Go to | 移動 |
| continue reading | 继续阅读 | 繼續閱讀 | Continue Reading | 続きを読む |
| all books | 全部书籍 | 所有書籍 | All Books | すべての本 |
| file missing | 文件缺失 | 找不到檔案 | File missing | ファイルが見つかりません |

Notes:

- **復原** (zh-Hant undo) and **結束** (zh-Hant exit): Windows TW menu wording. 撤銷 and 退出 are mainland.
- **既定** (ja default): Windows JP wording, as in 既定のアプリ. デフォルト is informal.
- **鍵盤快速鍵** (zh-Hant): Windows TW's name for keyboard shortcuts.
- **本畫面** (zh-Hant screen): mirrors 本屏. Pagination is not authoritative (spec pitfall 14), so never write 頁碼 or 第 N 頁.

## 3. Style rules per language

**All languages**

- Quiet, plain and declarative. No exclamation marks, no emoji, no marketing adjectives.
- Errors say what happened and what to do next. They never blame the reader.
- Keep every `{placeholder}` exactly as in the English table. `{app}` is filled in automatically, so never write the product name into copy.
- Product name `EPUB Reader`, language endonyms and shortcut labels (`Ctrl+F`, `F11`, arrow glyphs) are never translated.
- Use the single character `…` when a command opens a dialog or needs more input.
- Put a path on its own line, after a sentence ending in a colon. Never inline it with quotes.
- `.one` / `.other` keys: English uses both. Chinese and Japanese have no grammatical plural, so both values are identical.

**简体中文 / 繁體中文**

- Verb-first commands: 打开书籍 / 開啟書籍, not 书籍打开.
- Full-width punctuation (，。：；？（）) next to Chinese text. Book titles go in 《》 and quoted search text in 「」.
- Half-width numbers and units, with a space between them and Chinese text: `34%`, `12 分钟` / `12 分鐘`, `共 2371 处`.
- No cute particles (哦/呀/啦). Never use 请稍候 / 請稍候; the work is fast enough to just do.
- zh-Hant is not a character conversion of zh-Hans. Use the Taiwan terms above. The checker rejects
  文件 設置 搜索 字體 默認 信息 屏幕 全屏 視頻 鼠標 軟件 界面 菜單 導出 導入 撤銷 書簽 滾動 保存 退出 網絡 程序 文本 字符 高亮 打開 激活 登錄.

**English**

- Commands use Title Case and start with a verb: menu items, buttons, context-menu items, dialog titles and tab labels ("Open Book…", "Remove from Library").
- Everything else uses sentence case: setting labels, hints, status messages and card text.
- US spelling ("color"). Contractions are fine in titles ("Can't open this book").
- Short time units in the status bar ("3 h 20 min left in book"). Full plural words in dialogs ("2 hours 5 minutes").

**日本語**

- Standard Japanese UI verbs: 開く, 閉じる, 設定, 検索, 表示, 全画面表示, 終了.
- Katakana for loanwords (フォント, テーマ, ハイライト, メモ, スクロール). Follow Microsoft's long-vowel style: フォルダー, ユーザー.
- Buttons and menu items use plain forms (開く, 本棚から外す). Sentences use です/ます (見つかりません, 保存されました).
- Put quoted text in 「」 and book titles in 『』. Use full-width punctuation (、。：？).
- No Chinese phrasing, and no Simplified-only glyphs (the checker rejects 书 设 页 录 签 读 图 etc.).
  The checker also rejects 書簽/書籤 (use しおり), 搜索 (検索), 全屏 (全画面表示), 目錄 (目次), 設置 (設定), 退出 (終了),
  文件 (ファイル), 打開 (開く), 書架 (本棚), 高亮 (ハイライト) and 字體 (フォント).
- No space between numbers and Japanese units, following Microsoft JP style: `12分`, `3時間20分`, `2371件`, `34%`.
  Then `time.hours_minutes` is `{hours}{minutes}`. Dates: `9月16日`, `2026年9月16日` (month keys are `1月`…`12月`).

## 4. Translator workflow

1. Replace each value in `i18n/zh_Hant.py` or `i18n/ja.py` in place, removing the `[zh-Hant] ` / `[ja] ` marker. Keep the keys and their order.
2. Run `python strings.py --check` after every batch. It must print `OK`.
3. When done, run `python strings.py --check --strict-translations`. It fails while any stub marker remains.
4. Use `python strings.py --show <key>` to see one key in all four languages.
