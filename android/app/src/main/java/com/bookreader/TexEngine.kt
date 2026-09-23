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

    /** Where the engine keeps its compiled format file.  The format is tied to
     *  the bundle it was built from, so it is cleared whenever the bundle's
     *  fingerprint changes. */
    fun cacheDir(context: Context): File {
        val dir = File(context.cacheDir, "texformat").apply { mkdirs() }
        val stamp = File(dir, ".bundle-fingerprint")
        val fingerprint = bundleFingerprint(context)
        if (!stamp.isFile || stamp.readText() != fingerprint) {
            dir.listFiles()?.forEach { if (it.name != stamp.name) it.delete() }
            stamp.writeText(fingerprint)
        }
        return dir
    }

    /** The shipped bundle's fingerprint (the SHA256SUM the bundle build writes). */
    private fun bundleFingerprint(context: Context): String = runCatching {
        context.assets.open("texbundle/SHA256SUM").bufferedReader().readText().trim()
    }.getOrDefault("")

    /** Unpacks `assets/texbundle` into app storage, re-doing it whenever the
     *  shipped bundle changes (the stamp records the bundle's fingerprint, so an
     *  app update that carries new files actually replaces the extracted copy). */
    fun bundleDir(context: Context): File {
        val target = File(context.filesDir, "texbundle")
        val stamp = File(target, ".unpacked")
        val fingerprint = bundleFingerprint(context)
        if (stamp.isFile && stamp.readText() == fingerprint) return target
        target.deleteRecursively()
        target.mkdirs()
        unpack(context, "texbundle", target)
        stamp.writeText(fingerprint)
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
