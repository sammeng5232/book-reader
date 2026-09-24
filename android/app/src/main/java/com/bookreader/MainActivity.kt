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
        if (savedInstanceState == null) routeOpenIntent(intent)
    }

    override fun onResume() {
        super.onResume()
        if (::recentBox.isInitialized) refreshRecent()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        routeOpenIntent(intent)
    }

    // --------------------------------------------------------------
    // Import: SAF uri -> a real file the app owns
    // --------------------------------------------------------------
    private fun importBook(uri: Uri): File? {
        var temporary: File? = null
        return runCatching {
            var name = (displayName(uri) ?: "book").substringAfterLast('/').substringAfterLast('\\')
                .filter { it >= ' ' && it != '\u007f' }.takeIf { it != "." && it != ".." && it.isNotBlank() } ?: "book"
            if (contentResolver.getType(uri) == "application/pdf" && !name.endsWith(".pdf", ignoreCase = true)) name += ".pdf"
            val digest = java.security.MessageDigest.getInstance("SHA-256")
            val stage = File.createTempFile("book-import-", ".part", cacheDir).also { temporary = it }
            contentResolver.openInputStream(uri)?.use { input ->
                java.security.DigestInputStream(input, digest).use { source ->
                    stage.outputStream().use { source.copyTo(it) }
                }
            } ?: error("Could not open the selected file")
            // A different book with the same display name must not replace an
            // earlier import. Keep its original basename inside a content folder.
            val key = digest.digest().joinToString("") { "%02x".format(it) }.take(32)
            val dir = File(filesDir, "books/imports/$key").apply { mkdirs() }
            val dest = File(dir, name)
            if (!dest.isFile) {
                if (!stage.renameTo(dest)) stage.copyTo(dest, overwrite = false)
            }
            dest
        }.onFailure { Log.w("BookReader", "import failed", it) }.getOrNull().also { temporary?.delete() }
    }

    private fun routeOpenIntent(incoming: Intent?) {
        if (incoming?.action == Intent.ACTION_VIEW && incoming.data != null) {
            val uri = incoming.data!!
            if (uri.scheme !in setOf("content", "file")) {
                say("Please choose a local book or PDF file.")
                return
            }
            setBusy(true)
            thread {
                val file = importBook(uri)
                runOnUiThread {
                    setBusy(false)
                    if (file == null) say("Could not read that file.") else openBook(file)
                }
            }
        } else handleAutorun(incoming)
    }

    private fun isPdf(file: File): Boolean = file.extension.equals("pdf", ignoreCase = true) || runCatching {
        file.inputStream().use { input ->
            val signature = ByteArray(5)
            input.read(signature) == 5 && signature.contentEquals("%PDF-".toByteArray(Charsets.US_ASCII))
        }
    }.getOrDefault(false)

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
        if (isPdf(file)) {
            PdfReaderActivity.open(this, file.absolutePath, file.nameWithoutExtension)
            return
        }
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
        if (isPdf(file)) {
            openBook(file)
            return
        }
        // Convert into a private temp folder, then copy the results into the public
        // Downloads/Book Reader folder so the user can find them in any file manager
        // (Android/data is hidden from file managers).
        val work = File(cacheDir, "convert-out/${System.currentTimeMillis()}").apply { mkdirs() }
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
            val conversionSeconds = (System.currentTimeMillis() - started) / 1000.0
            val ok = result.optBoolean("ok")
            // "ok" only means the pipeline ran.  A missing pdf with ok=true is the
            // typesetter failing; its errors come back in "problems" and must be
            // shown to the user, not a success message reading "pages: 0".
            val pdfPath = result.optString("pdf").takeIf { it.isNotEmpty() && it != "null" }
            val problems = result.optJSONArray("problems")?.let { ja ->
                (0 until ja.length()).mapNotNull { ja.optString(it).ifEmpty { null } }
            } ?: emptyList()
            // Report the engine before copying thousands of images to MediaStore.
            // A failed compile must not be hidden behind a long "Typesetting" timer.
            Log.i("BookReader", "engine result: ok=$ok pages=${result.optInt("pages")} " +
                    "pdf=${pdfPath ?: "none"} seconds=$conversionSeconds")
            if (problems.isNotEmpty()) Log.w("BookReader", "engine problems: ${problems.joinToString(" | ")}")
            if (!ok) Log.e("BookReader", "conversion failed: ${result.optString("error")}")
            val published = if (ok) publishResults(work, file.nameWithoutExtension) else PublishedResults()
            val saved = published.saved
            val seconds = (System.currentTimeMillis() - started) / 1000.0
            Log.i("BookReader", "export result: saved=${saved.size} failed=${published.failed.size} seconds=$seconds")
            val savedList = (if (saved.isEmpty()) "" else buildString {
                append("\nSaved to Downloads/Book Reader:")
                for (name in saved.take(12)) append("\n  $name")
                if (saved.size > 12) append("\n  … and ${saved.size - 12} more")
            }) + if (published.failed.isEmpty()) "" else
                "\n${published.failed.size} files could not be saved. Local copies kept at ${work.absolutePath}."
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

    private data class PublishedResults(
        val saved: List<String> = emptyList(),
        val failed: List<String> = emptyList(),
    )

    /** Publish one complete export without deleting or renaming earlier exports. */
    private fun publishResults(work: File, sourceStem: String): PublishedResults {
        val saved = ArrayList<String>()
        val failed = ArrayList<String>()
        val exportRoot = work.listFiles()?.singleOrNull()?.takeIf { it.isDirectory } ?: work
        val originalFolder = if (exportRoot == work) sourceStem else exportRoot.name
        val files = exportRoot.walkTopDown().filter { it.isFile }.sortedWith(
            compareBy<File> { when (it.extension.lowercase()) { "pdf" -> 0; "tex" -> 1; else -> 2 } }
                .thenBy { it.relativeTo(exportRoot).invariantSeparatorsPath }
        ).toList()
        val store = android.provider.MediaStore.Downloads.EXTERNAL_CONTENT_URI
        val colRel = android.provider.MediaStore.MediaColumns.RELATIVE_PATH
        val colName = android.provider.MediaStore.MediaColumns.DISPLAY_NAME
        val colPending = android.provider.MediaStore.MediaColumns.IS_PENDING
        val downloads = android.os.Environment.getExternalStoragePublicDirectory(android.os.Environment.DIRECTORY_DOWNLOADS)
        val publicRoot = File(downloads, "Book Reader")
        // Choose once per export, so filenames and relative image links remain exact.
        // Directory stat also sees folders left by a previous installation.
        var folder = originalFolder
        var suffix = 2
        while (File(publicRoot, folder).exists()) folder = "$originalFolder (${suffix++})"
        var relativeRoot = "Download/Book Reader/$folder"
        val listener = progressListener()
        listener.onProgress("save", 0, files.size)
        val colId = android.provider.BaseColumns._ID
        fun publishBatch(batch: List<File>, startIndex: Int) {
            val created = ArrayList<Uri>()
            val ready = ArrayList<Pair<File, Uri>>()
            try {
                val inserts = ArrayList<android.content.ContentProviderOperation>()
                for (f in batch) {
                    val rel = f.relativeTo(exportRoot).invariantSeparatorsPath
                    val dir = rel.substringBeforeLast('/', "")
                    val values = android.content.ContentValues().apply {
                        put(colName, f.name)
                        put(colRel, if (dir.isEmpty()) relativeRoot else "$relativeRoot/$dir")
                        put(colPending, 1)
                        put(android.provider.MediaStore.Downloads.MIME_TYPE,
                            if (f.extension.equals("pdf", true)) "application/pdf" else "application/octet-stream")
                    }
                    inserts.add(android.content.ContentProviderOperation.newInsert(store).withValues(values).build())
                }
                val results = contentResolver.applyBatch(android.provider.MediaStore.AUTHORITY, inserts)
                for (result in results) created.add(result.uri ?: throw java.io.IOException("MediaStore refused an output file"))
                if (created.size != batch.size) throw java.io.IOException("MediaStore returned an incomplete batch")
                val ids = created.map { android.content.ContentUris.parseId(it) }
                val metadata = HashMap<Long, Pair<String, String>>()
                @Suppress("DEPRECATION")
                val pendingStore = android.provider.MediaStore.setIncludePending(store)
                contentResolver.query(pendingStore, arrayOf(colId, colName, colRel),
                    "$colId IN (${ids.joinToString(",") { "?" }})", ids.map { it.toString() }.toTypedArray(), null)?.use { c ->
                    while (c.moveToNext()) metadata[c.getLong(0)] = c.getString(1) to c.getString(2).trimEnd('/')
                } ?: throw java.io.IOException("Cannot verify output filenames")
                for ((offset, f) in batch.withIndex()) {
                    val index = startIndex + offset
                    val rel = f.relativeTo(exportRoot).invariantSeparatorsPath
                    val dir = rel.substringBeforeLast('/', "")
                    val uri = created[offset]
                    try {
                        val actual = metadata[ids[offset]] ?: throw java.io.IOException("Cannot verify ${f.name}")
                        if (actual.first != f.name) throw java.io.IOException("Destination name is already in use: ${f.name}")
                        // The first file reserves the export's directory. Preserve
                        // a folder adjustment made by MediaStore for every image.
                        if (index == 0 && dir.isEmpty()) {
                            relativeRoot = actual.second
                            folder = actual.second.substringAfterLast('/')
                        } else {
                            val expected = if (dir.isEmpty()) relativeRoot else "$relativeRoot/$dir"
                            if (actual.second != expected) throw java.io.IOException("MediaStore changed the output folder for ${f.name}")
                        }
                        val stream = contentResolver.openOutputStream(uri)
                            ?: throw java.io.IOException("Cannot write ${f.name}")
                        stream.use { target -> f.inputStream().use { it.copyTo(target) } }
                        ready.add(f to uri)
                    } catch (e: Exception) {
                        runCatching { contentResolver.delete(uri, null, null) }
                        failed.add(rel)
                        Log.w("BookReader", "publish failed for $rel", e)
                    }
                    if ((index + 1) % 25 == 0 || index + 1 == files.size) {
                        listener.onProgress("save", index + 1, files.size)
                    }
                }
                val updates = ArrayList<android.content.ContentProviderOperation>()
                for ((_, uri) in ready) updates.add(android.content.ContentProviderOperation.newUpdate(uri)
                    .withValue(colPending, 0).build())
                if (updates.isNotEmpty()) contentResolver.applyBatch(android.provider.MediaStore.AUTHORITY, updates)
                for ((f, _) in ready) saved.add("$folder/${f.relativeTo(exportRoot).invariantSeparatorsPath}")
            } catch (e: Exception) {
                // Only fresh rows belong to this batch; historical exports are
                // never deleted or renamed. Keep the local files for recovery.
                for (uri in created) runCatching { contentResolver.delete(uri, null, null) }
                for (f in batch) {
                    val rel = f.relativeTo(exportRoot).invariantSeparatorsPath
                    if (rel !in failed) failed.add(rel)
                }
                Log.w("BookReader", "publish batch failed", e)
            }
        }
        // PDF and TeX become usable immediately. Batch only the many image rows
        // to avoid thousands of insert/query/update database round trips.
        val firstFiles = files.takeWhile { it.extension.lowercase() in setOf("pdf", "tex") }
        for ((index, f) in firstFiles.withIndex()) publishBatch(listOf(f), index)
        var offset = firstFiles.size
        for (batch in files.drop(offset).chunked(64)) {
            publishBatch(batch, offset)
            offset += batch.size
        }
        if (failed.isEmpty()) work.deleteRecursively()
        return PublishedResults(saved, failed)
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
        "save" -> "Saving files"
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
        "pages" to (0 to 970),
        "save" to (970 to 1000),
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
                val msg = runCatching { typesetDocument(filePath) }
                    .getOrElse { "TeX reported a problem:\n${it.stackTraceToString()}" }
                Log.i("BookReader", msg)
                runOnUiThread { say(msg) }
            }
        }
    }

    /** Exercise the embedded engine directly, with the sample or `--es file <tex>`. */
    private fun typesetDocument(filePath: String): String {
        val tex = if (filePath.isNotEmpty()) File(filePath) else {
            val work = File(cacheDir, "texsample").apply { mkdirs() }
            File(work, "sample.tex").also { target ->
                assets.open("sample/sample.tex").use { input -> target.outputStream().use { input.copyTo(it) } }
            }
        }
        require(tex.isFile && tex.extension.equals("tex", ignoreCase = true)) { "No TeX source: $tex" }
        val pdf = File(tex.parentFile, tex.nameWithoutExtension + ".pdf")
        // The test must prove a new PDF was produced, not report an old output.
        if (pdf.exists() && !pdf.delete()) error("Cannot replace $pdf")
        val error = TexEngine.typeset(
            tex.absolutePath, pdf.absolutePath,
            TexEngine.bundleDir(this).absolutePath,
            TexEngine.cacheDir(this).absolutePath
        )
        return if (error.isEmpty() && pdf.isFile) "PDF: ${pdf.absolutePath}\n${pdf.length()} bytes"
        else "TeX reported a problem:\n$error"
    }
}
