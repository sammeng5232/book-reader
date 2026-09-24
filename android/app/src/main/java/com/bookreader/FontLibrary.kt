package com.bookreader

import android.content.Context
import android.webkit.WebResourceResponse
import org.json.JSONObject
import java.io.File
import java.util.Locale

/** Fonts are private user files, not Microsoft font payloads redistributed in the APK. */
class FontLibrary(private val context: Context) {
    data class Entry(
        val id: String,
        val name: String,
        val family: String,
        val file: String = "",
        val style: String = "normal",
        val weight: Int = 400,
        val italic: Boolean = false,
        val scripts: Set<String> = emptySet(),
        val source: String = "本机个人字体",
        val bundled: Boolean = false,
        val aliases: List<String> = emptyList(),
        val weightRange: List<Int> = emptyList(),
    )

    private val directory get() = File(context.filesDir, "fonts")
    @Volatile private var loadedStamp = ""
    @Volatile private var userEntries: List<Entry> = emptyList()

    @Synchronized
    fun reload(force: Boolean = false) {
        val catalog = File(directory, "catalog.json")
        val stamp = "${catalog.lastModified()}:${catalog.length()}"
        if (!force && stamp == loadedStamp) return
        val entries = runCatching {
            val array = JSONObject(catalog.readText(Charsets.UTF_8)).optJSONArray("fonts")
            val out = mutableListOf<Entry>()
            if (array != null) for (i in 0 until array.length()) {
                val item = array.optJSONObject(i) ?: continue
                val id = item.optString("id")
                val file = item.optString("file")
                val family = item.optString("family")
                if (!ID.matches(id) || !FILE.matches(file) || family.isBlank() || family.length > 160) continue
                if (family.any { it.code < 32 }) continue
                val path = containedFile(file) ?: continue
                if (!path.isFile || path.length() == 0L) continue
                val scripts = item.optJSONArray("scripts")?.let { a ->
                    (0 until a.length()).map { a.optString(it) }.filter { it in SCRIPTS }.toSet()
                } ?: emptySet()
                val aliases = item.optJSONArray("aliases")?.let { a ->
                    (0 until a.length()).map { a.optString(it) }.filter { it.isNotBlank() }
                } ?: emptyList()
                val range = item.optJSONArray("weightRange")?.takeIf { it.length() == 2 }?.let {
                    listOf(it.optInt(0, 400).coerceIn(100, 900), it.optInt(1, 400).coerceIn(100, 900))
                }?.takeIf { it[0] < it[1] } ?: emptyList()
                val italic = item.optBoolean("italic") || Regex("italic|oblique", RegexOption.IGNORE_CASE)
                    .containsMatchIn(item.optString("style"))
                out.add(Entry(id, item.optString("name", family), family, file,
                    item.optString("style", "normal"), item.optInt("weight", 400).coerceIn(100, 900),
                    italic, scripts, item.optString("source", "本机个人字体"), aliases = aliases, weightRange = range))
            }
            out.distinctBy { it.id }
        }.getOrDefault(emptyList())
        userEntries = entries
        loadedStamp = stamp
    }

    fun all(): List<Entry> {
        reload()
        return BUILTINS + userEntries
    }

    fun families(script: String, showAll: Boolean = false): List<Entry> = all()
        .filter { showAll || script in it.scripts }
        .groupBy { it.family }
        .values.map { group -> group.minBy { kotlin.math.abs(it.weight - 400) + if (it.italic) 1000 else 0 } }
        .sortedWith(compareBy<Entry> { !it.bundled }.thenBy { it.name.lowercase(Locale.ROOT) })

    fun displayName(family: String): String =
        if (family.isBlank()) "自动 / Auto" else all().firstOrNull { it.family == family }?.name ?: family

    private fun containedFile(name: String): File? = runCatching {
        val root = directory.canonicalFile
        val file = File(root, name).canonicalFile
        file.takeIf { it.parentFile == root }
    }.getOrNull()

    /** Only manifest ids or built-in filenames are accepted; never arbitrary paths. */
    fun serve(token: String): WebResourceResponse? {
        if (token.startsWith("user/")) {
            reload()
            val id = token.removePrefix("user/")
            val entry = userEntries.firstOrNull { it.id == id } ?: return null
            val file = containedFile(entry.file)?.takeIf { it.isFile } ?: return null
            val mime = if (file.extension.equals("otf", true)) "font/otf" else "font/ttf"
            return runCatching { WebResourceResponse(mime, null, file.inputStream()) }.getOrNull()
        }
        val file = BUNDLED_FILES[token] ?: return null
        return runCatching {
            WebResourceResponse("font/otf", null, context.assets.open("texbundle/$file"))
        }.getOrNull()
    }

    private fun cssQuote(value: String) = "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"

    fun css(): String {
        reload()
        val faces = BUILTINS.filter { it.file.isNotEmpty() } + userEntries
        return faces.joinToString("\n") { entry ->
            val url = if (entry.bundled) "/__er_fonts/${entry.file}" else "/__er_fonts/user/${entry.id}"
            val format = if (entry.file.endsWith(".otf", true)) "opentype" else "truetype"
            "@font-face{font-family:${cssQuote(entry.family)};src:url(${cssQuote(url)}) format('$format');" +
                "font-weight:${if (entry.weightRange.size == 2) entry.weightRange.joinToString(" ") else entry.weight.toString()};font-style:${if (entry.italic) "italic" else "normal"};font-display:swap;}"
        }
    }

    /** Metadata is passed to reader.js so it can construct unicode-range aliases. */
    fun fontFaces(): org.json.JSONArray = org.json.JSONArray().apply {
        all().filter { it.file.isNotEmpty() }.forEach { entry ->
            put(JSONObject().put("family", entry.family).put("weight", entry.weight)
                .put("weightRange", org.json.JSONArray(entry.weightRange)).put("italic", entry.italic).put("style", if (entry.italic) "italic" else "normal")
                .put("scripts", org.json.JSONArray(entry.scripts.toList()))
                .put("url", if (entry.bundled) "/__er_fonts/${entry.file}" else "/__er_fonts/user/${entry.id}")
                .put("format", if (entry.file.endsWith(".otf", true)) "opentype" else "truetype"))
        }
    }

    companion object {
        val SCRIPTS = setOf("latin", "hans", "hant", "japanese", "korean")
        private val ID = Regex("[A-Za-z0-9_-]{1,96}")
        private val FILE = Regex("[A-Za-z0-9_.-]+\\.(?:ttf|otf)", RegexOption.IGNORE_CASE)
        private val BUNDLED_FILES = mapOf(
            "song.otf" to "FandolSong-Regular.otf", "song-bold.otf" to "FandolSong-Bold.otf",
            "hei.otf" to "FandolHei-Regular.otf", "kai.otf" to "FandolKai-Regular.otf",
            "fang.otf" to "FandolFang-Regular.otf", "termes.otf" to "texgyretermes-regular.otf",
        )
        private val CJK = setOf("latin", "hans", "hant")
        private val BUILTINS = listOf(
            Entry("system-serif", "系统衬线 / Serif", "serif", scripts=SCRIPTS, source="Android 系统", bundled=true),
            Entry("system-sans", "系统无衬线 / Sans", "sans-serif", scripts=SCRIPTS, source="Android 系统", bundled=true),
            Entry("system-mono", "系统等宽 / Monospace", "monospace", scripts=SCRIPTS, source="Android 系统", bundled=true),
            Entry("song", "Fandol 宋体", "ER Fandol Song", "song.otf", scripts=CJK, source="APK 内置", bundled=true),
            Entry("song-bold", "Fandol 宋体", "ER Fandol Song", "song-bold.otf", weight=700, scripts=CJK, source="APK 内置", bundled=true),
            Entry("hei", "Fandol 黑体", "ER Fandol Hei", "hei.otf", scripts=CJK, source="APK 内置", bundled=true),
            Entry("kai", "Fandol 楷体", "ER Fandol Kai", "kai.otf", scripts=CJK, source="APK 内置", bundled=true),
            Entry("fang", "Fandol 仿宋", "ER Fandol Fang", "fang.otf", scripts=CJK, source="APK 内置", bundled=true),
            Entry("termes", "TeX Gyre Termes", "ER Termes", "termes.otf", scripts=setOf("latin"), source="APK 内置", bundled=true),
        )
    }
}
