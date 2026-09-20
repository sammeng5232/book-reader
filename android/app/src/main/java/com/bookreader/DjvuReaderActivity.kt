package com.bookreader

import android.content.Context
import android.content.Intent
import android.graphics.BitmapFactory
import android.os.Bundle
import android.view.GestureDetector
import android.view.Gravity
import android.view.MotionEvent
import android.view.ScaleGestureDetector
import android.view.View
import android.view.ViewGroup
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import com.bookreader.djvu.DjvuDocument
import java.io.File
import kotlin.concurrent.thread
import kotlin.math.max
import kotlin.math.min

/**
 * Reads a DjVu file as one continuous, vertically scrolling strip of pages — like
 * scrolling a long document.  Pages are decoded on demand by the Kotlin decoder
 * (`DjvuDocument.render`) as they scroll into view; nothing is converted.
 *
 * Gestures:
 * * **scroll up / down** to move through the book (continuous, the whole point);
 * * **pinch** to zoom the strip, then it scrolls in both directions while zoomed;
 * * **double-tap** to reset the zoom.
 *
 * Pages render at fit-width (fast, ~0.5–2 s for a 600 DPI scan) and are cached, so
 * scrolling back up is instant.
 */
class DjvuReaderActivity : AppCompatActivity() {

    private lateinit var list: RecyclerView
    private lateinit var status: TextView
    private var doc: DjvuDocument? = null
    private var count = 0

    // zoom state for the whole strip
    private var scale = 1f
    private var panX = 0f
    private var panY = 0f
    private lateinit var scaleGestures: ScaleGestureDetector
    private lateinit var gestures: GestureDetector

    // decoded pages, page -> Bitmap (fit-width).  Bounded so a long book stays small.
    private val cache = object : LinkedHashMap<Int, android.graphics.Bitmap>(32, 0.75f, true) {
        override fun removeEldestEntry(e: MutableMap.MutableEntry<Int, android.graphics.Bitmap>?) =
            size > 24
    }
    private val rendering = java.util.Collections.synchronizedSet(mutableSetOf<Int>())

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val path = intent.getStringExtra(EXTRA_PATH) ?: run { finish(); return }
        title = File(path).nameWithoutExtension

        val root = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        list = RecyclerView(this)
        status = TextView(this).apply {
            textSize = 12f
            gravity = Gravity.CENTER
            setPadding(0, 6, 0, 10)
        }
        root.addView(list, LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f))
        root.addView(status)
        setContentView(root)

        scaleGestures = ScaleGestureDetector(this, ScaleGestures())
        gestures = GestureDetector(this, Gestures())
        list.setOnTouchListener { _, e ->
            scaleGestures.onTouchEvent(e)
            gestures.onTouchEvent(e)
            // When zoomed out (scale == 1) let the RecyclerView scroll normally.
            // When zoomed in we consume move events to pan instead.
            scale > 1.02f
        }
        list.addOnScrollListener(object : RecyclerView.OnScrollListener() {
            override fun onScrolled(rv: RecyclerView, dx: Int, dy: Int) {
                updateStatus()
            }
        })

        status.text = "Opening…"
        thread {
            val d = DjvuDocument()
            val ok = runCatching { d.open(path) }.isSuccess
            runOnUiThread {
                if (!ok) {
                    status.text = "Could not open the DjVu file."
                    d.close()
                    return@runOnUiThread
                }
                doc = d
                count = d.pageCount
                list.adapter = PageAdapter()
                list.layoutManager = LinearLayoutManager(this)
                updateStatus()
            }
        }
    }

    private fun currentPage(): Int {
        val lm = list.layoutManager as? LinearLayoutManager ?: return 0
        return lm.findFirstVisibleItemPosition().coerceAtLeast(0)
    }

    private fun updateStatus() {
        if (count > 0) status.text = "${currentPage() + 1} / $count"
    }

    // --------------------------------------------------------------
    // The page adapter
    // --------------------------------------------------------------
    private inner class PageAdapter : RecyclerView.Adapter<PageHolder>() {
        override fun getItemCount() = count

        override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): PageHolder {
            val iv = ImageView(parent.context).apply {
                layoutParams = ViewGroup.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)
                adjustViewBounds = true
                scaleType = ImageView.ScaleType.FIT_CENTER
                setBackgroundColor(android.graphics.Color.WHITE)
            }
            return PageHolder(iv)
        }

        override fun onBindViewHolder(holder: PageHolder, position: Int) {
            val bmp = cache[position]
            if (bmp != null) {
                holder.image.setImageBitmap(bmp)
            } else {
                holder.image.setImageDrawable(null)
                holder.image.minimumHeight = list.width       // rough placeholder height
                load(position, holder)
            }
        }

        override fun onViewRecycled(holder: PageHolder) {
            holder.image.setImageDrawable(null)
        }
    }

    private class PageHolder(val image: ImageView) : RecyclerView.ViewHolder(image)

    private fun load(page: Int, holder: PageHolder) {
        val d = doc ?: return
        if (!rendering.add(page)) return
        thread {
            val bmp = runCatching {
                val bytes = d.render(page, list.width.coerceAtLeast(720), "jpg")
                BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
            }.getOrNull()
            rendering.remove(page)
            if (bmp != null) {
                cache[page] = bmp
                runOnUiThread {
                    // only rebind if this holder still shows this page
                    val pos = holder.absoluteAdapterPosition
                    if (pos == page) holder.image.setImageBitmap(bmp)
                }
            }
        }
    }

    // --------------------------------------------------------------
    // Zoom + pan of the whole strip
    // --------------------------------------------------------------
    private fun applyTransform() {
        list.scaleX = scale
        list.scaleY = scale
        list.pivotX = 0f
        list.pivotY = 0f
        list.translationX = panX
        list.translationY = panY
    }

    private fun clampPan() {
        val w = list.width * scale
        val h = list.height * scale
        panX = if (w > list.width) min(0f, max(list.width - w, panX)) else 0f
        panY = if (h > list.height) min(0f, max(list.height - h, panY)) else 0f
    }

    private inner class ScaleGestures : ScaleGestureDetector.SimpleOnScaleGestureListener() {
        override fun onScale(d: ScaleGestureDetector): Boolean {
            val old = scale
            scale = (scale * d.scaleFactor).coerceIn(1f, 5f)
            val fx = d.focusX
            val fy = d.focusY
            panX = fx - (fx - panX) * (scale / old)
            panY = fy - (fy - panY) * (scale / old)
            clampPan()
            applyTransform()
            return true
        }
    }

    private inner class Gestures : GestureDetector.SimpleOnGestureListener() {
        override fun onDown(e: MotionEvent): Boolean = scale > 1.02f

        override fun onDoubleTap(e: MotionEvent): Boolean {
            scale = 1f; panX = 0f; panY = 0f
            applyTransform()
            return true
        }

        override fun onScroll(e1: MotionEvent?, e2: MotionEvent, dx: Float, dy: Float): Boolean {
            if (scale > 1.02f) {
                panX -= dx
                panY -= dy
                clampPan()
                applyTransform()
                return true
            }
            return false
        }
    }

    override fun onDestroy() {
        doc?.close()
        doc = null
        super.onDestroy()
    }

    companion object {
        const val EXTRA_PATH = "path"

        fun open(context: Context, path: String) {
            context.startActivity(Intent(context, DjvuReaderActivity::class.java).putExtra(EXTRA_PATH, path))
        }
    }
}
