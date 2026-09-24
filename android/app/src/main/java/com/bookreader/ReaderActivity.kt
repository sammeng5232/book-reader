package com.bookreader

import android.annotation.SuppressLint
import android.content.Context
import android.content.Intent
import android.graphics.Color
import android.os.Bundle
import android.view.Gravity
import android.view.View
import android.webkit.WebView
import android.widget.ImageButton
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.ArrayAdapter
import android.widget.ScrollView
import android.widget.SeekBar
import android.widget.TextView
import android.widget.Button
import android.widget.EditText
import android.widget.CheckBox
import android.widget.ListView
import android.text.Editable
import android.text.TextWatcher
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.drawerlayout.widget.DrawerLayout
import org.json.JSONArray
import org.json.JSONObject

/**
 * The Android reading surface: a WebView running the desktop's reader.js engine.
 *
 * All controls live in a bottom sheet (tap the centre of the page to show/hide it):
 * a back button and the table of contents, a light/dark toggle, background swatches,
 * a font-size slider, and a font picker for Latin, Chinese and Japanese text.  A
 * slim status bar under it shows the reading position.
 *
 * This is §4.4 of the handoff: pagination/scroll, themes, fonts, font size, TOC and
 * position save/restore.  Search, highlights and notes are not here yet.
 */
class ReaderActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private lateinit var host: BookHost
    private lateinit var drawer: DrawerLayout
    private lateinit var tocList: LinearLayout
    private lateinit var status: TextView
    private lateinit var progress: ProgressBar
    private lateinit var sheet: LinearLayout          // the bottom control sheet
    private lateinit var statusBar: LinearLayout

    private var spine: JSONArray = JSONArray()
    private var totalUnits = 1
    private var currentSpine = 0
    private var pageCount = 1
    private var page = 0
    private var scrollPercent = 0.0

    // ---- reading settings -------------------------------------------------
    private var dark = false
    private var bgKey = "white"                        // white | sepia | green | grey
    private var fontSizePx = 21
    private val fontSelections = mutableMapOf<String, String>()
    private val fontLibrary by lazy { FontLibrary(this) }
    private var scrollMode = true                      // scroll is the phone default
    private var startSpine = 0
    private var initialized = false
    private var pendingLocator: JSONObject? = null

    private var formulaScales = JSONObject()
    private var fixedLayout = false
    private var openAtEnd = false
    private val prefs by lazy { getSharedPreferences("epub_reader_settings", Context.MODE_PRIVATE) }
    private val saveHandler = android.os.Handler(android.os.Looper.getMainLooper())
    private var pendingPosition: Pair<String, Double>? = null
    private val savePosition = Runnable { flushPosition() }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val path = intent.getStringExtra(EXTRA_PATH) ?: run { finish(); return }
        startSpine = intent.getIntExtra("spine", 0)     // autorun/debug
        loadSettings()
        buildUi()
        onBackPressedDispatcher.addCallback(this, object : androidx.activity.OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                when {
                    drawer.isDrawerOpen(Gravity.START) -> drawer.closeDrawer(Gravity.START)
                    sheet.visibility == View.VISIBLE -> sheet.visibility = View.GONE
                    else -> finish()
                }
            }
        })
        host = BookHost(this, webView)
        host.listener = HostEvents()
        host.install()

        Thread {
            val info = host.open(path)
            runOnUiThread { onBookOpened(info) }
        }.start()
    }

    // ======================================================================
    // UI
    // ======================================================================
    @SuppressLint("SetJavaScriptEnabled")
    private fun buildUi() {
        drawer = DrawerLayout(this)
        val content = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }

        webView = WebView(this)
        // WebView must receive the complete touch sequence, especially UP: it
        // computes fling velocity there. reader.js handles taps/edge gestures.

        // ---- the status line (always visible) ----
        statusBar = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(16, 2, 16, 6)
        }
        progress = ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal)
        status = TextView(this).apply { textSize = 12f; gravity = Gravity.END }
        statusBar.addView(progress, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        statusBar.addView(status, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))

        content.addView(webView, LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f))
        content.addView(statusBar)

        sheet = buildSheet()
        content.addView(sheet)
        drawer.addView(content)

        // ---- TOC drawer ----
        tocList = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(24, 24, 24, 24)
            setBackgroundColor(Color.WHITE)
        }
        val tocScroll = ScrollView(this).apply { addView(tocList) }
        drawer.addView(tocScroll, DrawerLayout.LayoutParams(
            (resources.displayMetrics.widthPixels * 0.8).toInt(),
            DrawerLayout.LayoutParams.MATCH_PARENT, Gravity.START))

        setContentView(drawer)
    }

    /** The bottom sheet: nav row, then the settings rows. */
    private fun buildSheet(): LinearLayout {
        val sheetBg = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(20, 12, 20, 20)
            visibility = View.GONE
        }

        // -- row 1: back, contents, scroll/pages, light/dark --
        val nav = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }
        fun navBtn(icon: Int, desc: String, onClick: () -> Unit) = ImageButton(this).apply {
            setImageResource(icon)
            contentDescription = desc
            setOnClickListener { onClick() }
        }
        nav.addView(navBtn(android.R.drawable.ic_media_rew, "Back") { finish() })
        nav.addView(navBtn(android.R.drawable.ic_menu_sort_by_size, "Contents") { drawer.openDrawer(Gravity.START) })
        nav.addView(View(this), LinearLayout.LayoutParams(0, 1, 1f))
        nav.addView(navBtn(android.R.drawable.ic_menu_agenda, "Scroll or pages") { toggleMode() })
        nav.addView(navBtn(android.R.drawable.ic_menu_day, "Light or dark") { toggleDark() })
        sheetBg.addView(nav)

        sheetBg.addView(separator())

        // -- row 2: background swatches --
        sheetBg.addView(label("Background"))
        val swatches = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        for (bg in BACKGROUNDS) {
            val key = bg.first
            val hex = bg.second
            val swatch = View(this).apply {
                setBackgroundColor(Color.parseColor(hex))
                val s = (44 * resources.displayMetrics.density).toInt()
                layoutParams = LinearLayout.LayoutParams(s, s).apply {
                    marginEnd = (12 * resources.displayMetrics.density).toInt()
                }
                setOnClickListener {
                    bgKey = key
                    applySettings()
                }
            }
            swatches.addView(swatch)
        }
        sheetBg.addView(swatches)

        sheetBg.addView(separator())

        // -- row 3: font size slider --
        sheetBg.addView(label("Font size"))
        val sizeRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }
        val sizeLabel = TextView(this).apply {
            text = "$fontSizePx"
            textSize = 13f
            setPadding(0, 0, 16, 0)
        }
        val slider = SeekBar(this).apply {
            max = 40 - 12                                // 12..40 px
            progress = fontSizePx - 12
            setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
                override fun onProgressChanged(sb: SeekBar?, p: Int, fromUser: Boolean) {
                    fontSizePx = p + 12
                    sizeLabel.text = "$fontSizePx"
                }
                override fun onStartTrackingTouch(sb: SeekBar?) {}
                override fun onStopTrackingTouch(sb: SeekBar?) { applySettings() }
            })
        }
        sizeRow.addView(slider, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        sizeRow.addView(sizeLabel)
        sheetBg.addView(sizeRow)

        sheetBg.addView(separator())

        // Language-specific selections are mapped by the reading engine, not
        // concatenated after a generic family which would mask CJK choices.
        sheetBg.addView(fontPicker())

        return sheetBg
    }

    private fun label(text: String) = TextView(this).apply {
        this.text = text
        textSize = 12f
        setPadding(0, 10, 0, 6)
        alpha = 0.7f
    }

    private fun separator() = View(this).apply {
        setBackgroundColor(0x22000000)
        layoutParams = LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 1).apply {
            topMargin = 8; bottomMargin = 8
        }
    }

    private fun fontPicker(): View = Button(this).apply {
        text = "字体设置 / Fonts by language"
        setOnClickListener { showFontSettings() }
    }

    private fun showFontSettings() {
        fontLibrary.reload()
        val content = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            val pad = (20 * resources.displayMetrics.density).toInt()
            setPadding(pad, pad / 2, pad, pad / 2)
        }
        val count = fontLibrary.all().map { it.family }.distinct().size
        content.addView(label("$count 种字体 · 按语言独立设置，改动立即保存"))
        for ((script, title) in FONT_LANGUAGES) {
            val button = Button(this)
            fun update() { button.text = "$title：${fontLibrary.displayName(fontSelections[script] ?: "")}" }
            update()
            button.setOnClickListener {
                showFontChoices(script, title) { family ->
                    fontSelections[script] = family
                    applySettings()
                    update()
                }
            }
            content.addView(button, LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT))
        }
        content.addView(label("自动选项跟随西文或书籍语言。列表优先显示支持当前语言的字体；可勾选“全部字体”。"))
        AlertDialog.Builder(this).setTitle("字体设置 / Fonts by language")
            .setView(ScrollView(this).apply { addView(content) })
            .setPositiveButton("完成", null).show()
    }

    private fun showFontChoices(script: String, title: String, onPick: (String) -> Unit) {
        val content = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            val pad = (18 * resources.displayMetrics.density).toInt()
            setPadding(pad, 0, pad, 0)
        }
        val query = EditText(this).apply {
            hint = "搜索字体名称、字族或来源"
            isSingleLine = true
        }
        val showAll = CheckBox(this).apply { text = "全部字体（包括不完整支持此语言的字体）" }
        val list = ListView(this).apply { choiceMode = ListView.CHOICE_MODE_SINGLE }
        val empty = TextView(this).apply {
            text = "没有匹配的字体"; gravity = Gravity.CENTER; setPadding(0, 20, 0, 20)
        }
        var choices: List<FontLibrary.Entry> = emptyList()
        val adapter = ArrayAdapter<String>(this, android.R.layout.simple_list_item_single_choice)
        list.adapter = adapter
        fun refresh() {
            val text = query.text.toString().trim()
            val automatic = FontLibrary.Entry("auto", "自动 / Auto", "", source="跟随西文或书籍语言")
            choices = (listOf(automatic) + fontLibrary.families(script, showAll.isChecked)).filter {
                text.isEmpty() || (it.name + " " + it.family + " " + it.source + " " + it.aliases.joinToString(" "))
                    .contains(text, ignoreCase = true)
            }
            adapter.clear()
            adapter.addAll(choices.map {
                val selected = if (it.family == (fontSelections[script] ?: "")) "  ✓" else ""
                "${it.name}$selected\n${it.source}"
            })
            adapter.notifyDataSetChanged()
            val index = choices.indexOfFirst { it.family == (fontSelections[script] ?: "") }
            if (index >= 0) list.setItemChecked(index, true)
            empty.visibility = if (choices.isEmpty()) View.VISIBLE else View.GONE
        }
        query.addTextChangedListener(object : TextWatcher {
            override fun beforeTextChanged(s: CharSequence?, start: Int, count: Int, after: Int) {}
            override fun onTextChanged(s: CharSequence?, start: Int, before: Int, count: Int) { refresh() }
            override fun afterTextChanged(s: Editable?) {}
        })
        showAll.setOnCheckedChangeListener { _, _ -> refresh() }
        content.addView(query)
        content.addView(showAll)
        content.addView(empty)
        content.addView(list, LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT,
            (resources.displayMetrics.heightPixels * .50).toInt()))
        val dialog = AlertDialog.Builder(this).setTitle(title).setView(content)
            .setNegativeButton("取消", null).create()
        list.setOnItemClickListener { _, _, position, _ ->
            choices.getOrNull(position)?.let { onPick(it.family); dialog.dismiss() }
        }
        refresh()
        dialog.show()
    }

    private fun toggleChrome() {
        val show = sheet.visibility != View.VISIBLE
        sheet.visibility = if (show) View.VISIBLE else View.GONE
    }

    // ======================================================================
    // Book lifecycle
    // ======================================================================
    private fun onBookOpened(info: JSONObject) {
        if (!info.optBoolean("ok")) {
            status.text = "Cannot open: ${info.optString("error")}"
            return
        }
        spine = info.optJSONArray("spine") ?: JSONArray()
        totalUnits = maxOf(1, info.optInt("totalUnits", 1))
        title = info.optString("title")
        pendingLocator = info.optJSONObject("position")?.optJSONObject("locator")
        formulaScales = info.optJSONObject("formulaScales") ?: JSONObject()
        fixedLayout = info.optBoolean("fixedLayout")
        buildToc(info.optJSONArray("toc") ?: JSONArray())
        applyChromeTheme()
        val savedSpine = pendingLocator?.optString("zip")?.let { findSpine(it) } ?: -1
        navigate(if (!intent.hasExtra("spine") && savedSpine >= 0) savedSpine else startSpine)
    }

    private fun buildToc(entries: JSONArray) {
        tocList.removeAllViews()
        fun addEntries(arr: JSONArray, depth: Int) {
            for (i in 0 until arr.length()) {
                val e = arr.optJSONObject(i) ?: continue
                val spineIdx = if (e.isNull("spine")) -1 else e.optInt("spine")
                val tv = TextView(this).apply {
                    text = e.optString("title").ifEmpty { "—" }
                    textSize = if (depth == 0) 16f else 14f
                    setPadding(12 + depth * 28, 14, 12, 14)
                    if (spineIdx >= 0) {
                        setOnClickListener {
                            drawer.closeDrawers()
                            navigate(spineIdx, e.optString("fragment"))
                        }
                    } else {
                        alpha = 0.6f
                    }
                }
                tocList.addView(tv)
                addEntries(e.optJSONArray("children") ?: JSONArray(), depth + 1)
            }
        }
        addEntries(entries, 0)
    }

    private fun navigate(spineIndex: Int, fragment: String = "") {
        if (spineIndex < 0 || spineIndex >= spine.length()) return
        currentSpine = spineIndex
        initialized = false
        host.navigate(spine.getJSONObject(spineIndex).getString("zip"), fragment)
    }

    // ======================================================================
    // Host events
    // ======================================================================
    private inner class HostEvents : BookHost.Listener {
        override fun onPageLoaded(url: String) {
            if (initialized) return
            val cfg = JSONObject()
                .put("settings", settingsJson())
                .put("mobileHost", true)
                .put("formulaScales", formulaScales)
                .put("fontFaces", fontLibrary.fontFaces())
                .put("fixedLayout", fixedLayout)
                .put("mode", if (scrollMode) "scroll" else "paginated")
                .put("book", JSONObject()
                    .put("offset", spine.getJSONObject(currentSpine).optInt("offset"))
                    .put("total", totalUnits))
            val loc = pendingLocator
            pendingLocator = null
            if (loc != null) cfg.put("locator", loc)
            host.callReader("init", cfg) { result ->
                val st = result as? JSONObject ?: return@callReader
                initialized = true
                page = st.optInt("page")
                pageCount = maxOf(1, st.optInt("pages"))
                if (openAtEnd) { openAtEnd = false; host.callReader("gotoPercent", 1.0) }
                updateStatus()
            }
        }

        override fun onReady(state: JSONObject) {}

        override fun onPosition(state: JSONObject) {
            page = state.optInt("page")
            pageCount = maxOf(1, state.optInt("pages"))
            // Shared JS reports character positions, while Android's spine
            // weights are estimated units. Combine chapter progress with those
            // weights here instead of mixing the two units (which showed 100%).
            val item = spine.optJSONObject(currentSpine)
            val off = item?.optInt("offset", 0) ?: 0
            val units = item?.optInt("units", 1) ?: 1
            scrollPercent = ((off + state.optDouble("chapterPercent", 0.0).coerceIn(0.0, 1.0) * units) /
                totalUnits.coerceAtLeast(1)).coerceIn(0.0, 1.0)
            updateStatus()
            val zip = host.currentZip
            host.callReader("capture") { loc ->
                if (loc is JSONObject && zip == host.currentZip) {
                    pendingPosition = loc.put("zip", zip).toString() to scrollPercent
                    saveHandler.removeCallbacks(savePosition)
                    saveHandler.postDelayed(savePosition, 350)
                }
            }
        }

        override fun onLink(zip: String, fragment: String, url: String?) {
            if (url != null) return
            val idx = findSpine(zip)
            if (idx >= 0) navigate(idx, fragment)
        }

        override fun onLog(message: String) {
            android.util.Log.i("BookReader", "page: $message")
        }

        override fun onReaderCommand(command: String) {
            if (!initialized) return
            when (command) {
                "EpubReader.TapCentre" -> toggleChrome()
                "EpubReader.NextChapter" -> if (currentSpine + 1 < spine.length()) {
                    openAtEnd = false; navigate(currentSpine + 1)
                }
                "EpubReader.PrevChapter" -> if (currentSpine > 0) {
                    openAtEnd = true; navigate(currentSpine - 1)
                }
            }
        }
    }

    private fun findSpine(zipName: String): Int {
        for (i in 0 until spine.length()) {
            if (spine.getJSONObject(i).getString("zip") == zipName) return i
        }
        return -1
    }

    private fun updateStatus() {
        val percent = if (scrollMode) (scrollPercent * 100).toInt() else if (totalUnits > 1) {
            val off = spine.getJSONObject(currentSpine).optInt("offset")
            val units = spine.getJSONObject(currentSpine).optInt("units", 1)
            val frac = if (pageCount > 0) page.toDouble() / pageCount else 0.0
            ((off + frac * units) / totalUnits * 100).toInt()
        } else 0
        progress.progress = percent
        status.text = if (scrollMode) "${currentSpine + 1} / ${spine.length()} · $percent%"
            else "${page + 1} / $pageCount · $percent%"
    }

    // ======================================================================
    // Settings -> reader.js
    // ======================================================================
    private fun palette(): JSONObject {
        val bg = BACKGROUNDS.first { it.first == bgKey }.second
        val o = JSONObject()
        if (dark) {
            o.put("bg", "#16181C").put("fg", "#C9CCD1").put("secondary", "#8A9099")
                .put("accent", "#7FB4F5").put("border", "#2A2E36").put("selection", "#2E4763")
        } else {
            o.put("bg", bg)
                .put("fg", FG[bgKey] ?: "#1A1A1A")
                .put("secondary", SECONDARY[bgKey] ?: "#636363")
                .put("accent", ACCENT[bgKey] ?: "#1155CC")
                .put("border", BORDER[bgKey] ?: "#E0E0E0")
                .put("selection", SELECTION[bgKey] ?: "#B4D5FE")
        }
        return o
    }

    private fun settingsJson(): JSONObject {
        val colors = palette()
        // Line height tracks the font size so large text doesn't feel double-spaced:
        // it eases from 2.0 at small sizes toward 1.5 at the largest.
        val lineHeight = 2.1 - (fontSizePx - 12) * (0.6 / 28.0)   // 12px→2.1, 40px→1.5
        return JSONObject()
            .put("theme", if (dark) "night" else "day")
            .put("colors", colors)
            .put("layout", if (scrollMode) "scroll" else "paged")
            .put("font_size_px", fontSizePx)
            .put("line_height", Math.round(lineHeight * 100.0) / 100.0)
            .put("page_margin_px", 48)
            .put("font_latin", fontSelections["latin"].orEmpty().ifBlank { "serif" })
            .put("font_cjk", fontSelections["hans"].orEmpty().ifBlank { fontSelections["latin"].orEmpty().ifBlank { "serif" } })
            .put("font_hans", fontSelections["hans"].orEmpty())
            .put("font_hant", fontSelections["hant"].orEmpty())
            .put("font_japanese", fontSelections["japanese"].orEmpty())
            .put("font_korean", fontSelections["korean"].orEmpty())
            .put("font_faces", fontLibrary.fontFaces())
            .put("image_click_zoom", true)
            .put("invert_images_in_dark", dark)
    }

    private fun applySettings() {
        persistSettings()
        applyChromeTheme()
        if (initialized) host.callReader("applySettings", settingsJson())
    }

    private fun toggleDark() {
        dark = !dark
        applySettings()
    }

    private fun toggleMode() {
        scrollMode = !scrollMode
        applySettings()
    }

    private fun loadSettings() {
        dark = prefs.getBoolean("dark", false)
        bgKey = prefs.getString("background", "white")?.takeIf { k -> BACKGROUNDS.any { it.first == k } } ?: "white"
        fontSizePx = prefs.getInt("font_size", 21).coerceIn(12, 40)
        val old = if (prefs.contains("font_family")) prefs.getString("font_family", "").orEmpty() else ""
        // Keep the previous single-font appearance until each language is edited.
        for ((script, _) in FONT_LANGUAGES) {
            fontSelections[script] = prefs.getString("font_$script", old) ?: old
        }
        scrollMode = prefs.getBoolean("scroll", true)
    }

    private fun persistSettings() {
        val edit = prefs.edit().putBoolean("dark", dark).putString("background", bgKey)
            .putInt("font_size", fontSizePx).putBoolean("scroll", scrollMode).putInt("font_settings_version", 2)
        for ((script, family) in fontSelections) edit.putString("font_$script", family)
        edit.apply()
    }

    private fun flushPosition() {
        saveHandler.removeCallbacks(savePosition)
        pendingPosition?.let { (locator, percent) -> host.savePosition(locator, percent) }
        pendingPosition = null
    }

    private fun applyChromeTheme() {
        val bgHex = if (dark) "#16181C" else BACKGROUNDS.first { it.first == bgKey }.second
        val fgHex = if (dark) "#C9CCD1" else (FG[bgKey] ?: "#1A1A1A")
        val bg = Color.parseColor(bgHex)
        val fg = Color.parseColor(fgHex)
        drawer.setBackgroundColor(bg)
        tocList.setBackgroundColor(bg)
        status.setTextColor(fg)
        webView.setBackgroundColor(bg)
    }

    override fun onDestroy() {
        flushPosition()
        host.close()
        webView.destroy()
        super.onDestroy()
    }

    companion object {
        const val EXTRA_PATH = "path"

        fun open(context: Context, path: String) {
            context.startActivity(Intent(context, ReaderActivity::class.java).putExtra(EXTRA_PATH, path))
        }

        // Backgrounds the page can take: white, sepia (paper), soft green, warm grey.
        private val BACKGROUNDS = listOf(
            "white" to "#FFFFFF",
            "sepia" to "#F6F0E4",
            "green" to "#E8F0E3",
            "grey" to "#EBEBEB",
        )
        private val FG = mapOf("white" to "#1A1A1A", "sepia" to "#33302B", "green" to "#2B332B", "grey" to "#1F1F1F")
        private val SECONDARY = mapOf("white" to "#636363", "sepia" to "#665E54", "green" to "#5A665A", "grey" to "#5E5E5E")
        private val ACCENT = mapOf("white" to "#1155CC", "sepia" to "#85561F", "green" to "#3E6B35", "grey" to "#1155CC")
        private val BORDER = mapOf("white" to "#E0E0E0", "sepia" to "#DFD5C0", "green" to "#C9D6C3", "grey" to "#D4D4D4")
        private val SELECTION = mapOf("white" to "#B4D5FE", "sepia" to "#E3D3AE", "green" to "#C3D8B8", "grey" to "#C8D4E0")

        private val FONT_LANGUAGES = listOf(
            "latin" to "西文 / Latin",
            "hans" to "简体中文",
            "hant" to "繁體中文",
            "japanese" to "日本語",
            "korean" to "한국어",
        )
    }
}
