// DjVu codecs written from the DjVu v3 specification (LizardTech, 2005).
// Where the specification's pseudo-code is ambiguous or differs from the reference
// decoder that produced real-world files, the reference behaviour is followed; each
// such place is marked "REF:".
//
// A straight port of djvutool/Codecs.cs, which the Windows app still runs and which
// is the oracle this file is checked against.  C# `uint`/`ushort` arithmetic is
// signed `Int` here, so every logical shift is `ushr` and every byte read that feeds
// a number is masked with `and 0xff`.
package com.bookreader.djvu

import java.io.ByteArrayOutputStream

/** What the C# throws as `System.IO.InvalidDataException`; the name reaches the caller. */
internal class InvalidDataException(message: String) : Exception(message)

/** What the C# throws as `System.NotSupportedException`. */
internal class NotSupportedException(message: String) : Exception(message)

// ======================================================================
// Z'-Coder (spec Appendix 3)
// ======================================================================
internal class Zp(private val buf: ByteArray, offset: Int, length: Int) {

    private var pos = offset
    private val end = minOf(buf.size, offset + maxOf(0, length))
    private var a = 0
    private var code: Int
    private var fence: Int
    private var buffer = 0
    private var scount = 0

    init {
        code = next() shl 8
        code = code or next()
        scount = 0
        buffer = 0
        preload()
        fence = if (code >= 0x8000) 0x7fff else code
    }

    /** Past the end of the chunk every further bit is 1 (spec 12.1). */
    private fun next(): Int = if (pos < end) buf[pos++].toInt() and 0xff else 0xff

    private fun preload() {
        while (scount <= 24) {
            buffer = (buffer shl 8) or next()
            scount += 8
        }
    }

    fun decode(ctx: ByteArray, i: Int): Int {
        val c = ctx[i].toInt() and 0xff
        val z = a + P[c]
        if (z <= fence) {
            a = z
            return c and 1
        }
        return decodeSub(ctx, i, c, z)
    }

    private fun decodeSub(ctx: ByteArray, i: Int, c: Int, z0: Int): Int {
        var z = z0
        val bit = c and 1
        val d = 0x6000 + ((z + a) shr 2)
        if (z > d) z = d
        // REF: the spec's Figure 1 takes the MPS branch only when C > Z; the
        // reference decoder takes LPS only when Z > C, i.e. MPS on equality.
        if (z > code) {
            z = 0x10000 - z
            a += z
            code += z
            ctx[i] = DN[c]
            val shift = ffz(a)
            scount -= shift
            a = (a shl shift) and 0xffff
            code = ((code shl shift) and 0xffff) or ((buffer ushr scount) and ((1 shl shift) - 1))
            if (scount < 16) preload()
            fence = if (code >= 0x8000) 0x7fff else code
            return bit xor 1
        }
        if (a >= M[c]) ctx[i] = UP[c]
        scount -= 1
        a = (z shl 1) and 0xffff
        code = ((code shl 1) and 0xffff) or ((buffer ushr scount) and 1)
        if (scount < 16) preload()
        fence = if (code >= 0x8000) 0x7fff else code
        return bit
    }

    private fun decodeSimple(z0: Int): Int {
        var z = z0
        if (z > code) {
            z = 0x10000 - z
            a += z
            code += z
            val shift = ffz(a)
            scount -= shift
            a = (a shl shift) and 0xffff
            code = ((code shl shift) and 0xffff) or ((buffer ushr scount) and ((1 shl shift) - 1))
            if (scount < 16) preload()
            fence = if (code >= 0x8000) 0x7fff else code
            return 1
        }
        scount -= 1
        a = (z shl 1) and 0xffff
        code = ((code shl 1) and 0xffff) or ((buffer ushr scount) and 1)
        if (scount < 16) preload()
        fence = if (code >= 0x8000) 0x7fff else code
        return 0
    }

    // REF: there are two context-free decoders.  BZZ's raw bits use a/2; the
    // spec's Figure 2 (3a/8) is the one IW44 uses for signs and mantissas.
    fun passthrough(): Int = decodeSimple(0x8000 + (a shr 1))

    fun iwPassthrough(): Int = decodeSimple(0x8000 + ((a + a + a) shr 3))

    companion object {
        private val P = IntArray(256)
        private val M = IntArray(256)
        private val UP = ByteArray(256)
        private val DN = ByteArray(256)
        private val FFZT = IntArray(256)        // leading one bits of a byte

        init {
            val t = ZpTable.ROWS
            for (i in 0 until 256) {
                P[i] = t[4 * i] and 0xffff
                M[i] = t[4 * i + 1] and 0xffff
                UP[i] = t[4 * i + 2].toByte()
                DN[i] = t[4 * i + 3].toByte()
            }
            for (i in 0 until 256) {
                var n = 0
                var j = i
                while (j and 0x80 != 0) {
                    n++
                    j = j shl 1
                }
                FFZT[i] = n
            }
        }

        private fun ffz(x: Int): Int =
            if (x >= 0xff00) FFZT[x and 0xff] + 8 else FFZT[(x shr 8) and 0xff]
    }
}

// ======================================================================
// BZZ (spec Appendix 4): Burrows-Wheeler + adaptive MTF over the Z'-Coder
// ======================================================================
internal object Bzz {

    fun decode(data: ByteArray, offset: Int, length: Int): ByteArray {
        val zp = Zp(data, offset, length)
        val ctx = ByteArray(300)
        val output = ByteArrayOutputStream()
        while (true) {
            val size = decodeRaw(zp, 24)
            if (size == 0) break
            if (size > 4096 * 1024) throw InvalidDataException("BZZ block too large")
            val block = decodeBlock(zp, ctx, size)
            output.write(block, 0, size - 1)
        }
        return output.toByteArray()
    }

    private fun decodeRaw(zp: Zp, bits: Int): Int {
        var n = 1
        val m = 1 shl bits
        while (n < m) n = (n shl 1) or zp.passthrough()
        return n - m
    }

    private fun decodeBinary(zp: Zp, ctx: ByteArray, first: Int, bits: Int): Int {
        var n = 1
        val m = 1 shl bits
        while (n < m) n = (n shl 1) or zp.decode(ctx, first + n - 1)
        return n - m
    }

    private fun decodeBlock(zp: Zp, ctx: ByteArray, size: Int): ByteArray {
        val data = ByteArray(size)
        var fshift = 0
        if (zp.passthrough() != 0) {
            fshift = 1
            if (zp.passthrough() != 0) fshift = 2
        }
        val mtf = ByteArray(256)
        for (i in 0 until 256) mtf[i] = i.toByte()
        val freq = IntArray(4)
        var fadd = 4
        var mtfno = 3
        var markerpos = -1
        for (i in 0 until size) {
            val ctxid = if (mtfno < 2) mtfno else 2       // contexts 0/1 after MTF 0/1, else 2
            if (zp.decode(ctx, ctxid) != 0) mtfno = 0
            else if (zp.decode(ctx, 3 + ctxid) != 0) mtfno = 1
            else if (zp.decode(ctx, 6) != 0) mtfno = 2 + decodeBinary(zp, ctx, 7, 1)
            else if (zp.decode(ctx, 8) != 0) mtfno = 4 + decodeBinary(zp, ctx, 9, 2)
            else if (zp.decode(ctx, 12) != 0) mtfno = 8 + decodeBinary(zp, ctx, 13, 3)
            else if (zp.decode(ctx, 20) != 0) mtfno = 16 + decodeBinary(zp, ctx, 21, 4)
            else if (zp.decode(ctx, 36) != 0) mtfno = 32 + decodeBinary(zp, ctx, 37, 5)
            else if (zp.decode(ctx, 68) != 0) mtfno = 64 + decodeBinary(zp, ctx, 69, 6)
            else if (zp.decode(ctx, 132) != 0) mtfno = 128 + decodeBinary(zp, ctx, 133, 7)
            else {
                mtfno = 256                               // end-of-block marker
                data[i] = 0
                markerpos = i
                continue
            }
            data[i] = mtf[mtfno]
            // adaptive frequency estimate decides where the symbol moves to
            fadd += fadd ushr fshift
            if (fadd > 0x10000000) {
                fadd = fadd ushr 24                       // REF: 24, as the pseudo-code says
                for (j in 0 until 4) freq[j] = freq[j] ushr 24
            }
            var fc = fadd
            if (mtfno < 4) fc += freq[mtfno]
            var k = mtfno
            while (k >= 4) {
                mtf[k] = mtf[k - 1]
                k--
            }
            while (k > 0 && fc >= freq[k - 1]) {
                mtf[k] = mtf[k - 1]
                freq[k] = freq[k - 1]                  // (the spec's "freq[j]" is a typo)
                k--
            }
            mtf[k] = data[i]
            freq[k] = fc
        }
        if (markerpos < 1 || markerpos >= size) throw InvalidDataException("BZZ: bad block marker")
        // inverse Burrows-Wheeler transform
        val count = IntArray(256)
        val posn = IntArray(size)
        for (i in 0 until size) {
            if (i == markerpos) continue
            val c = data[i].toInt() and 0xff
            posn[i] = (c shl 24) or (count[c] and 0xffffff)
            count[c]++
        }
        var last = 1
        for (i in 0 until 256) {
            val t = count[i]
            count[i] = last
            last += t
        }
        var p = 0
        last = size - 1
        while (last > 0) {
            val n = posn[p]
            val c = (n ushr 24) and 0xff
            data[--last] = c.toByte()
            p = count[c] + (n and 0xffffff)
        }
        if (p != markerpos) throw InvalidDataException("BZZ: inverse transform failed")
        return data
    }
}

// ======================================================================
// JB2 (spec Appendix 2): bi-level masks and shared shape dictionaries
// ======================================================================
internal class Bitmap1(val w: Int, val h: Int, val pad: Int) {

    val stride = w + 2 * pad
    val d = ByteArray(stride * (h + 2 * pad))

    /** row 0 is the BOTTOM row (DjVu bitmaps are stored bottom-up) */
    fun row(row: Int): Int = (row + pad) * stride + pad

    fun withPad(newPad: Int): Bitmap1 {
        if (newPad <= pad) return this
        val b = Bitmap1(w, h, newPad)
        for (r in 0 until h) System.arraycopy(d, row(r), b.d, b.row(r), w)
        return b
    }
}

internal class LibRect {
    var left = 0
    var right = 0
    var top = 0
    var bottom = 0
}

internal class JB2Dict {
    val shapes = ArrayList<Bitmap1>()
}

internal class Blit(var shape: Int = 0, var left: Int = 0, var bottom: Int = 0)

internal class JB2Decoder private constructor(
    data: ByteArray,
    offset: Int,
    length: Int,
    private val inherited: JB2Dict?,
    private val isDict: Boolean,
) {

    private val zp = Zp(data, offset, length)

    // numeric decision trees: roots (named contexts) and growable cells
    private val roots = IntArray(16)
    private var bitcells = ByteArray(4096)
    private var leftcell = IntArray(4096)
    private var rightcell = IntArray(4096)
    private var ncell = 1
    private val distRefinementFlag = ByteArray(1)
    private val offsetTypeDist = ByteArray(1)
    private val bitdist = ByteArray(1024)
    private val cbitdist = ByteArray(2048)

    private var gotStart = false
    private var imageColumns = 0
    private var imageRows = 0
    private var lastLeft = 0
    private var lastRight = 0
    private var lastBottom = 0
    private var lastRowLeft = 0
    private var lastRowBottom = 0
    private val shortList = IntArray(3)
    private var shortListPos = 0

    private val shapes = ArrayList<Bitmap1>()      // shape number -> bitmap
    private val lib2shape = ArrayList<Int>()
    private val libinfo = ArrayList<LibRect>()
    val blits = ArrayList<Blit>()
    val width: Int get() = imageColumns
    val height: Int get() = imageRows

    val dict = JB2Dict()

    fun shape(n: Int): Bitmap1 = shapes[n]

    // ---- numeric coding (spec 11.2.2 / 11.2.5) --------------------------------------
    private fun newCell(): Int {
        if (ncell >= bitcells.size) {
            val n = bitcells.size * 2
            bitcells = bitcells.copyOf(n)
            leftcell = leftcell.copyOf(n)
            rightcell = rightcell.copyOf(n)
        }
        bitcells[ncell] = 0
        leftcell[ncell] = 0
        rightcell[ncell] = 0
        return ncell++
    }

    private fun codeNum(low0: Int, high0: Int, root: Int): Int {
        var low = low0
        var high = high0
        var negative = false
        var cutoff = 0
        var phase = 1
        var range = -1
        var kind = 0
        var idx = root                              // where the current tree pointer lives
        while (range != 1) {
            var cell = if (kind == 0) roots[idx] else if (kind == 1) leftcell[idx] else rightcell[idx]
            if (cell == 0) {
                cell = newCell()
                when (kind) {
                    0 -> roots[idx] = cell
                    1 -> leftcell[idx] = cell
                    else -> rightcell[idx] = cell
                }
            }
            val decision = low >= cutoff || (high >= cutoff && zp.decode(bitcells, cell) != 0)
            kind = if (decision) 2 else 1
            idx = cell
            when (phase) {
                1 -> {
                    negative = !decision
                    if (negative) {
                        val temp = -low - 1
                        low = -high - 1
                        high = temp
                    }
                    phase = 2
                    cutoff = 1
                }
                2 -> {
                    if (!decision) {
                        phase = 3
                        range = (cutoff + 1) / 2
                        if (range == 1) cutoff = 0 else cutoff -= range / 2
                    } else cutoff += cutoff + 1
                }
                3 -> {
                    range /= 2
                    if (range != 1) {
                        if (!decision) cutoff -= range / 2 else cutoff += range / 2
                    } else if (!decision) cutoff--
                }
            }
        }
        return if (negative) -cutoff - 1 else cutoff
    }

    private fun resetNumcoder() {
        roots.fill(0)
        ncell = 1
    }

    // ---- records --------------------------------------------------------------------
    private fun run() {
        var rectype: Int
        var guard = 0
        do {
            rectype = codeNum(START_OF_DATA, END_OF_DATA, R_RECORD_TYPE)
            record(rectype)
            if (++guard > 50000000) throw InvalidDataException("JB2: runaway stream")
        } while (rectype != END_OF_DATA)
    }

    private fun initLibrary() {
        val parent = inherited ?: return
        for (s in parent.shapes) {
            // a dictionary that inherits passes the parent's shapes on, first
            if (isDict) dict.shapes.add(s)
            shapes.add(s)
            lib2shape.add(shapes.size - 1)
            libinfo.add(boundingBox(s))
        }
    }

    private fun addLibrary(shapeno: Int) {
        lib2shape.add(shapeno)
        libinfo.add(boundingBox(shapes[shapeno]))
    }

    private fun record(rectype: Int) {
        var bm: Bitmap1? = null
        var shapeno = -1
        var blit = Blit()
        var hasBlit = false
        when (rectype) {
            START_OF_DATA -> {
                val w = codeNum(0, BIGPOSITIVE, R_IMAGE_SIZE)
                val h = codeNum(0, BIGPOSITIVE, R_IMAGE_SIZE)
                zp.decode(distRefinementFlag, 0)     // eventual lossless-refinement flag
                if (isDict) {
                    if (w != 0 || h != 0) throw InvalidDataException("JB2: dictionary with a size")
                    // REF: dictionaries start their location state at zero
                    lastLeft = 1; lastRowLeft = 0; lastRowBottom = 0; lastRight = 0
                } else {
                    if (w == 0 || h == 0) throw InvalidDataException("JB2: zero image size")
                    imageColumns = w
                    imageRows = h
                    lastLeft = 1 + imageColumns; lastRowLeft = 0; lastRowBottom = imageRows; lastRight = 0
                }
                fillShortList(lastRowBottom)
                gotStart = true
                initLibrary()
            }
            NEW_MARK -> {
                bm = absoluteSize()
                decodeDirect(bm)
                blit = relativeLocation(bm.h, bm.w)
                hasBlit = true
            }
            NEW_MARK_LIBRARY_ONLY -> {
                bm = absoluteSize()
                decodeDirect(bm)
            }
            NEW_MARK_IMAGE_ONLY -> {
                bm = absoluteSize()
                decodeDirect(bm)
                blit = relativeLocation(bm.h, bm.w)
                hasBlit = true
            }
            MATCHED_REFINE, MATCHED_REFINE_IMAGE_ONLY -> {
                val match = codeNum(0, lib2shape.size - 1, R_MATCH_INDEX)
                val cbm = shapes[lib2shape[match]]
                val l = libinfo[match]
                bm = relativeSize(l.right - l.left + 1, l.top - l.bottom + 1)
                decodeCross(bm, cbm, l)
                blit = relativeLocation(bm.h, bm.w)
                hasBlit = true
            }
            MATCHED_REFINE_LIBRARY_ONLY -> {
                val match = codeNum(0, lib2shape.size - 1, R_MATCH_INDEX)
                val cbm = shapes[lib2shape[match]]
                val l = libinfo[match]
                bm = relativeSize(l.right - l.left + 1, l.top - l.bottom + 1)
                // REF: the reference page decoder codes no bitmap for this record type
                // (only dictionaries do); mirror it to stay in sync with real files.
                if (isDict) decodeCross(bm, cbm, l)
            }
            MATCHED_COPY -> {
                val match = codeNum(0, lib2shape.size - 1, R_MATCH_INDEX)
                val shape = lib2shape[match]
                val l = libinfo[match]
                val b = relativeLocation(l.top - l.bottom + 1, l.right - l.left + 1)
                b.left -= l.left
                b.bottom -= l.bottom
                b.shape = shape
                blits.add(b)
            }
            NON_MARK_DATA -> {
                bm = absoluteSize()
                decodeDirect(bm)
                val left = codeNum(1, imageColumns, R_ABS_LOC_X)
                val top = codeNum(1, imageRows, R_ABS_LOC_Y)
                blit.bottom = top - bm.h
                blit.left = left - 1
                hasBlit = true
            }
            PRESERVED_COMMENT -> {
                val size = codeNum(0, BIGPOSITIVE, R_COMMENT_LENGTH)
                for (i in 0 until size) codeNum(0, 255, R_COMMENT_BYTE)
            }
            REQUIRED_DICT_OR_RESET -> {
                if (!gotStart) {
                    val count = codeNum(0, BIGPOSITIVE, R_INHERITED)
                    val have = inherited?.shapes?.size ?: 0
                    if (count != have) {
                        throw InvalidDataException(
                            "JB2: shared dictionary has $have shapes, page needs $count")
                    }
                } else resetNumcoder()
            }
            END_OF_DATA -> Unit
            else -> throw InvalidDataException("JB2: unknown record type $rectype")
        }
        // post-record bookkeeping, in the reference decoder's order
        if (bm != null) {
            shapes.add(bm)
            shapeno = shapes.size - 1
            if (isDict) dict.shapes.add(bm)
        }
        if (rectype == NEW_MARK || rectype == NEW_MARK_LIBRARY_ONLY || rectype == MATCHED_REFINE ||
            rectype == MATCHED_REFINE_LIBRARY_ONLY
        ) {
            addLibrary(shapeno)
        }
        if (hasBlit) {
            blit.shape = shapeno
            blits.add(blit)
        }
    }

    private fun absoluteSize(): Bitmap1 {
        val w = codeNum(0, BIGPOSITIVE, R_ABS_SIZE_X)
        val h = codeNum(0, BIGPOSITIVE, R_ABS_SIZE_Y)
        if (w > 65535 || h > 65535) throw InvalidDataException("JB2: shape too large")
        return Bitmap1(w, h, PAD)
    }

    private fun relativeSize(cw: Int, ch: Int): Bitmap1 {
        val w = cw + codeNum(BIGNEGATIVE, BIGPOSITIVE, R_REL_SIZE_X)
        val h = ch + codeNum(BIGNEGATIVE, BIGPOSITIVE, R_REL_SIZE_Y)
        if (w < 0 || h < 0 || w > 65535 || h > 65535) throw InvalidDataException("JB2: bad refined size")
        return Bitmap1(w, h, PAD)
    }

    private fun fillShortList(v: Int) {
        shortList[0] = v
        shortList[1] = v
        shortList[2] = v
        shortListPos = 0
    }

    private fun updateShortList(v: Int): Int {
        if (++shortListPos == 3) shortListPos = 0
        val s = shortList
        s[shortListPos] = v
        return if (s[0] >= s[1]) {
            if (s[0] > s[2]) (if (s[1] >= s[2]) s[1] else s[2]) else s[0]
        } else {
            if (s[0] < s[2]) (if (s[1] >= s[2]) s[2] else s[1]) else s[0]
        }
    }

    private fun relativeLocation(rows: Int, columns: Int): Blit {
        if (!gotStart) throw InvalidDataException("JB2: location before start of data")
        val left: Int
        val bottom: Int
        val top: Int
        val right: Int
        val newRow = zp.decode(offsetTypeDist, 0) != 0
        if (newRow) {
            val xdiff = codeNum(BIGNEGATIVE, BIGPOSITIVE, R_REL_LOC_X_LAST)
            val ydiff = codeNum(BIGNEGATIVE, BIGPOSITIVE, R_REL_LOC_Y_LAST)
            left = lastRowLeft + xdiff
            top = lastRowBottom + ydiff
            right = left + columns - 1
            bottom = top - rows + 1
            lastLeft = left; lastRowLeft = left
            lastRight = right
            lastBottom = bottom; lastRowBottom = bottom
            fillShortList(bottom)
        } else {
            val xdiff = codeNum(BIGNEGATIVE, BIGPOSITIVE, R_REL_LOC_X_CURRENT)
            val ydiff = codeNum(BIGNEGATIVE, BIGPOSITIVE, R_REL_LOC_Y_CURRENT)
            left = lastRight + xdiff
            bottom = lastBottom + ydiff
            right = left + columns - 1
            top = bottom + rows - 1
            lastLeft = left
            lastRight = right
            lastBottom = updateShortList(bottom)
        }
        val b = Blit()
        b.bottom = bottom - 1
        b.left = left - 1
        return b
    }

    /** direct coding: 10-pixel template (2 rows above + 2 pixels to the left) */
    private fun decodeDirect(bm: Bitmap1) {
        val dw = bm.w
        var dy = bm.h - 1
        val d = bm.d
        var up2 = bm.row(dy + 2)
        var up1 = bm.row(dy + 1)
        var up0 = bm.row(dy)
        while (dy >= 0) {
            var ctx = (d[up2 - 1].toInt() shl 9) or (d[up2].toInt() shl 8) or (d[up2 + 1].toInt() shl 7) or
                (d[up1 - 2].toInt() shl 6) or (d[up1 - 1].toInt() shl 5) or (d[up1].toInt() shl 4) or
                (d[up1 + 1].toInt() shl 3) or (d[up1 + 2].toInt() shl 2) or
                (d[up0 - 2].toInt() shl 1) or d[up0 - 1].toInt()
            var dx = 0
            while (dx < dw) {
                val n = zp.decode(bitdist, ctx)
                d[up0 + dx] = n.toByte()
                dx++
                ctx = ((ctx shl 1) and 0x37a) or (d[up1 + dx + 2].toInt() shl 2) or
                    (d[up2 + dx + 1].toInt() shl 7) or n
            }
            dy--
            up2 = up1
            up1 = up0
            up0 = bm.row(dy)
        }
    }

    /** refinement coding: 11-pixel template over the new bitmap and the aligned match */
    private fun decodeCross(bm: Bitmap1, match: Bitmap1, l: LibRect) {
        val dw = bm.w
        val dh = bm.h
        val xd2c = (dw / 2 - dw + 1) - ((l.right - l.left + 1) / 2 - l.right)
        val yd2c = (dh / 2 - dh + 1) - ((l.top - l.bottom + 1) / 2 - l.top)
        if (xd2c < -15 || xd2c > 15 || yd2c < -15 || yd2c > 15) {
            throw InvalidDataException("JB2: bad refinement offset")
        }
        // the match is read around the new bitmap's extent: make sure it has border enough
        val need = maxOf(
            maxOf(2 - xd2c, 2 + dw + xd2c - match.w),
            maxOf(2 - yd2c + 2, 2 + dh + yd2c - match.h + 2),
        ) + 2
        val cbm = match.withPad(maxOf(need, PAD))
        val d = bm.d
        val c = cbm.d
        var dy = dh - 1
        var cy = dy + yd2c
        var up1 = bm.row(dy + 1)
        var up0 = bm.row(dy)
        var xup1 = cbm.row(cy + 1) + xd2c
        var xup0 = cbm.row(cy) + xd2c
        var xdn1 = cbm.row(cy - 1) + xd2c
        while (dy >= 0) {
            var ctx = (d[up1 - 1].toInt() shl 10) or (d[up1].toInt() shl 9) or (d[up1 + 1].toInt() shl 8) or
                (d[up0 - 1].toInt() shl 7) or (c[xup1].toInt() shl 6) or (c[xup0 - 1].toInt() shl 5) or
                (c[xup0].toInt() shl 4) or (c[xup0 + 1].toInt() shl 3) or (c[xdn1 - 1].toInt() shl 2) or
                (c[xdn1].toInt() shl 1) or c[xdn1 + 1].toInt()
            var dx = 0
            while (dx < dw) {
                val n = zp.decode(cbitdist, ctx)
                d[up0 + dx] = n.toByte()
                dx++
                ctx = ((ctx shl 1) and 0x636) or (d[up1 + dx + 1].toInt() shl 8) or
                    (c[xup1 + dx].toInt() shl 6) or (c[xup0 + dx + 1].toInt() shl 3) or
                    c[xdn1 + dx + 1].toInt() or (n shl 7)
            }
            up1 = up0
            up0 = bm.row(--dy)
            xup1 = xup0
            xup0 = xdn1
            xdn1 = cbm.row(--cy - 1) + xd2c
        }
    }

    companion object {
        private const val START_OF_DATA = 0
        private const val NEW_MARK = 1
        private const val NEW_MARK_LIBRARY_ONLY = 2
        private const val NEW_MARK_IMAGE_ONLY = 3
        private const val MATCHED_REFINE = 4
        private const val MATCHED_REFINE_LIBRARY_ONLY = 5
        private const val MATCHED_REFINE_IMAGE_ONLY = 6
        private const val MATCHED_COPY = 7
        private const val NON_MARK_DATA = 8
        private const val REQUIRED_DICT_OR_RESET = 9
        private const val PRESERVED_COMMENT = 10
        private const val END_OF_DATA = 11
        private const val BIGPOSITIVE = 262142
        private const val BIGNEGATIVE = -262143
        private const val PAD = 20

        private const val R_COMMENT_BYTE = 0
        private const val R_COMMENT_LENGTH = 1
        private const val R_RECORD_TYPE = 2
        private const val R_MATCH_INDEX = 3
        private const val R_ABS_LOC_X = 4
        private const val R_ABS_LOC_Y = 5
        private const val R_ABS_SIZE_X = 6
        private const val R_ABS_SIZE_Y = 7
        private const val R_IMAGE_SIZE = 8
        private const val R_INHERITED = 9
        private const val R_REL_LOC_X_CURRENT = 10
        private const val R_REL_LOC_X_LAST = 11
        private const val R_REL_LOC_Y_CURRENT = 12
        private const val R_REL_LOC_Y_LAST = 13
        private const val R_REL_SIZE_X = 14
        private const val R_REL_SIZE_Y = 15

        fun decodeDict(data: ByteArray, offset: Int, length: Int, inherited: JB2Dict?): JB2Dict {
            val d = JB2Decoder(data, offset, length, inherited, true)
            d.run()
            return d.dict
        }

        fun decodeImage(data: ByteArray, offset: Int, length: Int, inherited: JB2Dict?): JB2Decoder {
            val d = JB2Decoder(data, offset, length, inherited, false)
            d.run()
            return d
        }

        private fun boundingBox(bm: Bitmap1): LibRect {
            val r = LibRect()
            val w = bm.w
            val h = bm.h
            val d = bm.d
            r.right = w - 1
            while (r.right >= 0) {
                var any = false
                var y = 0
                while (y < h && !any) {
                    any = d[bm.row(y) + r.right].toInt() != 0
                    y++
                }
                if (any) break
                r.right--
            }
            r.top = h - 1
            while (r.top >= 0) {
                var any = false
                val row = bm.row(r.top)
                var x = 0
                while (x < w && !any) {
                    any = d[row + x].toInt() != 0
                    x++
                }
                if (any) break
                r.top--
            }
            r.left = 0
            while (r.left <= r.right) {
                var any = false
                var y = 0
                while (y < h && !any) {
                    any = d[bm.row(y) + r.left].toInt() != 0
                    y++
                }
                if (any) break
                r.left++
            }
            r.bottom = 0
            while (r.bottom <= r.top) {
                var any = false
                val row = bm.row(r.bottom)
                var x = 0
                while (x < w && !any) {
                    any = d[row + x].toInt() != 0
                    x++
                }
                if (any) break
                r.bottom++
            }
            return r
        }
    }
}

// ======================================================================
// IW44 (spec Appendix 1): progressive wavelet colour images
// ======================================================================
internal class IWMap(val iw: Int, val ih: Int) {

    val bw = (iw + 31) and 31.inv()
    val bh = (ih + 31) and 31.inv()
    val nb = (bw * bh) / 1024

    /** [block][bucket 0..63] -> 16 coefficients (lazy) */
    val blocks: Array<Array<ShortArray?>> = Array(nb) { arrayOfNulls<ShortArray>(64) }

    /** reconstruct signed 8-bit samples (row 0 = bottom, as encoded) */
    fun image(fast: Boolean): ByteArray {
        val data = ShortArray(bw * bh)
        val lift = ShortArray(1024)
        var blockno = 0
        var i = 0
        while (i < bh) {
            var j = 0
            while (j < bw) {
                lift.fill(0)
                val blk = blocks[blockno++]
                for (n1 in 0 until 64) {
                    val bucket = blk[n1] ?: continue
                    for (n2 in 0 until 16) lift[ZIGZAG[n1 * 16 + n2]] = bucket[n2]
                }
                for (ii in 0 until 32) System.arraycopy(lift, ii * 32, data, (i + ii) * bw + j, 32)
                j += 32
            }
            i += 32
        }
        if (fast) {
            backward(data, iw, ih, bw, 32, 2)
            var y = 0
            while (y < bh) {
                var jj = 0
                while (jj < bw) {
                    val p = y * bw + jj
                    val v = data[p]
                    data[p + 1] = v
                    if (y + 1 < bh) {
                        data[p + bw] = v
                        data[p + bw + 1] = v
                    }
                    jj += 2
                }
                y += 2
            }
        } else {
            backward(data, iw, ih, bw, 32, 1)
        }
        val img = ByteArray(iw * ih)
        for (r in 0 until ih) {
            for (cx in 0 until iw) {
                var x = (data[r * bw + cx] + 32) shr 6
                if (x < -128) x = -128 else if (x > 127) x = 127
                img[r * iw + cx] = x.toByte()
            }
        }
        return img
    }

    companion object {
        /**
         * coefficient n (0..1023) of a block -> position in its 32x32 tile: the index
         * bits alternate column/row from the most significant position down
         */
        val ZIGZAG = buildZigzag()

        private fun buildZigzag(): IntArray {
            val z = IntArray(1024)
            for (n in 0 until 1024) {
                var col = 0
                var row = 0
                for (b in 0 until 5) {
                    col = col or (((n shr (2 * b)) and 1) shl (4 - b))
                    row = row or (((n shr (2 * b + 1)) and 1) shl (4 - b))
                }
                z[n] = row * 32 + col
            }
            return z
        }

        private fun backward(d: ShortArray, w: Int, h: Int, rowsize: Int, begin: Int, end: Int) {
            var scale = begin shr 1
            while (scale >= end) {
                filterBv(d, 0, w, h, rowsize, scale)
                filterBh(d, 0, w, h, rowsize, scale)
                scale = scale shr 1
            }
        }

        private fun filterBv(d: ShortArray, p0: Int, w: Int, h0: Int, rowsize: Int, scale: Int) {
            var p = p0
            var y = 0
            val s = scale * rowsize
            val s3 = s + s + s
            val h = ((h0 - 1) / scale) + 1
            while (y - 3 < h) {
                run {
                    var q = p
                    val e = q + w
                    if (y >= 3 && y + 3 < h) {
                        while (q < e) {
                            val a = d[q - s] + d[q + s]
                            val b = d[q - s3] + d[q + s3]
                            d[q] = (d[q] - (((a shl 3) + a - b + 16) shr 5)).toShort()
                            q += scale
                        }
                    } else if (y < h) {
                        var q1 = if (y + 1 < h) q + s else -1
                        var q3 = if (y + 3 < h) q + s3 else -1
                        while (q < e) {
                            val a = (if (y >= 1) d[q - s].toInt() else 0) + (if (q1 >= 0) d[q1].toInt() else 0)
                            val b = (if (y >= 3) d[q - s3].toInt() else 0) + (if (q3 >= 0) d[q3].toInt() else 0)
                            d[q] = (d[q] - (((a shl 3) + a - b + 16) shr 5)).toShort()
                            if (q1 >= 0) q1 += scale
                            if (q3 >= 0) q3 += scale
                            q += scale
                        }
                    }
                }
                run {
                    var q = p - s3
                    val e = q + w
                    if (y >= 6 && y < h) {
                        while (q < e) {
                            val a = d[q - s] + d[q + s]
                            val b = d[q - s3] + d[q + s3]
                            d[q] = (d[q] + (((a shl 3) + a - b + 8) shr 4)).toShort()
                            q += scale
                        }
                    } else if (y >= 3) {
                        var q1 = if (y - 2 < h) q + s else q - s
                        while (q < e) {
                            val a = d[q - s] + d[q1]
                            d[q] = (d[q] + ((a + 1) shr 1)).toShort()
                            q += scale
                            q1 += scale
                        }
                    }
                }
                y += 2
                p += s + s
            }
        }

        private fun filterBh(d: ShortArray, p0: Int, w: Int, h: Int, rowsize0: Int, scale: Int) {
            var p = p0
            var y = 0
            val s = scale
            val s3 = s + s + s
            val rowsize = rowsize0 * scale
            while (y < h) {
                var q = p
                val e = p + w
                var a0 = 0
                var a1 = 0
                var a2 = 0
                var a3 = 0
                @Suppress("UNUSED_VALUE") var b0 = 0
                var b1 = 0
                var b2 = 0
                var b3 = 0
                if (q < e) {
                    if (q + s < e) a2 = d[q + s].toInt()
                    if (q + s3 < e) a3 = d[q + s3].toInt()
                    b3 = d[q] - ((((a1 + a2) shl 3) + (a1 + a2) - a0 - a3 + 16) shr 5)
                    b2 = b3
                    d[q] = b3.toShort()
                    q += s + s
                }
                if (q < e) {
                    a0 = a1; a1 = a2; a2 = a3
                    a3 = if (q + s3 < e) d[q + s3].toInt() else a3
                    b3 = d[q] - ((((a1 + a2) shl 3) + (a1 + a2) - a0 - a3 + 16) shr 5)
                    d[q] = b3.toShort()
                    q += s + s
                }
                if (q < e) {
                    b1 = b2; b2 = b3
                    a0 = a1; a1 = a2; a2 = a3
                    a3 = if (q + s3 < e) d[q + s3].toInt() else a3
                    b3 = d[q] - ((((a1 + a2) shl 3) + (a1 + a2) - a0 - a3 + 16) shr 5)
                    d[q] = b3.toShort()
                    d[q - s3] = (d[q - s3] + ((b1 + b2 + 1) shr 1)).toShort()
                    q += s + s
                }
                while (q + s3 < e) {
                    a0 = a1; a1 = a2; a2 = a3
                    a3 = d[q + s3].toInt()
                    b0 = b1; b1 = b2; b2 = b3
                    b3 = d[q] - ((((a1 + a2) shl 3) + (a1 + a2) - a0 - a3 + 16) shr 5)
                    d[q] = b3.toShort()
                    d[q - s3] = (d[q - s3] + ((((b1 + b2) shl 3) + (b1 + b2) - b0 - b3 + 8) shr 4)).toShort()
                    q += s + s
                }
                while (q < e) {
                    a0 = a1; a1 = a2; a2 = a3; a3 = 0
                    b0 = b1; b1 = b2; b2 = b3
                    b3 = d[q] - ((((a1 + a2) shl 3) + (a1 + a2) - a0 - a3 + 16) shr 5)
                    d[q] = b3.toShort()
                    d[q - s3] = (d[q - s3] + ((((b1 + b2) shl 3) + (b1 + b2) - b0 - b3 + 8) shr 4)).toShort()
                    q += s + s
                }
                while (q - s3 < e) {
                    b0 = b1; b1 = b2; b2 = b3
                    if (q - s3 >= p) d[q - s3] = (d[q - s3] + ((b1 + b2 + 1) shr 1)).toShort()
                    q += s + s
                }
                y += scale
                p += rowsize
            }
        }
    }
}

internal class IWCodec(private val map: IWMap) {

    private val quantLo = IntArray(16)
    private val quantHi = IntArray(10)
    private val coeffstate = ByteArray(256)
    private val bucketstate = ByteArray(16)
    private val ctxStart = ByteArray(32)
    private val ctxBucket = ByteArray(10 * 8)
    private val ctxMant = ByteArray(1)
    private val ctxRoot = ByteArray(1)
    private var curband = 0
    private var curbit = 1

    init {
        var i = 0
        var q = 0
        while (i < 4) quantLo[i++] = IW_QUANT[q++]
        repeat(4) { quantLo[i++] = IW_QUANT[q] }
        q++
        repeat(4) { quantLo[i++] = IW_QUANT[q] }
        q++
        repeat(4) { quantLo[i++] = IW_QUANT[q] }
        q++
        quantHi[0] = 0
        for (j in 1 until 10) quantHi[j] = IW_QUANT[q++]
    }

    private fun isNullSlice(band: Int): Boolean {
        if (band == 0) {
            var isNull = true
            for (i in 0 until 16) {
                val threshold = quantLo[i]
                coeffstate[i] = ZERO.toByte()
                if (threshold > 0 && threshold < 0x8000) {
                    coeffstate[i] = UNK.toByte()
                    isNull = false
                }
            }
            return isNull
        }
        val t = quantHi[band]
        return !(t > 0 && t < 0x8000)
    }

    fun codeSlice(zp: Zp): Int {
        if (curbit < 0) return 0
        if (!isNullSlice(curband)) {
            for (b in 0 until map.nb) {
                decodeBuckets(zp, curband, map.blocks[b], BAND_START[curband], BAND_SIZE[curband])
            }
        }
        quantHi[curband] = quantHi[curband] shr 1
        if (curband == 0) {
            for (i in 0 until 16) quantLo[i] = quantLo[i] shr 1
        }
        if (++curband >= 10) {
            curband = 0
            curbit++
            if (quantHi[9] == 0) {
                curbit = -1
                return 0
            }
        }
        return 1
    }

    private fun decodePrepare(fbucket: Int, nbucket: Int, blk: Array<ShortArray?>): Int {
        var bbstate = 0
        if (fbucket != 0) {
            for (buckno in 0 until nbucket) {
                var bstate = 0
                val pc = blk[fbucket + buckno]
                val cs = buckno * 16
                if (pc == null) {
                    bstate = UNK
                } else {
                    for (i in 0 until 16) {
                        val c = if (pc[i].toInt() != 0) ACTIVE else UNK
                        coeffstate[cs + i] = c.toByte()
                        bstate = bstate or c
                    }
                }
                bucketstate[buckno] = bstate.toByte()
                bbstate = bbstate or bstate
            }
        } else {
            val pc = blk[0]
            if (pc == null) {
                bbstate = UNK
            } else {
                for (i in 0 until 16) {
                    var c = coeffstate[i].toInt()
                    if (c != ZERO) c = if (pc[i].toInt() != 0) ACTIVE else UNK
                    coeffstate[i] = c.toByte()
                    bbstate = bbstate or c
                }
            }
            bucketstate[0] = bbstate.toByte()
        }
        return bbstate
    }

    private fun decodeBuckets(zp: Zp, band: Int, blk: Array<ShortArray?>, fbucket: Int, nbucket: Int) {
        var bbstate = decodePrepare(fbucket, nbucket, blk)
        if (nbucket < 16 || (bbstate and ACTIVE) != 0) {
            bbstate = bbstate or NEW
        } else if ((bbstate and UNK) != 0) {
            if (zp.decode(ctxRoot, 0) != 0) bbstate = bbstate or NEW
        }
        if ((bbstate and NEW) != 0) {
            for (buckno in 0 until nbucket) {
                if ((bucketstate[buckno].toInt() and UNK) == 0) continue
                var ctx = 0
                if (band > 0) {
                    val k0 = (fbucket + buckno) shl 2
                    val b = blk[k0 shr 4]
                    if (b != null) {
                        val k = k0 and 0xf
                        if (b[k].toInt() != 0) ctx++
                        if (b[k + 1].toInt() != 0) ctx++
                        if (b[k + 2].toInt() != 0) ctx++
                        if (ctx < 3 && b[k + 3].toInt() != 0) ctx++
                    }
                }
                if ((bbstate and ACTIVE) != 0) ctx = ctx or 4
                if (zp.decode(ctxBucket, band * 8 + ctx) != 0) {
                    bucketstate[buckno] = (bucketstate[buckno].toInt() or NEW).toByte()
                }
            }
        }
        if ((bbstate and NEW) != 0) {
            var thres = quantHi[band]
            for (buckno in 0 until nbucket) {
                if ((bucketstate[buckno].toInt() and NEW) == 0) continue
                val cs = buckno * 16
                var pc = blk[fbucket + buckno]
                if (pc == null) {
                    pc = ShortArray(16)
                    blk[fbucket + buckno] = pc
                    if (fbucket == 0) {
                        for (i in 0 until 16) {
                            if (coeffstate[cs + i].toInt() != ZERO) coeffstate[cs + i] = UNK.toByte()
                        }
                    } else {
                        for (i in 0 until 16) coeffstate[cs + i] = UNK.toByte()
                    }
                }
                var gotcha = 0
                for (i in 0 until 16) if ((coeffstate[cs + i].toInt() and UNK) != 0) gotcha++
                for (i in 0 until 16) {
                    if ((coeffstate[cs + i].toInt() and UNK) == 0) continue
                    if (band == 0) thres = quantLo[i]
                    var ctx = if (gotcha >= MAXGOTCHA) MAXGOTCHA else gotcha
                    if ((bucketstate[buckno].toInt() and ACTIVE) != 0) ctx = ctx or 8
                    if (zp.decode(ctxStart, ctx) != 0) {
                        coeffstate[cs + i] = (coeffstate[cs + i].toInt() or NEW).toByte()
                        val half = thres shr 1
                        val coeff = thres + half - (half shr 2)
                        pc[i] = (if (zp.iwPassthrough() != 0) -coeff else coeff).toShort()
                    }
                    if ((coeffstate[cs + i].toInt() and NEW) != 0) gotcha = 0
                    else if (gotcha > 0) gotcha--
                }
            }
        }
        if ((bbstate and ACTIVE) != 0) {
            var thres = quantHi[band]
            for (buckno in 0 until nbucket) {
                if ((bucketstate[buckno].toInt() and ACTIVE) == 0) continue
                val cs = buckno * 16
                val pc = blk[fbucket + buckno] ?: continue
                for (i in 0 until 16) {
                    if ((coeffstate[cs + i].toInt() and ACTIVE) == 0) continue
                    var coeff = pc[i].toInt()
                    if (coeff < 0) coeff = -coeff
                    if (band == 0) thres = quantLo[i]
                    if (coeff <= 3 * thres) {
                        coeff += thres shr 2
                        if (zp.decode(ctxMant, 0) != 0) coeff += thres shr 1
                        else coeff = coeff - thres + (thres shr 1)
                    } else {
                        if (zp.iwPassthrough() != 0) coeff += thres shr 1
                        else coeff = coeff - thres + (thres shr 1)
                    }
                    pc[i] = (if (pc[i] > 0) coeff else -coeff).toShort()
                }
            }
        }
    }

    companion object {
        private const val ZERO = 1
        private const val ACTIVE = 2
        private const val NEW = 4
        private const val UNK = 8
        private const val MAXGOTCHA = 7
        private val IW_QUANT = intArrayOf(
            0x004000, 0x008000, 0x008000, 0x010000, 0x010000, 0x010000, 0x020000, 0x020000,
            0x020000, 0x040000, 0x040000, 0x040000, 0x080000, 0x040000, 0x040000, 0x080000,
        )
        private val BAND_START = intArrayOf(0, 1, 2, 3, 4, 8, 12, 16, 32, 48)
        private val BAND_SIZE = intArrayOf(1, 1, 1, 1, 4, 4, 4, 16, 16, 16)
    }
}

/** A progressive IW44 colour (or grey) image built from successive chunks. */
internal class IWPixmap {

    private var ymap: IWMap? = null
    private var cbmap: IWMap? = null
    private var crmap: IWMap? = null
    private var ycodec: IWCodec? = null
    private var cbcodec: IWCodec? = null
    private var crcodec: IWCodec? = null
    private var cslice = 0
    private var cserial = 0
    private var crcbDelay = 0
    private var crcbHalf = 0

    val width: Int get() = ymap?.iw ?: 0
    val height: Int get() = ymap?.ih ?: 0
    val color: Boolean get() = crmap != null && crcbDelay >= 0

    fun decodeChunk(buf: ByteArray, off: Int, len: Int) {
        if (len < 2) throw InvalidDataException("IW44: short chunk")
        val serial = buf[off].toInt() and 0xff
        val slices = buf[off + 1].toInt() and 0xff
        var pos = off + 2
        if (serial != cserial) throw InvalidDataException("IW44: chunk out of order")
        val nslices = cslice + slices
        if (cserial == 0) {
            val major = buf[pos].toInt() and 0xff
            val minor = buf[pos + 1].toInt() and 0xff
            pos += 2
            val w = ((buf[pos].toInt() and 0xff) shl 8) or (buf[pos + 1].toInt() and 0xff)
            val h = ((buf[pos + 2].toInt() and 0xff) shl 8) or (buf[pos + 3].toInt() and 0xff)
            pos += 4
            var delayByte = 0
            if ((major and 0x7f) == 1 && minor >= 2) delayByte = buf[pos++].toInt() and 0xff
            crcbDelay = if (minor >= 2) delayByte and 0x7f else 0
            crcbHalf = if (minor >= 2) (if ((delayByte and 0x80) != 0) 0 else 1) else 0
            if ((major and 0x80) != 0) crcbDelay = -1
            if (w <= 0 || h <= 0) throw InvalidDataException("IW44: zero size")
            val ym = IWMap(w, h)
            ymap = ym
            ycodec = IWCodec(ym)
            if (crcbDelay >= 0) {
                val cb = IWMap(w, h)
                val cr = IWMap(w, h)
                cbmap = cb
                crmap = cr
                cbcodec = IWCodec(cb)
                crcodec = IWCodec(cr)
            }
        }
        // each chunk restarts the arithmetic decoder; the codecs' contexts carry over
        val zp = Zp(buf, pos, off + len - pos)
        var flag = 1
        val yc = ycodec ?: throw InvalidDataException("IW44: no image header")
        while (flag != 0 && cslice < nslices) {
            flag = yc.codeSlice(zp)
            val cbc = cbcodec
            val crc = crcodec
            if (crc != null && cbc != null && crcbDelay <= cslice) {
                flag = flag or cbc.codeSlice(zp)
                flag = flag or crc.codeSlice(zp)
            }
            cslice++
        }
        cserial++
    }

    /** RGB bytes, row 0 = bottom (as encoded) */
    fun toRgb(): ByteArray {
        val ym = ymap ?: return ByteArray(0)
        val w = ym.iw
        val h = ym.ih
        val rgb = ByteArray(w * h * 3)
        val y = ym.image(false)
        if (color) {
            val cb = cbmap!!.image(crcbHalf != 0)
            val cr = crmap!!.image(crcbHalf != 0)
            for (i in 0 until w * h) {
                val yy = y[i].toInt()
                val b = cb[i].toInt()
                val r = cr[i].toInt()
                val t1 = b shr 2
                val t2 = r + (r shr 1)
                val t3 = yy + 128 - t1
                val tr = yy + 128 + t2
                val tg = t3 - (t2 shr 1)
                val tb = t3 + (b shl 1)
                rgb[3 * i] = clamp(tr)
                rgb[3 * i + 1] = clamp(tg)
                rgb[3 * i + 2] = clamp(tb)
            }
        } else {
            for (i in 0 until w * h) {
                val g = clamp(127 - y[i])
                rgb[3 * i] = g
                rgb[3 * i + 1] = g
                rgb[3 * i + 2] = g
            }
        }
        return rgb
    }

    private fun clamp(v: Int): Byte = (if (v < 0) 0 else if (v > 255) 255 else v).toByte()
}
