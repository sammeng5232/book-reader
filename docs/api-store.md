# `store.py` — API (owner B)

Persistence for Book Reader: human-readable JSON under `%APPDATA%\Book Reader\`, atomic writes, `.bak`
generations, corrupt-file quarantine, integer schema migrations, one serialized writer thread.
Stdlib only; importing it has no side effects. Tests: `tests/test_store.py` (38 tests).

```
%APPDATA%\Book Reader\settings.json           debounced 500 ms, plus on quit
%APPDATA%\Book Reader\library.json            250 ms after add/remove, 60 s for progress churn
%APPDATA%\Book Reader\books\<id>.json(.bak)   position 2000 ms; bookmarks/highlights IMMEDIATELY
%APPDATA%\Book Reader\logs\book-reader.log    written by owner G
%LOCALAPPDATA%\Book Reader\cache\covers\<id>.jpg
```

## Identity constants — import these, never retype them

| Symbol | Value |
|---|---|
| `APP_DIR_NAME` | `"Book Reader"` — folder name under %APPDATA%/%LOCALAPPDATA%, and the Qt org/app name |
| `PROG_ID` | `"BookReader.Book.1"` |
| `PIPE_NAME` | `"book-reader-single-instance"` |
| `LOG_FILE_NAME` | `"book-reader.log"` |
| `LEGACY_DIR_NAME` | the retired development name. Used only to find a dev build's folder to migrate. |

```python
from store import APP_DIR_NAME, PIPE_NAME
app.setOrganizationName(APP_DIR_NAME); app.setApplicationName(APP_DIR_NAME)
server.listen(PIPE_NAME)
```

## Value vocabularies

| Symbol | Meaning |
|---|---|
| `UI_LANGUAGES` | `("auto", "zh-Hans", "zh-Hant", "en", "ja")`, the accepted `ui.language` values |
| `THEME_CHOICES` | `("light", "sepia", "dark", "system")`, the accepted `reader.theme` values (theme.py re-exports) |
| `LEGACY_THEME_ALIASES` | `{"day": "light", "paper": "sepia", "night": "dark", "auto": "system", "follow": "system"}` |
| `normalize_language(value) -> str` | `'zh'` becomes `'zh-Hans'`; `'zh_TW'`/`'zh-HK'` become `'zh-Hant'`; `'ja-JP'` becomes `'ja'`; anything unknown becomes `'auto'` |
| `normalize_theme_choice(value) -> str` | legacy names map to the new ones; unknown becomes `'system'` |

```python
normalize_language("zh")        # 'zh-Hans'
normalize_theme_choice("night") # 'dark'
```

`Store.set()` normalizes these two keys on the way in, and the loader rewrites legacy values it finds in
an existing settings.json (with one write-back, so `.bak` keeps the untouched original).

## Locations

All computed from `os.environ` (`APPDATA` / `LOCALAPPDATA`), **never** `QStandardPaths`.
`AppConfigLocation` is Local on Windows.

| Function | Returns |
|---|---|
| `app_dir() -> str` | `%APPDATA%\Book Reader` |
| `cache_dir() -> str` | `%LOCALAPPDATA%\Book Reader\cache` |
| `cover_dir(cache_root=None) -> str` | `<cache>\covers` |
| `books_dir(root=None) -> str` | `<root>\books` |
| `log_dir(root=None) -> str` | `<root>\logs` |
| `log_file(root=None) -> str` | `<root>\logs\book-reader.log` |

```python
handler = RotatingFileHandler(log_file(), maxBytes=1 << 20, backupCount=3, encoding="utf-8")
```

### `migrate_legacy_dirs(appdata=None, localappdata=None) -> list[str]`

This is the one-time move required by DECISIONS.md §1. If `<APPDATA>\<LEGACY_DIR_NAME>` exists and
`<APPDATA>\Book Reader` does **not**, it moves the Local cache first and then the state root (the rename is
the commit point). It then renames `logs\<legacy>.log*` to `book-reader.log*`. Once the new root exists it
returns `[]` without looking at the legacy location again. If the rename fails because a file is locked, it
copies the tree via a `.migrating` staging dir and leaves the original in place. It never raises and never
deletes anything. It returns notes for the log.

`Store()` with the default root calls it for you. Owner G may call it earlier, because it is idempotent.

```python
notes = migrate_legacy_dirs()          # [] on every launch after the first
for n in notes: log.info("migration: %s", n)
```

## Primitives

| Symbol | Notes |
|---|---|
| `SCHEMA` | `1`, the on-disk format version stamped into every file |
| `now_iso() -> str` | `2026-09-16T14:02:11+08:00` |
| `new_id(prefix) -> str` | time-sortable id, e.g. `h_01a0a8cd0efd41c2` |
| `book_id(path) -> str` | blake2b-128 hex of the whole file (32 chars). Uncached; prefer `Store.book_id_for` |
| `write_json(path, obj, *, lock=None)` | serialize under `lock`, then copy current to `.bak`, `mkstemp` in the same dir, write, fsync, `os.replace` |
| `read_json(path, default_factory) -> (obj, tag)` | primary, then `.bak`, then quarantine `<name>.corrupt-<epoch>[-n]` |
| `MIGRATIONS` | `{from_version: fn(obj) -> obj}`; ships `0: identity` (a file with no `schema` key) |

`read_json` tags: `primary`, `backup`, `new`, `reset:CORRUPT_QUARANTINED`, `<tag>:MIGRATED(a->b)`,
`<tag>:READ_ONLY_FUTURE_SCHEMA(v)`, `<tag>:UNMIGRATABLE(v)`.

```python
obj, tag = read_json(path, default_library)
if tag.startswith("reset"): log.warning("library.json was unreadable; kept as .corrupt-*")
```

## Defaults

`DEFAULT_SETTINGS` (read-only by convention), `default_settings()`, `default_library()`,
`default_book_state(book_id, title="")`, all fresh deep copies. The settings shape is the product-spec
literal schema, except `ui.language` now defaults to `"auto"` and `reader.theme` to `"system"`. The `themes`
and `highlight_colors` blocks are gone: colours live only in `theme.py`.

```python
fresh = default_settings(); fresh["reader"]["font_size_px"]   # 21
```

Policy constants: `SETTINGS_DEBOUNCE_MS = 500`, `POSITION_DEBOUNCE_MS = 2000`, `LIBRARY_DEBOUNCE_MS = 250`,
`LIBRARY_IDLE_MS = 60000`.

## `class Store`

```python
Store(root: str | None = None, *, cache_root: str | None = None, start_writer: bool = True)
```

* `root=None` uses `%APPDATA%\Book Reader` and runs `migrate_legacy_dirs()` first. Its cache is
  `%LOCALAPPDATA%\Book Reader\cache`.
* An explicit `root` makes the cache default to `<root>\cache`, so a test store never touches the real
  profile. **Every test must pass a temp root.**
* Thread-safe. One daemon writer thread coalesces writes per path. An `atexit` safety net flushes if
  `close()` was skipped.

Attributes: `root`, `cache_root`, `books_dir`, `log_dir`, `settings_path`, `library_path`,
`settings` (see below), `load_notes: list[str]` (what happened during load, for the log),
`migration_notes: list[str]`.

```python
store = Store()
for note in store.load_notes: log.warning(note)
```

### Settings

| Method | Notes |
|---|---|
| `settings` | `Settings` view: `store.settings.reader.font_size_px = 23` (debounced 500 ms) |
| `get(dotted, default=None)` | never raises |
| `set(dotted, value)` | no-op when unchanged; normalizes `ui.language` / `reader.theme` |
| `update(mapping)` | several dotted keys, one coalesced write |
| `settings_dict()` | the live dict (read-only by convention) |
| `reset_settings()` | shipped defaults, keeping `window` and `ui.language` |

```python
store.set("ui.language", "zh-Hant")
lang = store.settings.ui.language              # 'zh-Hant'
reader = store.settings.reader.to_dict()       # deep copy
```

`Settings` also supports `s["reader.theme"]`, `"reader.theme" in s`, `s.get(dotted, default)`,
`s.keys()`, `s.to_dict()`. A missing attribute raises `AttributeError`, and a missing item raises `KeyError`.

### Library (`library.json`)

| Method | Notes |
|---|---|
| `library() -> list[dict]` | list copy of live entries |
| `library_get(book_id) -> dict \| None` | |
| `library_upsert(entry, *, immediate=False)` | merges; a new entry gets `hash_algo`, `path_history`, `added_at`, `progress`... `immediate=True` blocks until on disk |
| `library_update(book_id, **fields)` | progress/opened_at churn, 60 s debounce |
| `library_remove(book_id)` | shelf only. Never touches the .epub, and **keeps** `books/<id>.json` so re-adding restores notes |

```python
bid = store.book_id_for(path)
store.library_upsert({"id": bid, "path": path, "size": st.st_size, "mtime_ns": st.st_mtime_ns,
                      "title": book.metadata["title"]}, immediate=True)
```

### Per-book state (`books/<id>.json`)

| Method | Write policy |
|---|---|
| `book_state(book_id) -> dict` | loads (with .bak recovery) and caches; returns the **live** dict |
| `save_book_state(book_id, state, *, immediate=False)` | `False`: 2000 ms bounded debounce. `True`: blocks until this file is on disk |
| `save_position(book_id, position, *, force=False)` | debounced; `force=True` on chapter change, book close, window deactivate, quit |
| `add_bookmark(book_id, bm) -> dict` | immediate; fills `id`, `created_at`; newest first |
| `remove_bookmark(book_id, bookmark_id) -> bool` | immediate |
| `add_highlight(book_id, hl) -> dict` | immediate; fills `id`, `created_at`, `updated_at`, `anchor_state='exact'` |
| `update_highlight(book_id, highlight_id, **fields) -> bool` | immediate; bumps `updated_at` |
| `remove_highlight(book_id, highlight_id) -> bool` | immediate. Only for a user-requested delete; a lost anchor sets `anchor_state='lost'` instead |
| `reader_settings(book_id \| None) -> dict` | deep copy of global `reader` merged with the book's `overrides` |
| `set_override(book_id, key, value)` / `clear_overrides(book_id)` | the 仅用于本书 checkbox, immediate |
| `book_state_path(book_id) -> str` | |

```python
hl = store.add_highlight(bid, {"start": loc_a, "end": loc_b, "text": text, "color": "yellow", "note": ""})
# the file already contains hl["id"] here; a crash now cannot lose it
store.save_position(bid, {"spine_index": 7, "locator": loc})           # every page turn: cheap (0.02 ms)
settings_for_js = store.reader_settings(bid)
```

"Bounded debounce" means repeated saves of one file keep the **earliest** deadline. A reader who scrolls
continuously is still saved every 2 s (verified first write at about 2.0 s under saves every 50 ms).

If you mutate the dict from `book_state()` in place, do it on one thread or under `store.lock`. On Python
3.14 the C JSON encoder cannot interleave with a mutator (verified 0 errors), and a
`dictionary changed size` race is retried anyway.

### Identity and covers

| Method | Notes |
|---|---|
| `book_id_for(path) -> str` | blake2b-128, cached on `(normcase(abspath), size, mtime_ns)` in memory **and** seeded from library.json entries carrying `path`/`size`/`mtime_ns`, so a restart does not rehash |
| `cover_path(book_id) -> str` | `<cache_root>\covers\<id>.jpg`; directory created |

```python
thumb.save(store.cover_path(bid), "JPEG", quality=86)
```

### Read-only (future schema) and diagnostics

| Member | Notes |
|---|---|
| `read_only` | True when **any** loaded file came from a newer build (show "please update") |
| `read_only_reason` | e.g. `settings.json: primary:READ_ONLY_FUTURE_SCHEMA(99)` |
| `is_read_only(path)` / `is_book_read_only(book_id)` | writes to those paths are refused; every other file keeps saving |
| `lock` | the `RLock` the writer serializes under |
| `stats` | `{requests, writes, pending, errors, read_only, read_only_reason, load_notes}` |

```python
if store.read_only:
    log.warning("read-only: %s", store.read_only_reason)   # and show an update notice (copy from strings.py)
```

### Lifecycle

| Method | Notes |
|---|---|
| `flush(timeout=15.0) -> bool` | blocks until every queued write is on disk, ignoring debounce windows. False on timeout or error. **Call on quit.** |
| `close()` | flush and stop the writer; later saves are ignored |
| context manager | `with Store(tmp) as st: ...` |

```python
app.aboutToQuit.connect(store.close)
```

## Recovery behaviour (all verified by `tests/test_store.py`)

| Situation | Result |
|---|---|
| Clean roundtrip | identical data, CJK + Ext-B astral characters, raw UTF-8 on disk (no `\u` escapes), LF newlines, no `.tmp_` leftovers |
| Second write | `.bak` holds the previous generation |
| Truncated primary | loads the `.bak`, keeps the wreckage as `.corrupt-<epoch>`, rewrites a good primary (`backup+healed`) |
| Primary and `.bak` both garbage | primary renamed `.corrupt-<epoch>[-n]`, defaults used, nothing deleted |
| No `schema` key (v0) | migrated to v1, legacy values normalized, written back once; `.bak` = original |
| Unknown older schema | defaults used; original copied to `.unmigratable-<epoch>` first |
| `schema` > 1 | that file is read-only; no write, no normalization, other files keep saving |
| Process killed (`TerminateProcess`) mid-write, 12 times | always `primary` or `backup`, never reset |
| Transient `PermissionError` on replace (AV/sync) | retried every 250 ms, up to 20 attempts |
| Stale `.tmp_*.json` older than 10 min | swept on start |
