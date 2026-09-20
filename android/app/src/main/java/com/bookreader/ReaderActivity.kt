package com.bookreader

import android.annotation.SuppressLint
import android.content.Context
import android.content.Intent
import android.graphics.Color
import android.os.Bundle
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.webkit.WebView
import android.widget.ImageButton
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.RadioButton
import android.widget.RadioGroup
import android.widget.ScrollView
import android.widget.SeekBar
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.drawerlayout.widget.DrawerLayout
import org.json.JSONArray
import org.json.JSONObject
import kotlin.math.abs

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

    // ---- reading settings -------------------------------------------------
    private var dark = false
    private var bgKey = "white"                        // white | sepia | green | grey
    private var fontSizePx = 21
    private var fontLatin = "serif"
    private var fontCjk = "sans-serif"
    private var fontJp = "sans-serif"
    private var fontJpChangedAfterCjk = false        // which CJK picker was touched last
    private var scrollMode = true                      // scroll is the phone default
    private var startSpine = 0
    private var initialized = false
    private var pendingLocator: JSONObject? = null

    private var downX = 0f
    private var downY = 0f
    private var downT = 0L

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val path = intent.getStringExtra(EXTRA_PATH) ?: run { finish(); return }
        startSpine = intent.getIntExtra("spine", 0)     // autorun/debug

        buildUi()
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
        webView.setOnTouchListener(TapListener())

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

        // -- rows 4-6: font pickers --
        sheetBg.addView(fontPicker("English font", LATIN_FONTS) { fontLatin = it; applySettings() })
        sheetBg.addView(fontPicker("Chinese font", CJK_FONTS) { fontCjk = it; fontJpChangedAfterCjk = false; applySettings() })
        sheetBg.addView(fontPicker("Japanese font", JP_FONTS) { fontJp = it; fontJpChangedAfterCjk = true; applySettings() })

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

    /** A row of radio-style choices; the chosen one is applied to the page. */
    private fun fontPicker(title: String, options: List<Pair<String, String>>, onPick: (String) -> Unit): View {
        val col = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        col.addView(label(title))
        val group = RadioGroup(this).apply { orientation = RadioGroup.HORIZONTAL }
        for (i in options.indices) {
            val name = options[i].first
            val family = options[i].second
            val rb = RadioButton(this).apply {
                text = name
                textSize = 13f
                isChecked = i == 0
                setOnClickListener { onPick(family) }
            }
            group.addView(rb)
        }
        col.addView(group)
        return col
    }

    // ======================================================================
    // Gestures
    // ======================================================================
    private inner class TapListener : View.OnTouchListener {
        override fun onTouch(v: View, e: MotionEvent): Boolean {
            when (e.actionMasked) {
                MotionEvent.ACTION_DOWN -> { downX = e.x; downY = e.y; downT = e.eventTime }
                MotionEvent.ACTION_UP -> {
                    val dx = e.x - downX
                    val dy = e.y - downY
                    val dt = e.eventTime - downT
                    val isTap = dt < 300 && abs(dx) < 16 && abs(dy) < 16
                    if (isTap) {
                        val w = v.width
                        when {
                            !scrollMode && e.x < w * 0.25f -> { turn(-1); return true }
                            !scrollMode && e.x > w * 0.75f -> { turn(1); return true }
                            else -> { toggleChrome(); return true }
                        }
                    }
                    if (abs(dy) > 140 && abs(dy) > abs(dx) * 2f) {
                        if (!scrollMode) {
                            turn(if (dy < 0) 1 else -1)
                        } else {
                            val density = v.resources.displayMetrics.density
                            val contentBottom = webView.contentHeight * density
                            val atBottom = webView.scrollY + webView.height >= contentBottom - 16
                            val atTop = webView.scrollY <= 0
                            if (dy < 0 && atBottom && currentSpine < spine.length() - 1) navigate(currentSpine + 1)
                            else if (dy > 0 && atTop && currentSpine > 0) navigate(currentSpine - 1)
                        }
                        return true
                    }
                }
            }
            return false
        }
    }

    private fun turn(dir: Int) {
        val before = page
        host.callReader(if (dir > 0) "nextPage" else "prevPage") { result ->
            val st = result as? JSONObject ?: return@callReader
            page = st.optInt("page")
            pageCount = maxOf(1, st.optInt("pages"))
            updateStatus()
            if (page == before) {
                val next = currentSpine + dir
                if (next in 0 until spine.length()) navigate(next)
            }
        }
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
        buildToc(info.optJSONArray("toc") ?: JSONArray())
        applyChromeTheme()
        navigate(startSpine)
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
                updateStatus()
            }
        }

        override fun onReady(state: JSONObject) {}

        override fun onPosition(state: JSONObject) {
            page = state.optInt("page")
            pageCount = maxOf(1, state.optInt("pages"))
            updateStatus()
            val loc = state.opt("locator") ?: state.optJSONObject("capture")
            if (loc != null) host.savePosition(loc.toString(), state.optDouble("percent"))
        }

        override fun onLink(zip: String, fragment: String, url: String?) {
            if (url != null) return
            val idx = findSpine(zip)
            if (idx >= 0) navigate(idx, fragment)
        }

        override fun onLog(message: String) {
            android.util.Log.i("BookReader", "page: $message")
        }
    }

    private fun findSpine(zipName: String): Int {
        for (i in 0 until spine.length()) {
            if (spine.getJSONObject(i).getString("zip") == zipName) return i
        }
        return -1
    }

    private fun updateStatus() {
        val percent = if (totalUnits > 1) {
            val off = spine.getJSONObject(currentSpine).optInt("offset")
            val units = spine.getJSONObject(currentSpine).optInt("units", 1)
            val frac = if (pageCount > 0) page.toDouble() / pageCount else 0.0
            ((off + frac * units) / totalUnits * 100).toInt()
        } else 0
        progress.progress = percent
        status.text = "${page + 1} / $pageCount · $percent%"
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
        // reader.js's font_cjk is ONE family name (it builds the CJK stack from it).
        // The Chinese and Japanese pickers both feed it; the most recent choice wins,
        // since a CJK font renders Chinese, Japanese and Korean alike.
        val cjk = if (fontJpChangedAfterCjk) fontJp else fontCjk
        return JSONObject()
            .put("theme", if (dark) "night" else "day")
            .put("colors", colors)
            .put("layout", if (scrollMode) "scroll" else "paged")
            .put("font_size_px", fontSizePx)
            .put("line_height", Math.round(lineHeight * 100.0) / 100.0)
            .put("page_margin_px", 48)
            .put("font_latin", fontLatin)
            .put("font_cjk", cjk)
            .put("image_click_zoom", true)
            .put("invert_images_in_dark", dark)
    }

    private fun applySettings() {
        applyChromeTheme()
        if (initialized) host.callReader("applySettings", settingsJson())
    }

    private fun toggleDark() {
        dark = !dark
        applySettings()
    }

    private fun toggleMode() {
        scrollMode = !scrollMode
        if (initialized) host.callReader("applySettings", settingsJson())
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

        // Fonts that actually resolve on Android: the system only exposes the generic
        // families (serif / sans-serif / monospace / cursive) — named fonts like
        // "Noto Sans CJK SC" are NOT in fonts.xml, so WebView silently ignores them.
        // The CJK serif/sans difference is real: serif maps to Noto Serif CJK and
        // sans-serif to Noto Sans CJK.
        private val LATIN_FONTS = listOf(
            "Serif" to "serif",
            "Sans" to "sans-serif",
            "Monospace" to "monospace",
            "Cursive" to "cursive",
        )
        private val CJK_FONTS = listOf(
            "Sans" to "sans-serif",
            "Serif" to "serif",
            "Monospace" to "monospace",
        )
        private val JP_FONTS = listOf(
            "Sans" to "sans-serif",
            "Serif" to "serif",
            "Monospace" to "monospace",
        )
    }
}
