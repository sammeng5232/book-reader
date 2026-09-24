package com.bookreader.djvu

import java.io.File

fun main(args: Array<String>) {
    for (path in args) {
        val document = DjvuDocument()
        val metadata = document.open(path)
        check(document.pageCount > 0)
        if (File(path).name == "outline-regression.djvu") {
            check(document.pageCount == 295)
            check(document.outline.size == 2)
            check(document.outline[0].children.map { it.page } == listOf(0, 147))
            check(document.outline[1].page == 294)
            check(document.outlineError == null)
        } else check(document.outline.isEmpty())
        println("PASS: ${File(path).name} pages=${document.pageCount} outline=${document.outline.size} error=${document.outlineError}")
        check(metadata.contains("\"outline\":"))
        document.close()
        check(document.pageCount == 0 && document.outline.isEmpty())
    }
}
