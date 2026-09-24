package com.bookreader.djvu

import java.io.ByteArrayOutputStream

private fun record(children: Int, title: String, url: String): ByteArray {
    val out = ByteArrayOutputStream()
    out.write(children)
    for (value in listOf(title, url)) {
        val bytes = value.toByteArray(Charsets.UTF_8)
        out.write((bytes.size ushr 16) and 255)
        out.write((bytes.size ushr 8) and 255)
        out.write(bytes.size and 255)
        out.write(bytes)
    }
    return out.toByteArray()
}

private fun nav(vararg records: ByteArray) = byteArrayOf((records.size ushr 8).toByte(), records.size.toByte()) +
    records.fold(ByteArray(0)) { all, next -> all + next }

fun main() {
    val ids = listOf("cover.djvu", "p0002.djvu", "p+003.djvu")
    val titles = listOf("封面", "第一章", "附录 A")
    fun resolve(value: String) = DjvuOutline.resolvePage(value, ids, titles)
    check(resolve("#1") == 0 && resolve("#3") == 2)
    check(resolve("#0") == -1 && resolve("#4") == -1)
    check(resolve("#p0002.djvu") == 1)
    check(resolve("#第一章") == 1 && resolve("#%E9%99%84%E5%BD%95%20A") == 2)
    check(resolve("#p+003.djvu") == 2 && resolve("#p%2B003.djvu") == 2)
    check(resolve("https://example.org") == -1)
    check(DjvuOutline.resolvePage("#2", listOf("2", "foo"), listOf("", "")) == 0)
    val tree = DjvuOutline.parse(nav(record(2, "目录组", ""), record(0, "第\"一章", "#p0002.djvu"),
        record(0, "附录", "#3"), record(0, "外部链接", "https://example.org")), ::resolve)
    check(tree.size == 2 && tree[0].children.size == 2)
    check(tree[0].page == -1 && tree[0].children[0].page == 1 && tree[0].children[1].page == 2)
    check(tree[1].page == -1)
    check("第\\\"一章" in DjvuOutline.json(tree))
    check(DjvuOutline.parse(byteArrayOf(0, 0), ::resolve).isEmpty())
    check(runCatching { DjvuOutline.parse(byteArrayOf(0, 1, 0, 0, 0, 50), ::resolve) }.isFailure)
    check(runCatching { DjvuOutline.parse(nav(record(2, "bad", "#1")), ::resolve) }.isFailure)
    val deep = (0..65).map { record(if (it < 65) 1 else 0, "$it", "#1") }
    check(runCatching { DjvuOutline.parse(nav(*deep.toTypedArray()), ::resolve) }.isFailure)
    println("PASS: nested NAVM, numeric/id/title/Unicode/plus targets, unresolved groups, JSON, empty and malformed outlines")
}
