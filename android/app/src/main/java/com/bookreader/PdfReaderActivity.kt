package com.bookreader

import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.Color
import android.graphics.pdf.PdfRenderer
import android.os.Bundle
import android.os.ParcelFileDescriptor
import android.text.InputType
import android.text.TextUtils
import android.util.Log
import android.util.LruCache
import android.view.GestureDetector
import android.view.Gravity
import android.view.MotionEvent
import android.view.ScaleGestureDetector
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.HorizontalScrollView
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.PopupMenu
import android.widget.SeekBar
import android.widget.TextView
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import java.io.File
import java.security.MessageDigest
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.ThreadPoolExecutor
import java.util.concurrent.TimeUnit
import kotlin.math.abs
import kotlin.math.roundToInt
import kotlin.math.sqrt

/** Offline PDF pages, rendered lazily. The RecyclerView owns ordinary vertical
 * gestures; zoom changes real page sizes, so scrolling still reaches every page. */
class PdfReaderActivity : AppCompatActivity() {
    private lateinit var pages: RecyclerView
    private lateinit var layout: LinearLayoutManager
    private lateinit var viewport: PdfViewport
    private lateinit var status: TextView
    private lateinit var previous: Button
    private lateinit var next: Button
    private lateinit var seek: SeekBar
    private lateinit var pageAdapter: PageAdapter
    private val worker = ThreadPoolExecutor(1, 1, 0L, TimeUnit.MILLISECONDS, LinkedBlockingQueue<Runnable>())
    private var renderer: PdfRenderer? = null // opened, used and closed only on worker
    private var descriptor: ParcelFileDescriptor? = null
    private var count = 0
    private var ratios = FloatArray(0)
    private var zoom = 1f
    private var baseWidth = 0
    @Volatile private var generation = 0
    @Volatile private var closing = false
    private var seeking = false
    private var pinching = false
    private var positionKey = ""
    private val prefs by lazy { getSharedPreferences("pdf_reader", Context.MODE_PRIVATE) }
    private val pending = mutableSetOf<String>() // UI thread only
    private val cache = object : LruCache<String, Bitmap>(32 * 1024) {
        override fun sizeOf(key: String, value: Bitmap) = (value.allocationByteCount / 1024).coerceAtLeast(1)
        // Do not recycle evicted bitmaps: a visible ImageView can still use them.
    }
    private data class Anchor(val page: Int, val fraction: Float, val y: Int)
    private var pinchAnchor: Anchor? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val path = intent.getStringExtra(EXTRA_PATH) ?: run { finish(); return }
        val source = File(path)
        title = intent.getStringExtra(EXTRA_TITLE) ?: source.nameWithoutExtension
        val identity = "${source.absolutePath}|${source.length()}|${source.lastModified()}"
        positionKey = MessageDigest.getInstance("SHA-256").digest(identity.toByteArray())
            .joinToString("") { "%02x".format(it) }
        zoom = (savedInstanceState?.getFloat("zoom") ?: prefs.getFloat("$positionKey.zoom", 1f)).coerceIn(1f, 3f)
        val restorePage = savedInstanceState?.getInt("page") ?: intent.getIntExtra("page",
            prefs.getInt("$positionKey.page", 0))
        val restoreFraction = savedInstanceState?.getFloat("fraction") ?: prefs.getFloat("$positionKey.fraction", 0f)

        val root = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        ViewCompat.setOnApplyWindowInsetsListener(root) { v, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars())
            v.setPadding(bars.left, bars.top, bars.right, bars.bottom)
            insets
        }
        val toolbar = LinearLayout(this).apply { gravity = Gravity.CENTER_VERTICAL }
        toolbar.addView(button("‹", "返回") { finish() }, LinearLayout.LayoutParams(dp(44), dp(48)))
        toolbar.addView(TextView(this).apply {
            text = title; contentDescription = title; textSize = 15f
            maxLines = 1; ellipsize = TextUtils.TruncateAt.MIDDLE
        }, LinearLayout.LayoutParams(0, dp(48), 1f))
        toolbar.addView(button("−", "缩小") { changeZoom(zoom / 1.25f) }, LinearLayout.LayoutParams(dp(42), dp(48)))
        toolbar.addView(button("+", "放大") { changeZoom(zoom * 1.25f) }, LinearLayout.LayoutParams(dp(42), dp(48)))
        val menu = button("⋮", "菜单") { }
        menu.setOnClickListener { showMenu(menu) }
        toolbar.addView(menu, LinearLayout.LayoutParams(dp(44), dp(48)))
        root.addView(toolbar)

        layout = LinearLayoutManager(this)
        pageAdapter = PageAdapter()
        pages = RecyclerView(this).apply {
            layoutManager = layout; adapter = pageAdapter
            setItemViewCacheSize(2); itemAnimator = null
            setBackgroundColor(0xffdddddd.toInt())
            contentDescription = "PDF 页面，上下滑动翻页"
            addOnScrollListener(object : RecyclerView.OnScrollListener() {
                override fun onScrolled(rv: RecyclerView, dx: Int, dy: Int) = updateStatus()
                override fun onScrollStateChanged(rv: RecyclerView, newState: Int) {
                    if (newState == RecyclerView.SCROLL_STATE_IDLE) savePosition()
                }
            })
        }
        viewport = PdfViewport(this)
        viewport.addView(pages, FrameLayout.LayoutParams(1, ViewGroup.LayoutParams.MATCH_PARENT))
        root.addView(viewport, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f))
        viewport.addOnLayoutChangeListener { _, left, _, right, _, oldLeft, _, oldRight, _ ->
            val width = right - left
            if (width > 0 && (baseWidth == 0 || width != oldRight - oldLeft)) {
                val anchor = currentAnchor(0)
                baseWidth = width
                resizePages(anchor, true)
            }
        }
        seek = SeekBar(this).apply {
            contentDescription = "快速跳页"
            setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
                override fun onStartTrackingTouch(bar: SeekBar) { seeking = true }
                override fun onProgressChanged(bar: SeekBar, value: Int, fromUser: Boolean) {
                    if (fromUser && count > 0) status.text = "${value + 1} / $count"
                }
                override fun onStopTrackingTouch(bar: SeekBar) { seeking = false; jump(bar.progress) }
            })
        }
        root.addView(seek, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(30)))
        val navigation = LinearLayout(this).apply { gravity = Gravity.CENTER_VERTICAL }
        previous = button("上一页", "上一页") { jump(currentPage() - 1) }
        next = button("下一页", "下一页") { jump(currentPage() + 1) }
        status = TextView(this).apply {
            text = "正在打开 PDF…"; gravity = Gravity.CENTER; textSize = 14f
            contentDescription = "页码，点击跳页"; isClickable = true
            setOnClickListener { showGoToPage() }
        }
        navigation.addView(previous, LinearLayout.LayoutParams(dp(86), dp(46)))
        navigation.addView(status, LinearLayout.LayoutParams(0, dp(46), 1f))
        navigation.addView(next, LinearLayout.LayoutParams(dp(86), dp(46)))
        root.addView(navigation)
        previous.isEnabled = false; next.isEnabled = false; seek.isEnabled = false
        setContentView(root)

        worker.execute {
            runCatching {
                descriptor = ParcelFileDescriptor.open(source, ParcelFileDescriptor.MODE_READ_ONLY)
                renderer = PdfRenderer(descriptor!!)
                val n = renderer!!.pageCount
                require(n > 0) { "PDF 没有可显示的页面" }
                val ratio = renderer!!.openPage(0).use { it.height.toFloat() / it.width.coerceAtLeast(1) }
                runOnUiThread {
                    if (!closing) {
                        count = n; ratios = FloatArray(n) { ratio }
                        seek.max = n - 1; seek.isEnabled = true
                        pageAdapter.notifyDataSetChanged()
                        pages.post {
                            jump(restorePage.coerceIn(0, n - 1), restoreFraction)
                            Log.i("BookReader", "PDF opened: pages=$n page=${restorePage.coerceIn(0, n - 1) + 1}")
                        }
                    }
                }
            }.onFailure { error ->
                Log.e("BookReader", "PDF open failed", error)
                runOnUiThread { if (!closing) status.text = "无法打开 PDF：${error.message}" }
            }
        }
    }

    private fun dp(value: Int) = (value * resources.displayMetrics.density).roundToInt()
    private fun button(label: String, description: String, action: () -> Unit) = Button(this).apply {
        text = label; contentDescription = description; minWidth = 0; minimumWidth = 0
        setPadding(dp(3), 0, dp(3), 0); textSize = 13f; isAllCaps = false
        setOnClickListener { action() }
    }
    private fun currentPage() = layout.findFirstVisibleItemPosition().coerceIn(0, (count - 1).coerceAtLeast(0))
    private fun displayWidth() = (baseWidth.coerceAtLeast(1) * zoom).roundToInt()
    private fun pageHeight(page: Int) = (displayWidth() * ratios.getOrElse(page) { 1.414f }).roundToInt().coerceAtLeast(1)
    private fun currentAnchor(y: Int): Anchor {
        val child = pages.findChildViewUnder((viewport.scrollX + viewport.width / 2).toFloat(), y.toFloat())
            ?: layout.findViewByPosition(currentPage())
        val page = child?.let { pages.getChildAdapterPosition(it) }?.takeIf { it >= 0 } ?: currentPage()
        val fraction = if (child != null && child.height > 0) (y - child.top).toFloat() / child.height else 0f
        return Anchor(page, fraction.coerceIn(0f, 1f), y)
    }
    private fun updateStatus() {
        if (count == 0) return
        val page = currentPage()
        if (!seeking) { status.text = "${page + 1} / $count"; seek.progress = page }
        previous.isEnabled = page > 0; next.isEnabled = page < count - 1
    }
    private fun jump(page: Int, fraction: Float = 0f) {
        if (count == 0) return
        generation++; worker.queue.clear(); pending.clear()
        pages.stopScroll()
        val target = page.coerceIn(0, count - 1)
        val epoch = generation
        // A restored page may have a different aspect ratio from page one.
        // Resolve its true geometry before applying a fractional scroll offset.
        worker.execute {
            val ratio = runCatching { renderer!!.openPage(target).use { it.height.toFloat() / it.width.coerceAtLeast(1) } }
            runOnUiThread {
                if (closing || generation != epoch) return@runOnUiThread
                ratio.onSuccess { ratios[target] = it }.onFailure { Log.w("BookReader", "PDF page size unavailable", it) }
                pageAdapter.notifyDataSetChanged()
                layout.scrollToPositionWithOffset(target, -(pageHeight(target) * fraction.coerceIn(0f, 1f)).roundToInt())
                pages.post { updateStatus(); savePosition() }
                Log.i("BookReader", "PDF jump: page=${target + 1}/$count")
            }
        }
    }
    private fun savePosition() {
        if (count == 0 || closing) return
        val anchor = currentAnchor(0)
        prefs.edit().putInt("$positionKey.page", anchor.page).putFloat("$positionKey.fraction", anchor.fraction)
            .putFloat("$positionKey.zoom", zoom).apply()
    }
    private fun showGoToPage() {
        if (count == 0) return
        val input = EditText(this).apply {
            inputType = InputType.TYPE_CLASS_NUMBER; setSingleLine()
            contentDescription = "输入页码"; setText((currentPage() + 1).toString()); selectAll()
        }
        val dialog = AlertDialog.Builder(this).setTitle("跳页（1–$count）").setView(input)
            .setNegativeButton("取消", null).setPositiveButton("跳转", null).create()
        dialog.setOnShowListener {
            dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener {
                val number = input.text.toString().toIntOrNull()
                if (number == null || number !in 1..count) input.error = "请输入 1 到 $count 的页码"
                else { jump(number - 1); dialog.dismiss() }
            }
        }
        dialog.show()
    }
    private fun showMenu(anchor: View) {
        PopupMenu(this, anchor).apply {
            menu.add("跳页…").setOnMenuItemClickListener { showGoToPage(); true }
            menu.add("第一页").setOnMenuItemClickListener { jump(0); true }
            menu.add("最后一页").setOnMenuItemClickListener { jump(count - 1); true }
            menu.add("适合宽度").setOnMenuItemClickListener { changeZoom(1f); true }
            menu.add("放大").setOnMenuItemClickListener { changeZoom(zoom * 1.25f); true }
            menu.add("缩小").setOnMenuItemClickListener { changeZoom(zoom / 1.25f); true }
            show()
        }
    }
    private fun changeZoom(value: Float, anchor: Anchor = currentAnchor(viewport.height / 2)) {
        val newZoom = value.coerceIn(1f, 3f)
        if (abs(newZoom - zoom) < 0.005f) return
        zoom = newZoom
        resizePages(anchor, !pinching)
    }
    private fun resizePages(anchor: Anchor, render: Boolean) {
        if (baseWidth == 0) return
        if (render) { generation++; worker.queue.clear(); pending.clear() }
        val horizontalFraction = if (pages.width > viewport.width)
            viewport.scrollX.toFloat() / (pages.width - viewport.width) else 0.5f
        pages.layoutParams = pages.layoutParams.apply { width = displayWidth() }
        pageAdapter.notifyDataSetChanged()
        pages.post {
            if (count > 0) layout.scrollToPositionWithOffset(anchor.page.coerceIn(0, count - 1),
                anchor.y - (pageHeight(anchor.page) * anchor.fraction).roundToInt())
            viewport.scrollTo(((displayWidth() - viewport.width).coerceAtLeast(0) * horizontalFraction).roundToInt(), 0)
            updateStatus()
        }
    }

    private class PageHolder(val frame: FrameLayout, val image: ImageView, val message: TextView) : RecyclerView.ViewHolder(frame) {
        var key: String = ""
    }
    private inner class PageAdapter : RecyclerView.Adapter<PageHolder>() {
        override fun getItemCount() = count
        override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): PageHolder {
            val frame = FrameLayout(parent.context).apply { setBackgroundColor(Color.WHITE) }
            val image = ImageView(parent.context).apply { scaleType = ImageView.ScaleType.FIT_CENTER }
            val message = TextView(parent.context).apply { gravity = Gravity.CENTER; setTextColor(Color.DKGRAY) }
            frame.addView(image, FrameLayout.LayoutParams(-1, -1))
            frame.addView(message, FrameLayout.LayoutParams(-1, -1))
            return PageHolder(frame, image, message)
        }
        override fun onBindViewHolder(holder: PageHolder, position: Int) {
            val width = minOf(displayWidth(), 2800, sqrt(5_000_000.0 / ratios[position]).toInt()).coerceAtLeast(1)
            val key = "$position:$width"
            holder.frame.layoutParams = RecyclerView.LayoutParams(-1, pageHeight(position)).apply { bottomMargin = dp(6) }
            holder.image.contentDescription = "PDF 第 ${position + 1} 页"
            val oldKey = holder.key
            holder.key = key
            val bitmap = cache.get(key)
            if (bitmap != null) {
                holder.image.setImageBitmap(bitmap); holder.message.visibility = View.GONE
            } else {
                if (!pinching || oldKey.substringBefore(':') != position.toString()) holder.image.setImageDrawable(null)
                holder.message.text = "正在载入第 ${position + 1} 页…"
                holder.message.visibility = if (pinching) View.GONE else View.VISIBLE
                if (!pinching) loadPage(position, width, key, holder)
            }
        }
        override fun onViewRecycled(holder: PageHolder) { holder.key = ""; holder.image.setImageDrawable(null) }
    }
    private fun loadPage(index: Int, width: Int, key: String, holder: PageHolder) {
        if (!pending.add(key) || closing) return
        val epoch = generation
        worker.execute {
            if (closing || epoch != generation) return@execute
            val result = runCatching {
                renderer!!.openPage(index).use { page ->
                    val ratio = page.height.toFloat() / page.width.coerceAtLeast(1)
                    val safeWidth = minOf(width, sqrt(5_000_000.0 / ratio).toInt(), (8192f / ratio).toInt()).coerceAtLeast(1)
                    val bitmap = Bitmap.createBitmap(safeWidth, (safeWidth * ratio).roundToInt().coerceIn(1, 8192), Bitmap.Config.ARGB_8888)
                    bitmap.eraseColor(Color.WHITE)
                    page.render(bitmap, null, null, PdfRenderer.Page.RENDER_MODE_FOR_DISPLAY)
                    bitmap to ratio
                }
            }
            runOnUiThread {
                if (closing || epoch != generation) return@runOnUiThread
                pending.remove(key)
                result.onSuccess { (bitmap, ratio) ->
                    cache.put(key, bitmap)
                    ratios[index] = ratio
                    if (holder.key == key && holder.absoluteAdapterPosition == index) {
                        holder.frame.layoutParams = holder.frame.layoutParams.apply { height = pageHeight(index) }
                        holder.image.setImageBitmap(bitmap); holder.message.visibility = View.GONE
                    } else pageAdapter.notifyItemChanged(index)
                }.onFailure {
                    Log.e("BookReader", "PDF page ${index + 1} render failed", it)
                    if (holder.key == key) holder.message.text = "第 ${index + 1} 页无法显示：${it.message}"
                }
            }
        }
    }

    /** Horizontal pan only when zoomed; vertical drags stay with RecyclerView. */
    private inner class PdfViewport(context: Context) : HorizontalScrollView(context) {
        private var x0 = 0f
        private var y0 = 0f
        private var consumeDoubleTap = false
        private val scaleDetector = ScaleGestureDetector(context, object : ScaleGestureDetector.SimpleOnScaleGestureListener() {
            override fun onScaleBegin(detector: ScaleGestureDetector): Boolean {
                pinchAnchor = currentAnchor(detector.focusY.roundToInt()); return true
            }
            override fun onScale(detector: ScaleGestureDetector): Boolean {
                changeZoom(zoom * detector.scaleFactor, pinchAnchor ?: currentAnchor(detector.focusY.roundToInt()))
                return true
            }
            override fun onScaleEnd(detector: ScaleGestureDetector) {
                pinching = false
                resizePages(pinchAnchor ?: currentAnchor(0), true)
            }
        })
        private val taps = GestureDetector(context, object : GestureDetector.SimpleOnGestureListener() {
            override fun onDown(event: MotionEvent) = true
            override fun onDoubleTap(event: MotionEvent): Boolean {
                consumeDoubleTap = true
                changeZoom(if (zoom < 1.5f) 2f else 1f, currentAnchor(event.y.roundToInt()))
                return true
            }
        })
        private var consumePinchSequence = false
        init { isFillViewport = true; isHorizontalScrollBarEnabled = true }
        override fun onInterceptTouchEvent(event: MotionEvent): Boolean {
            if (event.actionMasked == MotionEvent.ACTION_DOWN) { x0 = event.x; y0 = event.y }
            if (event.actionMasked == MotionEvent.ACTION_MOVE && abs(event.y - y0) > abs(event.x - x0)) return false
            return zoom > 1.01f && super.onInterceptTouchEvent(event)
        }
        override fun dispatchTouchEvent(event: MotionEvent): Boolean {
            if (event.actionMasked == MotionEvent.ACTION_DOWN) { consumePinchSequence = false; consumeDoubleTap = false }
            if (event.pointerCount > 1 && !consumePinchSequence) {
                val cancel = MotionEvent.obtain(event).apply { action = MotionEvent.ACTION_CANCEL }
                super.dispatchTouchEvent(cancel); cancel.recycle()
                consumePinchSequence = true; pinching = true
                pages.stopScroll()
            }
            scaleDetector.onTouchEvent(event)
            if (!consumePinchSequence) taps.onTouchEvent(event)
            if (consumePinchSequence || consumeDoubleTap) {
                if (event.actionMasked == MotionEvent.ACTION_UP || event.actionMasked == MotionEvent.ACTION_CANCEL) {
                    consumePinchSequence = false; consumeDoubleTap = false
                    if (pinching) { pinching = false; resizePages(pinchAnchor ?: currentAnchor(0), true) }
                }
                return true
            }
            return super.dispatchTouchEvent(event)
        }
    }
    override fun onPause() { savePosition(); super.onPause() }
    override fun onSaveInstanceState(out: Bundle) {
        val anchor = currentAnchor(0)
        out.putInt("page", anchor.page); out.putFloat("fraction", anchor.fraction); out.putFloat("zoom", zoom)
        super.onSaveInstanceState(out)
    }
    override fun onDestroy() {
        closing = true; generation++; worker.queue.clear(); pending.clear(); cache.evictAll()
        worker.execute { renderer?.close(); renderer = null; descriptor?.close(); descriptor = null }
        worker.shutdown()
        super.onDestroy()
    }
    companion object {
        const val EXTRA_PATH = "path"
        const val EXTRA_TITLE = "title"
        fun open(context: Context, path: String, title: String) {
            context.startActivity(Intent(context, PdfReaderActivity::class.java).putExtra(EXTRA_PATH, path).putExtra(EXTRA_TITLE, title))
        }
    }
}
