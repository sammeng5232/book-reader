// djvutool on the phone: DjVu document access for Book Reader's Android app.
//
// A straight port of djvutool/Program.cs, which the Windows app still runs as a
// helper process and which is the oracle this file is checked against.  The serve
// protocol is gone -- Python calls these methods directly through Chaquopy (see
// src/main/python/djvu_bridge.py) -- but every operation answers exactly what the
// corresponding request answered: the same JSON, the same image bytes, and the same
// binary `layers` reply that djvupdf.py parses.
package com.bookreader.djvu

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import java.io.ByteArrayOutputStream
import java.io.File
import java.util.zip.CRC32
import java.util.zip.Deflater
import kotlin.math.floor

// ----------------------------------------------------------------------
// IFF
// ----------------------------------------------------------------------
internal class Chunk {
    var id: String = ""             // "FORM" or a leaf id
    var form: String? = null        // secondary id for FORM chunks
    var offset: Int = 0             // start of the chunk header
    var dataOffset: Int = 0         // start of the payload (after the secondary id for FORMs)
    var length: Int = 0             // payload length
    var kids: List<Chunk>? = null

    fun find(id: String): Chunk? = kids?.firstOrNull { it.id == id }

    companion object {
        fun parse(b: ByteArray, off0: Int, end: Int): List<Chunk> {
            val list = ArrayList<Chunk>()
            var off = off0
            while (off + 8 <= end) {
                val id = String(b, off, 4, Charsets.US_ASCII)
                var size = ((b[off + 4].toInt() and 0xff) shl 24) or ((b[off + 5].toInt() and 0xff) shl 16) or
                    ((b[off + 6].toInt() and 0xff) shl 8) or (b[off + 7].toInt() and 0xff)
                if (size < 0 || off + 8 + size > end) size = maxOf(0, end - off - 8)
                val c = Chunk()
                c.id = id
                c.offset = off
                c.dataOffset = off + 8
                c.length = size
                if (id == "FORM" && size >= 4) {
                    c.form = String(b, off + 8, 4, Charsets.US_ASCII)
                    c.dataOffset = off + 12
                    c.length = size - 4
                    c.kids = parse(b, off + 12, off + 8 + size)
                }
                list.add(c)
                off += 8 + size + (size and 1)
            }
            return list
        }
    }
}

internal class Component {
    var id: String? = null
    var title: String? = null
    var kind: Int = 0               // 0 include, 1 page, 2 thumbnails
    var bytes: ByteArray? = null
    var form: Chunk? = null
}

internal class PageInfo {
    var width = 0
    var height = 0
    var dpi = 300
    var rotation = 0                // rotation: 0, 90, 180, 270 (clockwise)
    var hasText = false
    var hasColor = false
}

// ----------------------------------------------------------------------
// Document
// ----------------------------------------------------------------------
/**
 * One open DjVu document.  Create it, [open] a file, ask for [infoJson], [textJson],
 * [allTextJson], [render] or [layers], then [close].
 *
 * Instances are not thread-safe; the Python side keeps one per book and serialises
 * its calls, exactly as it serialised requests to the helper process on Windows.
 */
class DjvuDocument {

    private var path: String = ""
    private val components = ArrayList<Component>()
    private val pages = ArrayList<Component>()
    private val byId = HashMap<String, Component>()
    private val dicts = HashMap<String, JB2Dict>()
    private var outlineJson = "[]"
    private var truncated = false
    private var opened = false

    /** Reads [file] and answers the same JSON the `open` request answered. */
    fun open(file: String): String {
        close()
        path = file
        val b = File(file).readBytes()
        if (b.size < 16 || String(b, 0, 4, Charsets.US_ASCII) != "AT&T") {
            throw InvalidDataException("not a DjVu file")
        }
        val top = Chunk.parse(b, 4, b.size)
        if (top.isEmpty() || top[0].id != "FORM") throw InvalidDataException("no top-level FORM")
        val root = top[0]
        // a partial download still parses; its top-level FORM promises more than the file holds
        val declared = ((b[8].toLong() and 0xff) shl 24) or ((b[9].toLong() and 0xff) shl 16) or
            ((b[10].toLong() and 0xff) shl 8) or (b[11].toLong() and 0xff)
        truncated = 12 + declared > b.size
        when (root.form) {
            "DJVU", "DJVI" -> {
                val c = Component()
                c.id = File(file).name
                c.kind = if (root.form == "DJVU") 1 else 0
                c.bytes = b
                c.form = root
                add(c)
            }
            "DJVM" -> loadMultipage(b, root)
            else -> throw InvalidDataException("unsupported DjVu form " + root.form)
        }
        for (c in components) if (c.kind == 1) pages.add(c)
        if (pages.isEmpty()) throw InvalidDataException("the document has no pages")
        opened = true
        return infoJson()
    }

    /** Frees every decoded page and shape dictionary. */
    fun close() {
        components.clear()
        pages.clear()
        byId.clear()
        dicts.clear()
        outlineJson = "[]"
        truncated = false
        opened = false
    }

    val pageCount: Int get() = pages.size

    private fun need(): DjvuDocument {
        if (!opened) throw IllegalStateException("no document is open")
        return this
    }

    private fun add(c: Component) {
        components.add(c)
        val id = c.id
        if (id != null && !byId.containsKey(id)) byId[id] = c
    }

    private fun loadMultipage(b: ByteArray, root: Chunk) {
        val dirm = root.find("DIRM") ?: throw InvalidDataException("multipage document without DIRM")
        var p = dirm.dataOffset
        val flags = b[p].toInt() and 0xff
        val bundled = (flags and 0x80) != 0
        val n = ((b[p + 1].toInt() and 0xff) shl 8) or (b[p + 2].toInt() and 0xff)
        p += 3
        if (bundled) p += 4 * n                   // offsets: the FORM children are walked instead
        val dir = Bzz.decode(b, p, dirm.dataOffset + dirm.length - p)
        val q = intArrayOf(3 * n)                 // sizes (INT24 each) are not needed
        val kinds = IntArray(n)
        for (i in 0 until n) kinds[i] = dir[q[0] + i].toInt() and 0xff
        q[0] += n
        val ids = arrayOfNulls<String>(n)
        val titles = arrayOfNulls<String>(n)
        for (i in 0 until n) {
            ids[i] = readZ(dir, q)
            if ((kinds[i] and 0x80) != 0) readZ(dir, q)                        // name
            titles[i] = if ((kinds[i] and 0x40) != 0) readZ(dir, q) else null  // title
        }
        val forms = ArrayList<Chunk>()
        for (c in root.kids ?: emptyList()) if (c.id == "FORM") forms.add(c)
        val dirName = File(path).absoluteFile.parent ?: "."
        for (i in 0 until n) {
            val c = Component()
            c.id = ids[i]
            c.title = titles[i]
            c.kind = kinds[i] and 0x3f
            if (bundled) {
                if (i >= forms.size) break
                c.bytes = b
                c.form = forms[i]
            } else {
                val f = File(dirName, ids[i] ?: "")
                if (f.isFile) {
                    val fb = f.readBytes()
                    val t = Chunk.parse(fb, 4, fb.size)
                    if (t.isNotEmpty() && t[0].id == "FORM") {
                        c.bytes = fb
                        c.form = t[0]
                    }
                }
            }
            add(c)
        }
        val navm = root.find("NAVM")
        if (navm != null) {
            outlineJson = try {
                parseOutline(Bzz.decode(b, navm.dataOffset, navm.length))
            } catch (e: Exception) {
                "[]"
            }
        }
    }

    private fun readZ(d: ByteArray, q: IntArray): String {
        val s = q[0]
        while (q[0] < d.size && d[q[0]].toInt() != 0) q[0]++
        val r = String(d, s, q[0] - s, Charsets.UTF_8)
        q[0]++
        return r
    }

    // NAVM: count, then pre-order records {nChildren, INT24 len, title, INT24 len, url}
    private fun parseOutline(d: ByteArray): String {
        if (d.size < 2) return "[]"
        val count = ((d[0].toInt() and 0xff) shl 8) or (d[1].toInt() and 0xff)
        val cur = Cursor(2, count)
        val sb = StringBuilder()
        sb.append('[')
        var first = true
        while (cur.remaining > 0 && cur.q < d.size) {
            if (!first) sb.append(',')
            first = false
            outlineRecord(d, cur, sb)
        }
        sb.append(']')
        return sb.toString()
    }

    private class Cursor(var q: Int, var remaining: Int)

    private fun outlineRecord(d: ByteArray, cur: Cursor, sb: StringBuilder) {
        val kids = d[cur.q++].toInt() and 0xff
        val tl = ((d[cur.q].toInt() and 0xff) shl 16) or ((d[cur.q + 1].toInt() and 0xff) shl 8) or
            (d[cur.q + 2].toInt() and 0xff)
        cur.q += 3
        val title = String(d, cur.q, minOf(tl, d.size - cur.q), Charsets.UTF_8)
        cur.q += tl
        val ul = ((d[cur.q].toInt() and 0xff) shl 16) or ((d[cur.q + 1].toInt() and 0xff) shl 8) or
            (d[cur.q + 2].toInt() and 0xff)
        cur.q += 3
        val url = String(d, cur.q, minOf(ul, d.size - cur.q), Charsets.UTF_8)
        cur.q += ul
        cur.remaining--
        sb.append("{\"t\":").append(Json.str(title)).append(",\"p\":").append(resolvePage(url)).append(",\"c\":[")
        var i = 0
        while (i < kids && cur.remaining > 0 && cur.q < d.size) {
            if (i > 0) sb.append(',')
            outlineRecord(d, cur, sb)
            i++
        }
        sb.append("]}")
    }

    /** "#12" (page number), "#p0012.djvu" (component id or title) -> 0-based page, or -1 */
    private fun resolvePage(url: String): Int {
        if (url.isEmpty() || url[0] != '#') return -1
        val key = url.substring(1)
        for (i in pages.indices) if (pages[i].id == key || pages[i].title == key) return i
        val n = key.trim().toIntOrNull()
        if (n != null && n >= 1 && n <= pages.size) return n - 1
        return -1
    }

    private fun info(page: Int): PageInfo {
        val c = pages[page]
        val pi = PageInfo()
        val form = c.form ?: return pi
        val b = c.bytes ?: return pi
        val info = form.find("INFO")
        if (info != null && info.length >= 4) {
            val o = info.dataOffset
            pi.width = ((b[o].toInt() and 0xff) shl 8) or (b[o + 1].toInt() and 0xff)
            pi.height = ((b[o + 2].toInt() and 0xff) shl 8) or (b[o + 3].toInt() and 0xff)
            if (info.length >= 8) {
                val dpi = (b[o + 6].toInt() and 0xff) or ((b[o + 7].toInt() and 0xff) shl 8)   // little-endian
                if (dpi > 0) pi.dpi = dpi
            }
            if (info.length >= 10) {
                pi.rotation = when (b[o + 9].toInt() and 7) {
                    6 -> 270            // 90 degrees counter-clockwise
                    2 -> 180
                    5 -> 90             // 90 degrees clockwise
                    else -> 0
                }
            }
        }
        for (k in form.kids ?: emptyList()) {
            if (k.id == "TXTz" || k.id == "TXTa") pi.hasText = true
            if (k.id == "BG44" || k.id == "FG44" || k.id == "BGjp" || k.id == "FGjp") pi.hasColor = true
        }
        return pi
    }

    private fun dictionary(id: String): JB2Dict? {
        dicts[id]?.let { return it }
        val c = byId[id] ?: return null
        val form = c.form ?: return null
        val bytes = c.bytes ?: return null
        val djbz = form.find("Djbz") ?: return null
        var parent: JB2Dict? = null
        val incl = form.find("INCL")
        if (incl != null) {
            parent = dictionary(String(bytes, incl.dataOffset, incl.length, Charsets.UTF_8).trim('\u0000', ' '))
        }
        val d = JB2Decoder.decodeDict(bytes, djbz.dataOffset, djbz.length, parent)
        dicts[id] = d
        return d
    }

    // ---- rendering ----------------------------------------------------------------
    /**
     * format: "jpg" (24-bit JPEG) or "png" (8-bit grey PNG, or 24-bit when the page has colour);
     * the caller decides, because the EPUB manifest declares each page image's type up front.
     */
    fun render(page: Int, maxWidth: Int, want: String): ByteArray {
        need()
        val l = decodeLayers(page)
        val pi = l.info
        val w = l.w
        val h = l.h
        val mask = l.mask
        val palette = l.palette
        val bgRgb = l.bgRgb
        val fgRgb = l.fgRgb
        val color = bgRgb != null || fgRgb != null || paletteHasColor(palette)
        // ---- compose at the output size ----
        val r0 = maxOf(1, (w + maxWidth - 1) / maxOf(1, maxWidth))
        val ow = (w + r0 - 1) / r0
        val oh = (h + r0 - 1) / r0
        val outRgb = ByteArray(ow * oh * 3)        // top-down
        val sample = IntArray(3)
        for (oy in 0 until oh) {
            // output row oy (top-down) covers page rows (bottom-up) [yb0, yb1)
            val yt0 = oy * r0
            val yt1 = minOf(h, yt0 + r0)
            val yb0 = h - yt1
            val yb1 = h - yt0
            for (ox in 0 until ow) {
                val x0 = ox * r0
                val x1 = minOf(w, x0 + r0)
                val total = (x1 - x0) * (yb1 - yb0)
                var covered = 0
                var fr = 0
                var fg = 0
                var fb = 0
                if (mask != null) {
                    for (y in yb0 until yb1) {
                        val row = y * w
                        for (x in x0 until x1) {
                            val v = mask[row + x].toInt() and 0xffff
                            if (v == 0) continue
                            covered++
                            if (palette != null && v - 1 < palette.size / 3) {
                                val pi3 = (v - 1) * 3
                                fb += palette[pi3].toInt() and 0xff
                                fg += palette[pi3 + 1].toInt() and 0xff
                                fr += palette[pi3 + 2].toInt() and 0xff
                            }
                        }
                    }
                }
                val cx = (x0 + x1) / 2                     // block centre, bottom-up
                val cyb = (yb0 + yb1) / 2
                var br = 255
                var bgc = 255
                var bb = 255
                if (bgRgb != null) {
                    sampleLayer(bgRgb, l.bgW, l.bgH, w, h, cx, cyb, l.bgBottomUp, sample)
                    br = sample[0]; bgc = sample[1]; bb = sample[2]
                }
                val orr: Int
                val og: Int
                val ob: Int
                if (covered == 0) {
                    orr = br; og = bgc; ob = bb
                } else {
                    var cr: Int
                    var cg: Int
                    var cb: Int
                    if (palette != null) {                 // FGbz: each glyph's own colour
                        cr = fr / covered; cg = fg / covered; cb = fb / covered
                    } else if (fgRgb != null) {
                        sampleLayer(fgRgb, l.fgW, l.fgH, w, h, cx, cyb, l.fgBottomUp, sample)
                        cr = sample[0]; cg = sample[1]; cb = sample[2]
                    } else {
                        cr = 0; cg = 0; cb = 0
                    }
                    val un = total - covered
                    orr = (br * un + cr * covered) / total
                    og = (bgc * un + cg * covered) / total
                    ob = (bb * un + cb * covered) / total
                }
                val o = (oy * ow + ox) * 3
                outRgb[o] = orr.toByte()
                outRgb[o + 1] = og.toByte()
                outRgb[o + 2] = ob.toByte()
            }
        }
        if (mask == null && bgRgb == null && fgRgb == null && l.hasSmmr) {
            throw NotSupportedException("this page uses MMR (fax) compression, which is not supported")
        }
        return encode(outRgb, ow, oh, color, want == "jpg", pi.rotation)
    }

    /** The decoded layers of one page, as DjVu stores them (shared by [render] and [layers]). */
    private class PageLayers {
        lateinit var info: PageInfo
        var w = 0
        var h = 0
        var mask: ShortArray? = null   // bottom-up; per pixel 0 = no ink, else 1 + blit colour index
        var palette: ByteArray? = null // FGbz: B, G, R triples, or null
        var bgRgb: ByteArray? = null   // R, G, B
        var fgRgb: ByteArray? = null
        var bgW = 0
        var bgH = 0
        var fgW = 0
        var fgH = 0
        var bgBottomUp = true
        var fgBottomUp = true
        var hasSmmr = false
    }

    private fun decodeLayers(page: Int): PageLayers {
        val c = pages[page]
        val pi = info(page)
        val w = pi.width
        val h = pi.height
        if (w <= 0 || h <= 0) throw InvalidDataException("page without a size")
        val b = c.bytes ?: throw InvalidDataException("page without data")
        var sjbz: Chunk? = null
        var fgbz: Chunk? = null
        var bgjp: Chunk? = null
        var fgjp: Chunk? = null
        var smmr: Chunk? = null
        val bg44 = ArrayList<Chunk>()
        val fg44 = ArrayList<Chunk>()
        var dict: JB2Dict? = null
        for (k in c.form?.kids ?: emptyList()) {
            when (k.id) {
                "Sjbz" -> sjbz = k
                "FGbz" -> fgbz = k
                "BG44" -> bg44.add(k)
                "FG44" -> fg44.add(k)
                "BGjp" -> bgjp = k
                "FGjp" -> fgjp = k
                "Smmr" -> smmr = k
                "INCL" -> {
                    val d = dictionary(String(b, k.dataOffset, k.length, Charsets.UTF_8).trim('\u0000', ' '))
                    if (d != null) dict = d
                }
            }
        }
        // ---- mask (per pixel: 0 = background, else 1 + blit colour index) ----
        var mask: ShortArray? = null
        var blitColor: IntArray? = null
        var palette: ByteArray? = null      // B, G, R triples
        if (sjbz != null) {
            val jb = JB2Decoder.decodeImage(b, sjbz.dataOffset, sjbz.length, dict)
            if (fgbz != null) {
                val pal = readPalette(b, fgbz)
                palette = pal.first
                blitColor = pal.second
            }
            val m = ShortArray(w * h)
            mask = m
            for (i in jb.blits.indices) {
                val bl = jb.blits[i]
                val s = jb.shape(bl.shape)
                var v: Short = 1
                val bc = blitColor
                if (bc != null && i < bc.size) v = (bc[i] + 1).toShort()
                for (r in 0 until s.h) {
                    val y = bl.bottom + r
                    if (y < 0 || y >= h) continue
                    val src = s.row(r)
                    val dst = y * w
                    for (x0 in 0 until s.w) {
                        if (s.d[src + x0].toInt() == 0) continue
                        val x = bl.left + x0
                        if (x in 0 until w) m[dst + x] = v
                    }
                }
            }
        }
        // ---- colour layers ----
        var bgRgb: ByteArray? = null
        var bgW = 0
        var bgH = 0
        var bgBottomUp = true
        if (bg44.isNotEmpty()) {
            val pm = IWPixmap()
            for (k in bg44) {
                try {
                    pm.decodeChunk(b, k.dataOffset, k.length)
                } catch (e: Exception) {
                    break                                  // keep what decoded so far
                }
            }
            if (pm.width > 0) {
                bgRgb = pm.toRgb(); bgW = pm.width; bgH = pm.height
            }
        } else if (bgjp != null) {
            val im = decodeJpeg(b, bgjp)
            bgRgb = im.rgb; bgW = im.w; bgH = im.h
            bgBottomUp = false
        }
        var fgRgb: ByteArray? = null
        var fgW = 0
        var fgH = 0
        var fgBottomUp = true
        if (fg44.isNotEmpty()) {
            val pm = IWPixmap()
            for (k in fg44) {
                try {
                    pm.decodeChunk(b, k.dataOffset, k.length)
                } catch (e: Exception) {
                    break
                }
            }
            if (pm.width > 0) {
                fgRgb = pm.toRgb(); fgW = pm.width; fgH = pm.height
            }
        } else if (fgjp != null) {
            val im = decodeJpeg(b, fgjp)
            fgRgb = im.rgb; fgW = im.w; fgH = im.h
            fgBottomUp = false
        }
        val l = PageLayers()
        l.info = pi; l.w = w; l.h = h; l.mask = mask; l.palette = palette
        l.bgRgb = bgRgb; l.bgW = bgW; l.bgH = bgH; l.bgBottomUp = bgBottomUp
        l.fgRgb = fgRgb; l.fgW = fgW; l.fgH = fgH; l.fgBottomUp = fgBottomUp
        l.hasSmmr = smmr != null
        return l
    }

    // ---- layers: the page's layers for a mixed-raster PDF --------------------------
    // Reply (after the 'L' tag byte; integers big-endian):
    //   u8 version (1); u32 W, u32 H (page pixels); u16 dpi; u16 rotation (0/90/180/270)
    //   u8 flags: 1 = the page has a mask, 2 = too many distinct ink colours (no planes sent)
    //   u16 nplanes, then per plane: u8 R, G, B; u8 source (0 = the solid colour R,G,B,
    //       1 = the foreground image); u32 x, y, w, h: the plane's bounding box in top-down
    //       page pixels (the whole page for source 1); u32 length; packed 1-bit rows of the
    //       box, top-down, MSB first, (w + 7) / 8 bytes per row, bit 1 = no ink, 0 = ink
    //       (usable as a PDF stencil mask as is)
    //   background: u32 w, u32 h, w*h*3 bytes RGB top-down (w = 0: none)
    //   foreground: u32 w, u32 h, RGB top-down (only when a plane uses it, else w = 0)
    // Planes are the mask split by ink colour (FGbz palette entries with the same RGB merged,
    // empty ones dropped).  With more than maxPlanes colours no planes and no layers are sent:
    // the caller composes the page with render instead.
    fun layers(page: Int, maxPlanes: Int): ByteArray {
        need()
        val l = decodeLayers(page)
        if (l.mask == null && l.bgRgb == null && l.fgRgb == null && l.hasSmmr) {
            throw NotSupportedException("this page uses MMR (fax) compression, which is not supported")
        }
        val w = l.w
        val h = l.h
        // ink value v (1 + blit colour index) -> plane; plane colours as RGB
        val planeColor = ArrayList<Int>()           // 0xRRGGBB
        val planeSource = ArrayList<Byte>()
        var valuePlane: IntArray? = null
        var tooMany = false
        val mask = l.mask
        if (mask != null) {
            var maxV = 0
            val seen = BooleanArray(65536)
            for (s in mask) {
                val v = s.toInt() and 0xffff
                if (v != 0 && !seen[v]) {
                    seen[v] = true
                    if (v > maxV) maxV = v
                }
            }
            val vp = IntArray(maxV + 1)
            valuePlane = vp
            val byColor = HashMap<Int, Int>()
            val palette = l.palette
            for (v in 1..maxV) {
                vp[v] = -1
                if (!seen[v]) continue
                val rgb: Int
                val src: Byte
                if (palette != null) {
                    val k = if (v - 1 < palette.size / 3) v - 1 else -1
                    rgb = if (k < 0) 0 else {
                        ((palette[3 * k + 2].toInt() and 0xff) shl 16) or
                            ((palette[3 * k + 1].toInt() and 0xff) shl 8) or (palette[3 * k].toInt() and 0xff)
                    }
                    src = 0
                } else if (l.fgRgb != null) {
                    rgb = 0; src = 1
                } else {
                    rgb = 0; src = 0
                }
                val key = if (src.toInt() == 1) -1 else rgb
                var p = byColor[key]
                if (p == null) {
                    p = planeColor.size
                    byColor[key] = p
                    planeColor.add(rgb)
                    planeSource.add(src)
                }
                vp[v] = p
            }
            if (planeColor.size > maxPlanes) tooMany = true
        }
        val ms = ByteArrayOutputStream()
        ms.write('L'.code)
        ms.write(1)
        u32(ms, w); u32(ms, h); u16(ms, l.info.dpi); u16(ms, l.info.rotation)
        ms.write((if (mask != null) 1 else 0) or (if (tooMany) 2 else 0))
        if (tooMany) {
            u16(ms, 0)
            u32(ms, 0); u32(ms, 0); u32(ms, 0); u32(ms, 0)
            return ms.toByteArray()
        }
        val n = planeColor.size
        // each plane's bounding box (top-down page pixels, exclusive ends); a plane painted
        // with the foreground image covers the whole page, as the image does
        val bx0 = IntArray(n)
        val by0 = IntArray(n)
        val bx1 = IntArray(n)
        val by1 = IntArray(n)
        for (p in 0 until n) {
            if (planeSource[p].toInt() == 1) {
                bx1[p] = w; by1[p] = h
            } else {
                bx0[p] = w; by0[p] = h
            }
        }
        if (mask != null && valuePlane != null) {
            for (yt in 0 until h) {
                val src = (h - 1 - yt) * w
                for (x in 0 until w) {
                    val v = mask[src + x].toInt() and 0xffff
                    if (v == 0) continue
                    val p = valuePlane[v]
                    if (x < bx0[p]) bx0[p] = x
                    if (x >= bx1[p]) bx1[p] = x + 1
                    if (yt < by0[p]) by0[p] = yt
                    if (yt >= by1[p]) by1[p] = yt + 1
                }
            }
        }
        val bits = arrayOfNulls<ByteArray>(n)
        val rowBytes = IntArray(n)
        for (p in 0 until n) {
            rowBytes[p] = (bx1[p] - bx0[p] + 7) / 8
            val arr = ByteArray(rowBytes[p] * (by1[p] - by0[p]))
            arr.fill(0xFF.toByte())
            bits[p] = arr
        }
        if (mask != null && valuePlane != null) {
            for (yt in 0 until h) {
                val src = (h - 1 - yt) * w
                for (x in 0 until w) {
                    val v = mask[src + x].toInt() and 0xffff
                    if (v == 0) continue
                    val p = valuePlane[v]
                    val rx = x - bx0[p]
                    val arr = bits[p]!!
                    val at = (yt - by0[p]) * rowBytes[p] + (rx shr 3)
                    arr[at] = (arr[at].toInt() and (0x80 ushr (rx and 7)).inv()).toByte()
                }
            }
        }
        u16(ms, n)
        var usesFg = false
        for (p in 0 until n) {
            val rgb = planeColor[p]
            ms.write((rgb shr 16) and 0xff)
            ms.write((rgb shr 8) and 0xff)
            ms.write(rgb and 0xff)
            ms.write(planeSource[p].toInt() and 0xff)
            if (planeSource[p].toInt() == 1) usesFg = true
            u32(ms, bx0[p]); u32(ms, by0[p]); u32(ms, bx1[p] - bx0[p]); u32(ms, by1[p] - by0[p])
            val arr = bits[p]!!
            u32(ms, arr.size)
            ms.write(arr, 0, arr.size)
            bits[p] = null
        }
        writeLayer(ms, l.bgRgb, l.bgW, l.bgH, l.bgBottomUp)
        if (usesFg) writeLayer(ms, l.fgRgb, l.fgW, l.fgH, l.fgBottomUp)
        else {
            u32(ms, 0); u32(ms, 0)
        }
        return ms.toByteArray()
    }

    // ---- text layer ---------------------------------------------------------------
    fun textJson(page: Int): String {
        need()
        val c = pages[page]
        val pi = info(page)
        var data: ByteArray? = null
        val bytes = c.bytes
        if (bytes != null) {
            for (k in c.form?.kids ?: emptyList()) {
                if (k.id == "TXTz") {
                    data = Bzz.decode(bytes, k.dataOffset, k.length)
                    break
                }
                if (k.id == "TXTa") {
                    data = bytes.copyOfRange(k.dataOffset, k.dataOffset + k.length)
                    break
                }
            }
        }
        val sb = StringBuilder()
        sb.append("{\"w\":").append(pi.width).append(",\"h\":").append(pi.height).append(",\"dpi\":").append(pi.dpi)
        sb.append(",\"rot\":").append(pi.rotation).append(",\"lines\":[")
        if (data != null && data.size >= 3) {
            var len = ((data[0].toInt() and 0xff) shl 16) or ((data[1].toInt() and 0xff) shl 8) or
                (data[2].toInt() and 0xff)
            len = minOf(len, data.size - 3)
            val cur = intArrayOf(3 + len)
            if (cur[0] < data.size) cur[0]++                    // version byte
            val roots = ArrayList<Zone>()
            try {
                var prev: Zone? = null
                while (cur[0] + 17 <= data.size) {
                    val z = Zone.decode(data, cur, null, prev)
                    roots.add(z)
                    prev = z
                }
            } catch (e: Exception) {
                // a damaged tail simply ends the text layer, as in the C#
            }
            val lines = ArrayList<Zone>()
            for (z in roots) z.collectLines(lines)
            var firstLine = true
            for (line in lines) {
                val words = ArrayList<Zone>()
                line.collectWords(words)
                if (words.isEmpty()) words.add(line)
                if (!firstLine) sb.append(',')
                firstLine = false
                sb.append('[')
                appendRect(sb, line, pi.height)
                sb.append(",[")
                var fw = true
                for (word in words) {
                    val start = 3 + maxOf(0, word.textStart)
                    var count = maxOf(0, minOf(word.textLength, len - word.textStart))
                    if (start > data.size) count = 0 else count = minOf(count, data.size - start)
                    // C#'s Trim() removes only whitespace (Char.IsWhiteSpace: tab..CR,
                    // U+0085, and the Zs/Zl/Zp categories).  Neither Kotlin's trim()
                    // (every char <= ' ', which would drop the U+001F a real page
                    // carries as a whole word) nor Char.isWhitespace (Java counts
                    // U+001C-U+001F as whitespace, and U+00A0 as none) matches -- so
                    // the .NET set is spelled out.
                    val t = String(data, start, count, Charsets.UTF_8).trim(::isDotNetWhiteSpace)
                    if (t.isEmpty()) continue
                    if (!fw) sb.append(',')
                    fw = false
                    sb.append('[')
                    appendRect(sb, word, pi.height)
                    sb.append(',').append(Json.str(t)).append(']')
                }
                sb.append("]]")
            }
        }
        sb.append("]}")
        return sb.toString()
    }

    /** The `alltext` request: every page's [textJson] in one array. */
    fun allTextJson(): String {
        need()
        val sb = StringBuilder("[")
        for (i in pages.indices) {
            if (i > 0) sb.append(',')
            val t = try {
                textJson(i)
            } catch (e: Exception) {
                "{\"lines\":[]}"
            }
            sb.append(t)
        }
        return sb.append(']').toString()
    }

    fun infoJson(): String {
        val sb = StringBuilder("{\"pages\":[")
        for (i in pages.indices) {
            val pi = info(i)
            if (i > 0) sb.append(',')
            sb.append("{\"w\":").append(pi.width).append(",\"h\":").append(pi.height)
                .append(",\"dpi\":").append(pi.dpi).append(",\"rot\":").append(pi.rotation)
                .append(",\"text\":").append(if (pi.hasText) "true" else "false")
                .append(",\"color\":").append(if (pi.hasColor) "true" else "false")
                .append(",\"id\":").append(Json.str(pages[i].id ?: ""))
                .append(",\"title\":").append(Json.str(pages[i].title ?: "")).append('}')
        }
        sb.append("],\"outline\":").append(outlineJson)
            .append(",\"truncated\":").append(if (truncated) "true" else "false").append('}')
        return sb.toString()
    }

    private class RgbImage(val rgb: ByteArray, val w: Int, val h: Int)

    private companion object {

        /** Char.IsWhiteSpace, as C#'s String.Trim() applies it. */
        fun isDotNetWhiteSpace(c: Char): Boolean {
            if (c in '\u0009'..'\u000D' || c == '\u0085') return true
            val t = Character.getType(c)
            return t == Character.SPACE_SEPARATOR.toInt() ||
                t == Character.LINE_SEPARATOR.toInt() ||
                t == Character.PARAGRAPH_SEPARATOR.toInt()
        }

        /** x0, y0, x1, y1 in top-down page pixels */
        fun appendRect(sb: StringBuilder, z: Zone, h: Int) {
            sb.append(z.x).append(',').append(h - (z.y + z.h)).append(',')
                .append(z.x + z.w).append(',').append(h - z.y)
        }

        fun writeLayer(ms: ByteArrayOutputStream, rgb: ByteArray?, w: Int, h: Int, bottomUp: Boolean) {
            if (rgb == null) {
                u32(ms, 0); u32(ms, 0); return
            }
            u32(ms, w); u32(ms, h)
            val stride = w * 3
            for (yt in 0 until h) ms.write(rgb, (if (bottomUp) h - 1 - yt else yt) * stride, stride)
        }

        fun u32(s: ByteArrayOutputStream, v: Int) {
            s.write((v ushr 24) and 0xff); s.write((v ushr 16) and 0xff)
            s.write((v ushr 8) and 0xff); s.write(v and 0xff)
        }

        fun u16(s: ByteArrayOutputStream, v: Int) {
            s.write((v ushr 8) and 0xff); s.write(v and 0xff)
        }

        fun paletteHasColor(pal: ByteArray?): Boolean {
            if (pal == null) return false
            var i = 0
            while (i + 2 < pal.size) {
                if (pal[i] != pal[i + 1] || pal[i + 1] != pal[i + 2]) return true
                i += 3
            }
            return false
        }

        /** bilinear sample of a reduced layer at page position (px, py_bottom_up) */
        fun sampleLayer(
            rgb: ByteArray, lw: Int, lh: Int, w: Int, h: Int, px: Int, pyb: Int, bottomUp: Boolean,
            out: IntArray,
        ) {
            val fx = (px + 0.5) * lw / w - 0.5
            val fy = (pyb + 0.5) * lh / h - 0.5
            val x0 = floor(fx).toInt()
            val y0 = floor(fy).toInt()
            val ax = fx - x0
            val ay = fy - y0
            var a0 = 0.0
            var a1 = 0.0
            var a2 = 0.0
            for (dy in 0..1) {
                for (dx in 0..1) {
                    val x = minOf(lw - 1, maxOf(0, x0 + dx))
                    var y = minOf(lh - 1, maxOf(0, y0 + dy))
                    if (!bottomUp) y = lh - 1 - y
                    val weight = (if (dx == 0) 1 - ax else ax) * (if (dy == 0) 1 - ay else ay)
                    val o = (y * lw + x) * 3
                    a0 += weight * (rgb[o].toInt() and 0xff)
                    a1 += weight * (rgb[o + 1].toInt() and 0xff)
                    a2 += weight * (rgb[o + 2].toInt() and 0xff)
                }
            }
            out[0] = (a0 + 0.5).toInt()
            out[1] = (a1 + 0.5).toInt()
            out[2] = (a2 + 0.5).toInt()
        }

        fun readPalette(b: ByteArray, k: Chunk): Pair<ByteArray, IntArray?> {
            var p = k.dataOffset
            val version = b[p].toInt() and 0xff
            val n = ((b[p + 1].toInt() and 0xff) shl 8) or (b[p + 2].toInt() and 0xff)
            p += 3
            val palette = ByteArray(n * 3)
            System.arraycopy(b, p, palette, 0, minOf(n * 3, b.size - p))
            p += n * 3
            var blitColor: IntArray? = null
            if ((version and 0x80) != 0 && p + 3 <= k.dataOffset + k.length) {
                val count = ((b[p].toInt() and 0xff) shl 16) or ((b[p + 1].toInt() and 0xff) shl 8) or
                    (b[p + 2].toInt() and 0xff)
                p += 3
                val idx = Bzz.decode(b, p, k.dataOffset + k.length - p)
                val bc = IntArray(count)
                var i = 0
                while (i < count && 2 * i + 1 < idx.size) {
                    val c = ((idx[2 * i].toInt() and 0xff) shl 8) or (idx[2 * i + 1].toInt() and 0xff)
                    bc[i] = if (c < n) c else 0
                    i++
                }
                blitColor = bc
            }
            return Pair(palette, blitColor)
        }

        /** BGjp / FGjp: a plain JPEG, decoded by the platform (System.Drawing on Windows). */
        fun decodeJpeg(b: ByteArray, k: Chunk): RgbImage {
            val bmp = BitmapFactory.decodeByteArray(b, k.dataOffset, k.length)
                ?: throw InvalidDataException("a JPEG layer could not be decoded")
            try {
                val w = bmp.width
                val h = bmp.height
                val px = IntArray(w * h)
                bmp.getPixels(px, 0, w, 0, 0, w, h)
                val rgb = ByteArray(w * h * 3)
                for (i in 0 until w * h) {
                    val v = px[i]
                    rgb[3 * i] = ((v shr 16) and 0xff).toByte()
                    rgb[3 * i + 1] = ((v shr 8) and 0xff).toByte()
                    rgb[3 * i + 2] = (v and 0xff).toByte()
                }
                return RgbImage(rgb, w, h)
            } finally {
                bmp.recycle()
            }
        }

        /**
         * The composed page as JPEG (quality 90, as the C# asks GDI+ for) or PNG.
         *
         * PNG is written here rather than through [Bitmap.compress] so that a grey page
         * stays an 8-bit grey PNG, as the C# 8bpp-indexed bitmap was: Android's encoder
         * would turn every page into 32-bit RGBA.  The pixels are the same either way.
         */
        fun encode(rgb0: ByteArray, w0: Int, h0: Int, color: Boolean, jpeg: Boolean, rotation: Int): ByteArray {
            val rot = rotate(rgb0, w0, h0, rotation)
            val rgb = rot.rgb
            val w = rot.w
            val h = rot.h
            if (jpeg) {
                val px = IntArray(w * h)
                for (i in 0 until w * h) {
                    px[i] = -0x1000000 or ((rgb[3 * i].toInt() and 0xff) shl 16) or
                        ((rgb[3 * i + 1].toInt() and 0xff) shl 8) or (rgb[3 * i + 2].toInt() and 0xff)
                }
                val bmp = Bitmap.createBitmap(px, w, h, Bitmap.Config.ARGB_8888)
                try {
                    val out = ByteArrayOutputStream()
                    if (!bmp.compress(Bitmap.CompressFormat.JPEG, 90, out)) {
                        throw InvalidDataException("the page could not be encoded as JPEG")
                    }
                    return out.toByteArray()
                } finally {
                    bmp.recycle()
                }
            }
            return if (color) Png.rgb(rgb, w, h) else Png.grey(rgb, w, h)
        }

        fun rotate(rgb: ByteArray, w: Int, h: Int, rotation: Int): RgbImage {
            if (rotation != 90 && rotation != 180 && rotation != 270) return RgbImage(rgb, w, h)
            val dw = if (rotation == 180) w else h
            val dh = if (rotation == 180) h else w
            val out = ByteArray(dw * dh * 3)
            for (dy in 0 until dh) {
                for (dx in 0 until dw) {
                    val sx: Int
                    val sy: Int
                    when (rotation) {
                        90 -> { sx = dy; sy = h - 1 - dx }
                        180 -> { sx = w - 1 - dx; sy = h - 1 - dy }
                        else -> { sx = w - 1 - dy; sy = dx }
                    }
                    val s = (sy * w + sx) * 3
                    val d = (dy * dw + dx) * 3
                    out[d] = rgb[s]
                    out[d + 1] = rgb[s + 1]
                    out[d + 2] = rgb[s + 2]
                }
            }
            return RgbImage(out, dw, dh)
        }
    }
}

// ---- text zones (bottom-up coordinates; offsets relative, per the reference) --------
internal class Zone {
    var type = 0
    var x = 0
    var y = 0
    var w = 0
    var h = 0
    var textStart = 0
    var textLength = 0
    val kids = ArrayList<Zone>()

    fun collectLines(into: MutableList<Zone>) {
        if (type == 5 || (type >= 5 && kids.isEmpty())) {
            into.add(this); return
        }
        if (kids.isEmpty() && type < 5) {
            into.add(this); return
        }
        for (k in kids) k.collectLines(into)
    }

    fun collectWords(into: MutableList<Zone>) {
        if (type == 6 || (kids.isEmpty() && type != 7)) {
            into.add(this); return
        }
        if (type == 7) {
            into.add(this); return
        }
        for (k in kids) k.collectWords(into)
    }

    companion object {
        fun decode(d: ByteArray, cur: IntArray, parent: Zone?, prev: Zone?): Zone {
            val z = Zone()
            var q = cur[0]
            z.type = d[q].toInt() and 0xff
            if (z.type < 1 || z.type > 7) throw InvalidDataException("bad text zone")
            var x = u16(d, q + 1) - 0x8000
            var y = u16(d, q + 3) - 0x8000
            val w = u16(d, q + 5) - 0x8000
            val h = u16(d, q + 7) - 0x8000
            var start = u16(d, q + 9) - 0x8000       // REF: relative text offset, not "always 0"
            val length = ((d[q + 11].toInt() and 0xff) shl 16) or ((d[q + 12].toInt() and 0xff) shl 8) or
                (d[q + 13].toInt() and 0xff)
            val nkids = ((d[q + 14].toInt() and 0xff) shl 16) or ((d[q + 15].toInt() and 0xff) shl 8) or
                (d[q + 16].toInt() and 0xff)
            q += 17
            cur[0] = q
            if (prev != null) {
                if (z.type == 1 || z.type == 4 || z.type == 5) {     // page, paragraph, line
                    x += prev.x
                    y = prev.y - (y + h)
                } else {                                            // column, word, character
                    x += prev.x + prev.w
                    y += prev.y
                }
                start += prev.textStart + prev.textLength
            } else if (parent != null) {
                x += parent.x
                y = parent.y + parent.h - (y + h)
                start += parent.textStart
            }
            z.x = x; z.y = y; z.w = w; z.h = h; z.textStart = start; z.textLength = length
            var prevKid: Zone? = null
            var i = 0
            while (i < nkids && cur[0] + 17 <= d.size) {
                val k = decode(d, cur, z, prevKid)
                z.kids.add(k)
                prevKid = k
                i++
            }
            return z
        }

        private fun u16(d: ByteArray, i: Int): Int =
            ((d[i].toInt() and 0xff) shl 8) or (d[i + 1].toInt() and 0xff)
    }
}

internal object Json {
    fun str(s: String): String {
        val sb = StringBuilder("\"")
        for (c in s) {
            when (c) {
                '"' -> sb.append("\\\"")
                '\\' -> sb.append("\\\\")
                '\n' -> sb.append("\\n")
                '\r' -> sb.append("\\r")
                '\t' -> sb.append("\\t")
                else -> if (c.code < 0x20) sb.append("\\u").append(String.format("%04x", c.code)) else sb.append(c)
            }
        }
        return sb.append('"').toString()
    }
}

/**
 * A minimal PNG writer: 8-bit greyscale (colour type 0) or 8-bit RGB (colour type 2),
 * no interlacing, filter type 0 on every row.  Deflate comes from java.util.zip.
 */
internal object Png {

    fun grey(rgb: ByteArray, w: Int, h: Int): ByteArray {
        val raw = ByteArray((w + 1) * h)
        var o = 0
        for (y in 0 until h) {
            raw[o++] = 0                               // filter: none
            var s = y * w * 3
            for (x in 0 until w) {
                raw[o++] = rgb[s]                      // the C# wrote the red channel as the grey level
                s += 3
            }
        }
        return write(raw, w, h, 0)
    }

    fun rgb(rgb: ByteArray, w: Int, h: Int): ByteArray {
        val raw = ByteArray((w * 3 + 1) * h)
        var o = 0
        for (y in 0 until h) {
            raw[o++] = 0
            System.arraycopy(rgb, y * w * 3, raw, o, w * 3)
            o += w * 3
        }
        return write(raw, w, h, 2)
    }

    private fun write(raw: ByteArray, w: Int, h: Int, colorType: Int): ByteArray {
        val out = ByteArrayOutputStream(raw.size / 4 + 1024)
        out.write(byteArrayOf(0x89.toByte(), 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A))
        val ihdr = ByteArray(13)
        be32(ihdr, 0, w)
        be32(ihdr, 4, h)
        ihdr[8] = 8                                     // bit depth
        ihdr[9] = colorType.toByte()
        ihdr[10] = 0; ihdr[11] = 0; ihdr[12] = 0        // deflate, adaptive filtering, no interlace
        chunk(out, "IHDR", ihdr)
        chunk(out, "IDAT", deflate(raw))
        chunk(out, "IEND", ByteArray(0))
        return out.toByteArray()
    }

    private fun deflate(raw: ByteArray): ByteArray {
        val d = Deflater(Deflater.BEST_COMPRESSION)
        try {
            d.setInput(raw)
            d.finish()
            val out = ByteArrayOutputStream(raw.size / 4 + 64)
            val buf = ByteArray(1 shl 16)
            while (!d.finished()) {
                val n = d.deflate(buf)
                if (n > 0) out.write(buf, 0, n)
            }
            return out.toByteArray()
        } finally {
            d.end()
        }
    }

    private fun chunk(out: ByteArrayOutputStream, type: String, data: ByteArray) {
        val len = ByteArray(4)
        be32(len, 0, data.size)
        out.write(len, 0, 4)
        val t = type.toByteArray(Charsets.US_ASCII)
        out.write(t, 0, 4)
        out.write(data, 0, data.size)
        val crc = CRC32()
        crc.update(t)
        crc.update(data)
        val c = ByteArray(4)
        be32(c, 0, crc.value.toInt())
        out.write(c, 0, 4)
    }

    private fun be32(b: ByteArray, at: Int, v: Int) {
        b[at] = ((v ushr 24) and 0xff).toByte()
        b[at + 1] = ((v ushr 16) and 0xff).toByte()
        b[at + 2] = ((v ushr 8) and 0xff).toByte()
        b[at + 3] = (v and 0xff).toByte()
    }
}
