package com.bookreader

import android.content.Context
import com.chaquo.python.Kwarg
import com.chaquo.python.PyObject
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import org.json.JSONObject
import java.io.File

/**
 * Book -> LaTeX + PDF, and DjVu -> PDF, on the device.
 *
 * The conversion itself is the reader's own Python (the same modules the Windows
 * app runs), started here through Chaquopy and handed this app's folders and the
 * TeX engine that travels in the APK.
 */
object Converter {

    /** What the caller watches while a conversion runs. */
    interface Listener {
        fun onProgress(stage: String, done: Int, total: Int)
    }

    /** Lets a running conversion be stopped; nothing half-written is left behind. */
    class Cancel {
        @Volatile
        private var cancelled = false
        fun cancel() { cancelled = true }
        fun isCancelled(): Boolean = cancelled
    }

    private var bridge: PyObject? = null

    /** Starts Python and points the shared code at this app's folders.  Idempotent. */
    @Synchronized
    fun start(context: Context): PyObject {
        bridge?.let { return it }
        if (!Python.isStarted()) {
            Python.start(AndroidPlatform(context.applicationContext))
        }
        val py = Python.getInstance()
        val module = py.getModule("bookreader_android")
        module.callAttr(
            "setup",
            File(context.filesDir, "state").absolutePath,
            context.cacheDir.absolutePath,
            TexEngine.bundleDir(context).absolutePath,
            TexEngine.cacheDir(context).absolutePath,
        )
        bridge = module
        return module
    }

    /** EPUB / MOBI / AZW / AZW3 -> a folder holding the .tex, its pictures and the PDF. */
    fun convertBook(
        context: Context,
        bookPath: String,
        destination: File,
        paper: String = "a5",
        fontSize: Int = 11,
        cover: Boolean = true,
        contents: Boolean = true,
        makePdf: Boolean = true,
        listener: Listener? = null,
        cancel: Cancel? = null,
    ): JSONObject {
        destination.mkdirs()
        val module = start(context)
        val json = module.callAttr(
            "convert_book", bookPath, destination.absolutePath,
            Kwarg("paper", paper),
            Kwarg("font_size", fontSize),
            Kwarg("cover", cover),
            Kwarg("contents", contents),
            Kwarg("make_pdf", makePdf),
            Kwarg("listener", listener),
            Kwarg("cancel", cancel),
        )
        return JSONObject(json.toString()).withPageCount()
    }

    /** The PDF's own page count, which the engine does not report back. */
    private fun JSONObject.withPageCount(): JSONObject {
        val path = optString("pdf").takeIf { it.isNotEmpty() && it != "null" } ?: return this
        val file = File(path)
        if (!file.isFile) return this
        runCatching {
            android.os.ParcelFileDescriptor.open(file, android.os.ParcelFileDescriptor.MODE_READ_ONLY)
                .use { fd -> android.graphics.pdf.PdfRenderer(fd).use { put("pages", it.pageCount) } }
        }
        return this
    }

    /** DjVu -> a searchable PDF beside the chosen destination. */
    fun convertDjvu(
        context: Context,
        bookPath: String,
        pdfPath: File,
        listener: Listener? = null,
        cancel: Cancel? = null,
    ): JSONObject {
        val module = start(context)
        val json = module.callAttr(
            "convert_djvu", bookPath, pdfPath.absolutePath,
            Kwarg("listener", listener),
            Kwarg("cancel", cancel),
        )
        return JSONObject(json.toString())
    }

    /** Title, author and format of a book file, without opening the reader. */
    fun bookInfo(context: Context, bookPath: String): JSONObject =
        JSONObject(start(context).callAttr("book_info", bookPath).toString())
}
