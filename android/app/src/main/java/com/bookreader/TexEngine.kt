package com.bookreader

import android.content.Context
import java.io.File

/**
 * The bundled TeX engine (Tectonic: XeTeX + xdvipdfmx, built for this device's ABI).
 *
 * Everything runs offline: the LaTeX packages the app needs are shipped in
 * `assets/texbundle/` and copied once into app storage, where the engine reads them
 * as a directory bundle.  Nothing is ever downloaded.
 */
object TexEngine {

    init {
        System.loadLibrary("bookreadertex")
    }

    /** Version string of the embedded engine, e.g. "Tectonic 0.15.0 (XeTeX)". */
    external fun version(): String

    /**
     * Typeset [texPath] into [pdfPath], reading packages from [bundlePath] and keeping the
     * compiled LaTeX format in [cachePath] (built once, then reused).
     * Returns an empty string on success, or the engine's error text.
     */
    external fun typeset(texPath: String, pdfPath: String, bundlePath: String, cachePath: String): String

    /** Where the engine keeps its compiled format file. */
    fun cacheDir(context: Context): File =
        File(context.cacheDir, "texformat").apply { mkdirs() }

    /** Unpacks `assets/texbundle` into app storage on first use and returns the folder. */
    fun bundleDir(context: Context): File {
        val target = File(context.filesDir, "texbundle")
        val stamp = File(target, ".unpacked")
        if (stamp.isFile) return target
        target.mkdirs()
        unpack(context, "texbundle", target)
        stamp.writeText("1")
        return target
    }

    private fun unpack(context: Context, assetPath: String, target: File) {
        val entries = context.assets.list(assetPath) ?: return
        if (entries.isEmpty()) {                       // a file, not a directory
            context.assets.open(assetPath).use { input ->
                target.outputStream().use { input.copyTo(it) }
            }
            return
        }
        target.mkdirs()
        for (name in entries) {
            unpack(context, "$assetPath/$name", File(target, name))
        }
    }
}
