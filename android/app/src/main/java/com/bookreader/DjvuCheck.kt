package com.bookreader

import android.content.Context
import android.util.Log
import com.bookreader.djvu.DjvuDocument
import java.io.File

/**
 * Temporary harness: runs the Kotlin DjVu decoder over one file and writes out the
 * same artefacts the C# `djvutool` produces, so the two can be diffed on the host.
 *
 * Started with `am start -e autorun djvu -e file <path> -e pages 0,1,7`; the results
 * land in `<external files>/djvucheck/<name>/` and are pulled with `adb pull`.
 */
object DjvuCheck {

    fun run(context: Context, path: String, pageList: String): String {
        val src = File(path)
        if (!src.isFile) return "no such file: $path"
        val out = File(context.getExternalFilesDir(null) ?: context.filesDir,
                       "djvucheck/" + src.nameWithoutExtension).apply { mkdirs() }
        out.listFiles()?.forEach { it.delete() }
        val log = StringBuilder()
        val doc = DjvuDocument()
        try {
            var t = System.currentTimeMillis()
            doc.open(src.absolutePath)
            log.append("open: ${ms(t)} ms, ${doc.pageCount} pages\n")

            t = System.currentTimeMillis()
            File(out, "info.json").writeBytes(doc.infoJson().toByteArray(Charsets.UTF_8))
            log.append("info: ${ms(t)} ms\n")

            t = System.currentTimeMillis()
            File(out, "alltext.json").writeBytes(doc.allTextJson().toByteArray(Charsets.UTF_8))
            log.append("alltext: ${ms(t)} ms\n")

            for (p in pageList.split(",").mapNotNull { it.trim().toIntOrNull() }) {
                if (p >= doc.pageCount) continue
                t = System.currentTimeMillis()
                File(out, "text_$p.json").writeBytes(doc.textJson(p).toByteArray(Charsets.UTF_8))
                val textMs = ms(t)
                t = System.currentTimeMillis()
                File(out, "render_${p}_jpg.jpg").writeBytes(doc.render(p, 1800, "jpg"))
                val jpgMs = ms(t)
                t = System.currentTimeMillis()
                File(out, "render_${p}_png.png").writeBytes(doc.render(p, 1800, "png"))
                val pngMs = ms(t)
                t = System.currentTimeMillis()
                File(out, "layers_$p.bin").writeBytes(doc.layers(p, 32))
                val layMs = ms(t)
                log.append("page $p: text ${textMs} ms, jpg ${jpgMs} ms, png ${pngMs} ms, layers ${layMs} ms\n")
                Log.i("BookReader", "djvucheck page $p done")
            }
        } catch (e: Throwable) {
            log.append("FAILED: $e\n${e.stackTraceToString().take(1500)}\n")
        } finally {
            doc.close()
        }
        File(out, "log.txt").writeBytes(log.toString().toByteArray(Charsets.UTF_8))
        return "out: ${out.absolutePath}\n$log"
    }

    private fun ms(since: Long) = System.currentTimeMillis() - since
}
