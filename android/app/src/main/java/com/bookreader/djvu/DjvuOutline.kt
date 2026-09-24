package com.bookreader.djvu

import java.net.URLDecoder

/** An embedded NAVM bookmark. [page] is zero-based, or -1 for a group/external link. */
data class DjvuOutlineItem(val title: String, val page: Int, val children: List<DjvuOutlineItem>)

/** Independent of Android so the binary NAVM format can be regression-tested on the host. */
internal object DjvuOutline {
    fun parse(data: ByteArray, resolve: (String) -> Int): List<DjvuOutlineItem> {
        require(data.size >= 2) { "Truncated DjVu outline header" }
        var at = 2
        var remaining = ((data[0].toInt() and 255) shl 8) or (data[1].toInt() and 255)
        fun readText(): String {
            require(at <= data.size - 3) { "Truncated DjVu outline string length" }
            val size = ((data[at].toInt() and 255) shl 16) or
                ((data[at + 1].toInt() and 255) shl 8) or (data[at + 2].toInt() and 255)
            at += 3
            require(size <= data.size - at) { "Truncated DjVu outline string" }
            return String(data, at, size, Charsets.UTF_8).also { at += size }
        }
        fun record(depth: Int): DjvuOutlineItem {
            require(depth <= 64) { "DjVu outline nesting is too deep" }
            require(remaining > 0 && at < data.size) { "Truncated DjVu outline record" }
            val childCount = data[at++].toInt() and 255
            val title = readText()
            val target = readText()
            remaining--
            require(childCount <= remaining) { "DjVu outline child count exceeds its records" }
            val children = ArrayList<DjvuOutlineItem>(childCount)
            repeat(childCount) { children.add(record(depth + 1)) }
            return DjvuOutlineItem(title, resolve(target), children)
        }
        val roots = ArrayList<DjvuOutlineItem>()
        while (remaining > 0) roots.add(record(0))
        return roots
    }

    fun resolvePage(url: String, ids: List<String>, titles: List<String>): Int {
        if (!url.startsWith('#')) return -1
        val raw = url.substring(1)
        val key = runCatching { URLDecoder.decode(raw.replace("+", "%2B"), "UTF-8") }.getOrDefault(raw)
        for (index in ids.indices) if (ids[index] == key || titles.getOrNull(index) == key) return index
        val number = key.trim().toIntOrNull()
        return if (number != null && number in 1..ids.size) number - 1 else -1
    }

    fun json(nodes: List<DjvuOutlineItem>): String = nodes.joinToString(prefix = "[", postfix = "]") {
        "{\"t\":${quote(it.title)},\"p\":${it.page},\"c\":${json(it.children)}}"
    }

    private fun quote(value: String): String = buildString {
        append('"')
        for (c in value) when (c) {
            '"' -> append("\\\"")
            '\\' -> append("\\\\")
            '\n' -> append("\\n")
            '\r' -> append("\\r")
            '\t' -> append("\\t")
            else -> if (c.code < 32) append("\\u%04x".format(c.code)) else append(c)
        }
        append('"')
    }
}
