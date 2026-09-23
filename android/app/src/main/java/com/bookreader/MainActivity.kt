package com.bookreader

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.provider.OpenableColumns
import android.util.Log
import android.view.View
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.ScrollView
import android.widget.TextView
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import java.io.File
import kotlin.concurrent.thread

/**
 * The whole app in one screen: **Open** a book to read it, **Convert** a book to
 * LaTeX + PDF (DjVu straight to PDF).  Books are picked with the system file picker;
 * the bytes are copied into the app's own storage, so the source file is never
 * touched (the user's rule) and the Python side gets a real, seekable path.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var log: TextView
    private lateinit var bar: ProgressBar
    private lateinit var barLabel: TextView
    private lateinit var openButton: Button
    private lateinit var convertButton: Button
    private lateinit var recentBox: LinearLayout
    private lateinit var recentHeader: TextView

    private var pendingAction: ((File) -> Unit)? = null

    // --------------------------------------------------------------
    // Recent books (paths in app storage, most recent first)
    // --------------------------------------------------------------
    private val prefs by lazy { getSharedPreferences("reader", Context.MODE_PRIVATE) }

    private fun recentBooks(): List<String> =
        (prefs.getStringSet("recent", emptySet()) ?: emptySet()).toList()
            .mapNotNull { entry ->
                val i = entry.lastIndexOf('|')
                if (i < 0) null else (entry.substring(0, i).toLongOrNull() ?: 0L) to entry.substring(i + 1)
            }
            .sortedByDescending { it.first }
            .map { it.second }
            .filter { File(it).isFile }

    private fun addRecent(path: String) {
        val entries = (prefs.getStringSet("recent", emptySet()) ?: emptySet())
            .filterNot { it.endsWith("|$path") }
            .toMutableSet()
        entries.add("${System.currentTimeMillis()}|$path")
        prefs.edit().putStringSet("recent", entries).apply()
        refreshRecent()
    }

    private fun removeRecent(path: String) {
        val entries = (prefs.getStringSet("recent", emptySet()) ?: emptySet())
            .filterNot { it.endsWith("|$path") }
            .toMutableSet()
        prefs.edit().putStringSet("recent", entries).apply()
        refreshRecent()
    }

    private fun refreshRecent() {
        recentBox.removeAllViews()
        val books = recentBooks()
        recentHeader.visibility = if (books.isEmpty()) View.GONE else View.VISIBLE
        for (path in books) {
            val f = File(path)
            val row = TextView(this).apply {
                text = f.name
                textSize = 15f
                setPadding(8, 18, 8, 18)
                setOnClickListener { openBook(f) }
                setOnLongClickListener {
                    removeRecent(path)
                    true
                }
            }
            recentBox.addView(row)
            recentBox.addView(View(this).apply {
                setBackgroundColor(0x1F000000)
                layoutParams = LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 1)
            })
        }
    }

    private val pickBook = registerForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        val action = pendingAction ?: return@registerForActivityResult
        pendingAction = null
        if (uri == null) return@registerForActivityResult
        setBusy(true)
        thread {
            val file = importBook(uri)
            runOnUiThread {
                setBusy(false)
                if (file == null) {
                    say("Could not read that file.")
                } else {
                    action(file)
                }
            }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(32, 48, 32, 32)
        }
        openButton = Button(this).apply { text = "Open a book" }
        convertButton = Button(this).apply { text = "Convert to PDF / TeX" }
        // progress: a labelled bar the user can watch (determinate when a count is
        // known, a live clock while typesetting)
        barLabel = TextView(this).apply { textSize = 14f; visibility = View.GONE }
        bar = ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal).apply {
            visibility = View.GONE
            max = 1000
        }
        log = TextView(this).apply { textSize = 13f; setTextIsSelectable(true) }
        // the recent-books list sits under the buttons
        recentHeader = TextView(this).apply {
            text = "Recent"
            textSize = 13f
            alpha = 0.7f
            setPadding(0, 20, 0, 4)
        }
        recentBox = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        root.addView(openButton)
        root.addView(convertButton)
        root.addView(barLabel)
        root.addView(bar, LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT))
        root.addView(recentHeader)
        root.addView(recentBox)
        root.addView(ScrollView(this).apply { addView(log) })
        setContentView(root)

        openButton.setOnClickListener {
            pendingAction = { f -> openBook(f) }
            pickBook.launch(arrayOf("*/*"))
        }
        convertButton.setOnClickListener {
            pendingAction = { f -> convertBook(f) }
            pickBook.launch(arrayOf("*/*"))
        }

        say(getString(R.string.engine_ready, TexEngine.version()))
        refreshRecent()
        handleAutorun(intent)
    }

    override fun onResume() {
        super.onResume()
        if (::recentBox.isInitialized) refreshRecent()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleAutorun(intent)
    }

    // --------------------------------------------------------------
    // Import: SAF uri -> a real file the app owns
    // --------------------------------------------------------------
    private fun importBook(uri: Uri): File? {
        return runCatching {
            val name = displayName(uri) ?: "book"
            val dir = File(filesDir, "books").apply { mkdirs() }
            val dest = File(dir, name)
            contentResolver.openInputStream(uri)?.use { input ->
                dest.outputStream().use { input.copyTo(it) }
            } ?: return null
            dest
        }.onFailure { Log.w("BookReader", "import failed", it) }.getOrNull()
    }

    private fun displayName(uri: Uri): String? {
        runCatching {
            contentResolver.query(uri, null, null, null, null)?.use { c ->
                val i = c.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                if (i >= 0 && c.moveToFirst()) return c.getString(i)
            }
        }
        return uri.lastPathSegment?.substringAfterLast('/')
    }

    // --------------------------------------------------------------
    // Actions
    // --------------------------------------------------------------
    private fun openBook(file: File) {
        val spine = intent.getIntExtra("spine", 0)
        addRecent(file.absolutePath)
        when (file.extension.lowercase()) {
            // DjVu is decoded directly by the Kotlin decoder — no conversion needed.
            "djvu", "djv" -> DjvuReaderActivity.open(this, file.absolutePath)
            "epub", "mobi", "azw", "azw3" -> {
                val i = Intent(this, ReaderActivity::class.java)
                    .putExtra(ReaderActivity.EXTRA_PATH, file.absolutePath)
                    .putExtra("spine", spine)
                startActivity(i)
            }
            "pdf" -> PdfReaderActivity.open(this, file.absolutePath, file.nameWithoutExtension)
            else -> say("This format is not supported yet: ${file.name}")
        }
    }

    private fun convertBook(file: File) {
        // Convert into a private temp folder, then copy the results into the public
        // Downloads/Book Reader folder so the user can find them in any file manager
        // (Android/data is hidden from file managers).
        val work = File(cacheDir, "convert-out").apply { deleteRecursively(); mkdirs() }
        say("Converting ${file.name}…")
        setBusy(true)
        lastStage = ""
        typesetStartMs = 0L
        showProgress(null)                          // indeterminate until the first report
        bar.post(typesetTicker)                     // keeps the typeset percentage moving
        // Long conversions must survive battery managers (Samsung freezes the app
        // the moment the screen is off): run under a foreground service.  Ask for
        // the notification permission once (Android 13+); the service also works
        // with the notification silenced.
        if (android.os.Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS)
                != android.content.pm.PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(android.Manifest.permission.POST_NOTIFICATIONS), 7)
        }
        ConvertService.start(this)
        thread {
            val started = System.currentTimeMillis()
            val result = if (file.extension.lowercase() in setOf("djvu", "djv")) {
                Converter.convertDjvu(this, file.absolutePath, File(work, file.nameWithoutExtension + ".pdf"),
                    listener = progressListener())
            } else {
                Converter.convertBook(this, file.absolutePath, work, listener = progressListener())
            }
            val seconds = (System.currentTimeMillis() - started) / 1000.0
            val ok = result.optBoolean("ok")
            // "ok" only means the pipeline ran.  A missing pdf with ok=true is the
            // typesetter failing; its errors come back in "problems" and must be
            // shown to the user, not a success message reading "pages: 0".
            val pdfPath = result.optString("pdf").takeIf { it.isNotEmpty() && it != "null" }
            val problems = result.optJSONArray("problems")?.let { ja ->
                (0 until ja.length()).mapNotNull { ja.optString(it).ifEmpty { null } }
            } ?: emptyList()
            // move every produced file to Downloads/Book Reader
            val saved = if (ok) publishResults(work) else emptyList()
            val savedList = if (saved.isEmpty()) "" else buildString {
                append("\nSaved to Downloads/Book Reader:")
                for (name in saved.take(12)) append("\n  $name")
                if (saved.size > 12) append("\n  … and ${saved.size - 12} more")
            }
            runOnUiThread {
                setBusy(false)
                hideProgress()
                ConvertService.stop(this@MainActivity)
                when {
                    !ok -> {
                        Log.e("BookReader", "conversion failed: ${result.optString("error")}" +
                                "\n${result.optString("trace")}")
                        say(buildString {
                            append("Conversion failed: ${result.optString("error")}")
                            val detail = result.optString("detail")
                            if (detail.isNotEmpty() && detail != "null") append("\n$detail")
                        })
                    }
                    pdfPath == null -> {
                        Log.e("BookReader", "typesetter produced no PDF: ${problems.joinToString(" | ")}")
                        say(buildString {
                            append("${file.name}\n")
                            append("The LaTeX source was written, but the typesetter produced no PDF:")
                            for (p in problems.take(5)) append("\n  $p")
                            append(savedList)
                            append("\n(${"%.1f".format(seconds)} s)")
                        })
                    }
                    else -> {
                        if (problems.isNotEmpty()) {
                            Log.w("BookReader", "conversion problems: ${problems.joinToString(" | ")}")
                        }
                        say(buildString {
                            append("${file.name}\n")
                            append("pages: ${result.optInt("pages")}")
                            append(savedList)
                            append("\n(${"%.1f".format(seconds)} s)")
                        })
                    }
                }
            }
        }
    }

    /**
     * Copy every file the converter produced under [work] into the public
     * `Downloads/Book Reader/` folder via MediaStore (no permission needed),
     * keeping the converter's folder layout (`<title>/<title>.tex`,
     * `<title>/images/…`) so the .tex still finds its pictures when it is
     * compiled elsewhere.  Returns the published relative paths.  The private
     * copies are deleted afterwards.
     */
    private fun publishResults(work: File): List<String> {
        val saved = ArrayList<String>()
        val files = work.walkTopDown().filter { it.isFile }.toList()
        val store = android.provider.MediaStore.Downloads.EXTERNAL_CONTENT_URI
        val colRel = android.provider.MediaStore.MediaColumns.RELATIVE_PATH
        val colName = android.provider.MediaStore.MediaColumns.DISPLAY_NAME
        val colId = android.provider.BaseColumns._ID
        for (f in files) {
            val rel = f.relativeTo(work).path.replace(File.separatorChar, '/')
            val dir = rel.substringBeforeLast('/', "")
            runCatching {
                val values = android.content.ContentValues().apply {
                    put(android.provider.MediaStore.Downloads.DISPLAY_NAME, f.name)
                    put(android.provider.MediaStore.Downloads.RELATIVE_PATH,
                        if (dir.isEmpty()) "Download/Book Reader" else "Download/Book Reader/$dir")
                    put(android.provider.MediaStore.Downloads.MIME_TYPE,
                        if (f.extension.lowercase() == "pdf") "application/pdf" else "application/octet-stream")
                }
                val uri = contentResolver.insert(store, values)
                    ?: throw java.io.IOException("MediaStore refused ${f.name}")
                contentResolver.openOutputStream(uri)?.use { out -> f.inputStream().use { it.copyTo(out) } }
                // MediaStore never replaces: a taken name silently becomes "name (1)"
                // (or, under load, a "folder (2)").  Detect the rename and take the
                // name back: free it, then rename our fresh file into place.
                val got = contentResolver.query(uri, arrayOf(colName), null, null, null)
                    ?.use { c -> if (c.moveToFirst()) c.getString(0) else null }
                if (got != null && got != f.name) {
                    val base = if (dir.isEmpty()) "Download/Book Reader" else "Download/Book Reader/$dir"
                    contentResolver.query(store, arrayOf(colId),
                        "($colRel=? OR $colRel=?) AND $colName=?", arrayOf(base, "$base/", f.name), null)
                        ?.use { c ->
                            val ids = ArrayList<Long>()
                            while (c.moveToNext()) ids.add(c.getLong(0))
                            for (id in ids) contentResolver.delete(
                                android.net.Uri.withAppendedPath(store, id.toString()), null, null)
                        }
                    val up = android.content.ContentValues()
                    up.put(colName, f.name)
                    fun currentName(): String? = contentResolver.query(uri, arrayOf(colName), null, null, null)
                        ?.use { c -> if (c.moveToFirst()) c.getString(0) else null }
                    contentResolver.update(uri, up, null, null)
                    if (currentName() != f.name) {     // rare: the freed name needs a beat
                        Thread.sleep(250)
                        contentResolver.update(uri, up, null, null)
                    }
                    val now = currentName()
                    if (now != f.name) {
                        Log.w("BookReader", "publish: could not reclaim the name ${f.name} (is $now)")
                    }
                }
                saved.add(rel)
            }.onFailure { Log.w("BookReader", "publish failed for $rel", it) }
        }
        work.deleteRecursively()
        return saved
    }

    // --------------------------------------------------------------
    // Progress reporting
    // --------------------------------------------------------------
    private var lastStage = ""
    private var typesetStartMs = 0L

    /** Reports from the converter, on whatever thread it calls from. */
    private fun progressListener() = object : Converter.Listener {
        override fun onProgress(stage: String, done: Int, total: Int) {
            runOnUiThread {
                if (stage != lastStage) {
                    lastStage = stage
                    if (stage.startsWith("typeset")) typesetStartMs = System.currentTimeMillis()
                    Log.i("BookReader", "stage: $stage")
                }
                showProgress(stage, done, total)
            }
        }
    }

    private fun stageLabel(stage: String): String = when (stage) {
        "convert", "read" -> "Reading the book"
        "typeset", "typeset1", "typeset2", "typeset3" -> "Typesetting the PDF"
        "pages" -> "Building pages"
        else -> stage.replaceFirstChar { it.uppercase() }
    }

    // Each stage's slice of the 0..1000 bar (mirrors the desktop's convert_dialog).
    // Typesetting dominates a LaTeX conversion, so it owns the widest slice.
    private val STAGE_SPAN = mapOf(
        "read" to (0 to 60),
        "convert" to (60 to 250),
        "typeset1" to (250 to 700),
        "typeset2" to (700 to 970),
        "typeset3" to (970 to 1000),
        "typeset" to (250 to 1000),
        "pages" to (0 to 1000),
    )

    /** One 0..1000 progress value from the stage + the within-stage counts. */
    private fun overallProgress(stage: String, done: Int, total: Int): Pair<Int, String> {
        val span = STAGE_SPAN[stage] ?: (0 to 0)
        val (lo, hi) = span
        val frac = when {
            total > 0 -> done.toDouble() / total
            // typesetting writes the PDF only at the end, so there is no real count;
            // advance by elapsed time toward the end of the slice, capped so it never
            // quite reaches the top before the stage actually finishes.
            stage.startsWith("typeset") -> {
                if (typesetStartMs == 0L) typesetStartMs = System.currentTimeMillis()
                val secs = (System.currentTimeMillis() - typesetStartMs) / 1000.0
                // ease toward 1: about 90% after ~2 minutes, asymptotic
                (secs / (secs + 90.0)).coerceAtMost(0.98)
            }
            else -> 0.0
        }
        val value = (lo + (hi - lo) * frac).toInt().coerceIn(0, 1000)
        val pct = value / 10
        val detail = when {
            total > 0 -> "$done / $total"
            stage.startsWith("typeset") -> "${((System.currentTimeMillis() - typesetStartMs) / 1000)}s"
            else -> ""
        }
        val text = if (detail.isEmpty()) "${stageLabel(stage)}  —  $pct%"
                   else "${stageLabel(stage)}  —  $detail  ($pct%)"
        return value to text
    }

    /** During typesetting there are no intermediate counts, so re-render the
     *  time-based percentage once a second to keep the bar moving. */
    private val typesetTicker = object : Runnable {
        override fun run() {
            if (bar.visibility != View.VISIBLE) return
            if (lastStage.startsWith("typeset")) showProgress(lastStage, 0, 0)
            bar.postDelayed(this, 1000)
        }
    }

    private fun showProgress(stage: String?, done: Int = 0, total: Int = 0) {
        bar.visibility = View.VISIBLE
        barLabel.visibility = View.VISIBLE
        if (stage == null) {                          // the very start, before any report
            bar.isIndeterminate = true
            barLabel.text = "Starting…"
            return
        }
        bar.isIndeterminate = false
        val (value, text) = overallProgress(stage, done, total)
        bar.progress = value
        barLabel.text = text
        Log.i("BookReader", "progress: $text")
    }

    private fun hideProgress() {
        bar.visibility = View.GONE
        barLabel.visibility = View.GONE
        lastStage = ""
    }

    // --------------------------------------------------------------
    // Small helpers
    // --------------------------------------------------------------
    private fun setBusy(busy: Boolean) {
        openButton.isEnabled = !busy
        convertButton.isEnabled = !busy
    }

    private fun say(text: String) {
        log.append(text + "\n\n")
    }

    /** adb entry point, unchanged shape: `--es autorun open|convert --es file <path>`. */
    private fun handleAutorun(intent: Intent?) {
        val autorun = intent?.getStringExtra("autorun") ?: return
        val filePath = intent.getStringExtra("file") ?: ""
        when (autorun) {
            "read", "open" -> {
                val f = if (filePath.isNotEmpty()) File(filePath)
                else File(filesDir, "books").listFiles()?.firstOrNull()
                if (f != null && f.isFile) openBook(f) else say("nothing to open")
            }
            "convert" -> {
                val f = if (filePath.isNotEmpty()) File(filePath)
                else File(filesDir, "books").listFiles()?.firstOrNull()
                if (f != null && f.isFile) convertBook(f) else say("nothing to convert")
            }
            "typeset" -> thread {
                val msg = typesetSample()
                runOnUiThread { say(msg) }
            }
        }
    }

    /** Kept for the `typeset` autorun: the bundled TeX engine on its own. */
    private fun typesetSample(): String {
        val work = File(cacheDir, "texsample").apply { mkdirs() }
        val tex = File(work, "sample.tex")
        assets.open("sample/sample.tex").use { input -> tex.outputStream().use { input.copyTo(it) } }
        val pdf = File(work, "sample.pdf")
        val error = TexEngine.typeset(
            tex.absolutePath, pdf.absolutePath,
            TexEngine.bundleDir(this).absolutePath,
            TexEngine.cacheDir(this).absolutePath
        )
        return if (error.isEmpty() && pdf.isFile) "PDF: ${pdf.absolutePath}\n${pdf.length()} bytes"
        else "TeX reported a problem:\n$error"
    }
}
