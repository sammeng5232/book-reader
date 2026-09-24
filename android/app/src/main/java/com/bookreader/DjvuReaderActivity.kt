package com.bookreader

import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.os.Bundle
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
import android.widget.BaseAdapter
import android.widget.Button
import android.widget.EditText
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
import com.bookreader.djvu.DjvuDocument
import com.bookreader.djvu.DjvuOutlineItem
import org.json.JSONObject
import java.io.File
import java.security.MessageDigest
import java.util.concurrent.Executors
import java.util.concurrent.Future
import kotlin.math.abs
import kotlin.math.roundToInt

/** Continuous DjVu pages, with always-visible navigation and real embedded bookmarks. */
class DjvuReaderActivity : AppCompatActivity() {
    private lateinit var list: RecyclerView
    private lateinit var layout: LinearLayoutManager
    private lateinit var viewport: ZoomViewport
    private lateinit var status: Button
    private lateinit var seek: SeekBar
    private lateinit var previous: Button
    private lateinit var next: Button
    private lateinit var contents: Button
    private lateinit var jump: Button
    private lateinit var menu: Button
    private var count = 0
    private var aspectRatios = FloatArray(0)
    private var outline: List<DjvuOutlineItem> = emptyList()
    private var outlineError: String? = null
    private var scale = 1f
    private var seekDragging = false
    private var requestedPage = 0
    private var generation = 0
    private var positionKey = ""
    @Volatile private var destroyed = false
    private var document: DjvuDocument? = null // Accessed only by the serial decoder executor.
    private val decoder = Executors.newSingleThreadExecutor()
    private val jobs = HashMap<RenderKey, Future<*>>() // UI-thread owned, as is the bitmap cache.
    private val cache = object : LruCache<RenderKey, Bitmap>(
        (Runtime.getRuntime().maxMemory() / 1024 / 10).toInt().coerceIn(16 * 1024, 48 * 1024)
    ) {
        override fun sizeOf(key: RenderKey, value: Bitmap) = (value.byteCount / 1024).coerceAtLeast(1)
    }
    private val prefs by lazy { getSharedPreferences("djvu-reader", Context.MODE_PRIVATE) }
    private data class RenderKey(val page: Int, val width: Int)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val path = intent.getStringExtra(EXTRA_PATH) ?: run { finish(); return }
        val file = File(path)
        positionKey = MessageDigest.getInstance("SHA-256").digest(file.absolutePath.toByteArray())
            .joinToString("") { "%02x".format(it.toInt() and 255) }
        val restorePage = savedInstanceState?.getInt("page") ?: prefs.getInt("$positionKey.page", 0)
        val restoreOffset = savedInstanceState?.getFloat("offset") ?: prefs.getFloat("$positionKey.offset", 0f)
        val restorePan = savedInstanceState?.getFloat("pan") ?: prefs.getFloat("$positionKey.pan", 0f)
        scale = (savedInstanceState?.getFloat("scale") ?: prefs.getFloat("$positionKey.scale", 1f)).coerceIn(1f, 4f)
        val root = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        val toolbar = LinearLayout(this).apply { gravity = Gravity.CENTER_VERTICAL }
        toolbar.addView(button("返回") { finish() })
        toolbar.addView(TextView(this).apply {
            text = file.nameWithoutExtension
            textSize = 15f
            maxLines = 1
            ellipsize = TextUtils.TruncateAt.END
            contentDescription = "书名：${file.nameWithoutExtension}"
        }, LinearLayout.LayoutParams(0, dp(48), 1f))
        menu = button("菜单") { showMenu() }
        contents = button("目录") { showContents() }
        jump = button("跳页") { showPageDialog() }
        toolbar.addView(menu)
        toolbar.addView(contents)
        toolbar.addView(jump)
        root.addView(toolbar, LinearLayout.LayoutParams(-1, dp(48)))

        list = RecyclerView(this).apply {
            setBackgroundColor(Color.rgb(225, 225, 225))
            itemAnimator = null
            setItemViewCacheSize(3)
        }
        layout = LinearLayoutManager(this).apply { initialPrefetchItemCount = 1 }
        list.layoutManager = layout
        viewport = ZoomViewport(this).apply {
            isFillViewport = true
            isHorizontalScrollBarEnabled = false
            addView(list, ViewGroup.LayoutParams(-1, -1))
        }
        root.addView(viewport, LinearLayout.LayoutParams(-1, 0, 1f))
        val navigation = LinearLayout(this).apply { gravity = Gravity.CENTER_VERTICAL }
        previous = button("上一页") { goToPage(currentPage() - 1) }
        next = button("下一页") { goToPage(currentPage() + 1) }
        status = button("正在打开…") { showPageDialog() }.apply { textSize = 14f }
        navigation.addView(previous)
        navigation.addView(status, LinearLayout.LayoutParams(0, dp(44), 1f))
        navigation.addView(next)
        root.addView(navigation)
        seek = SeekBar(this).apply { contentDescription = "页码进度" }
        root.addView(seek, LinearLayout.LayoutParams(-1, dp(36)))
        setContentView(root)
        ViewCompat.setOnApplyWindowInsetsListener(root) { view, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout())
            view.setPadding(bars.left, bars.top, bars.right, bars.bottom)
            insets
        }
        ViewCompat.requestApplyInsets(root)
        enableNavigation(false)
        seek.setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
            override fun onStartTrackingTouch(bar: SeekBar) { seekDragging = true; list.stopScroll() }
            override fun onProgressChanged(bar: SeekBar, value: Int, fromUser: Boolean) {
                if (fromUser) status.text = "${value + 1} / $count"
            }
            override fun onStopTrackingTouch(bar: SeekBar) { seekDragging = false; goToPage(bar.progress) }
        })
        list.addOnScrollListener(object : RecyclerView.OnScrollListener() {
            override fun onScrolled(rv: RecyclerView, dx: Int, dy: Int) { updateStatus() }
            override fun onScrollStateChanged(rv: RecyclerView, newState: Int) {
                if (newState == RecyclerView.SCROLL_STATE_IDLE) savePosition()
            }
        })
        viewport.addOnLayoutChangeListener { _, l, _, r, _, oldL, _, oldR, _ ->
            if (r - l > 0 && r - l != oldR - oldL) resizeStrip(scale, viewport.width / 2f, 0f)
        }

        // DjvuDocument has mutable dictionary caches and is explicitly not thread-safe.
        // Open, render and close it on this one executor, never on concurrent page threads.
        decoder.execute {
            val opened = DjvuDocument()
            try {
                val metadata = JSONObject(opened.open(path))
                val pages = metadata.getJSONArray("pages")
                val ratios = FloatArray(pages.length()) { index ->
                    val page = pages.getJSONObject(index)
                    val width = page.optInt("w", 1).coerceAtLeast(1)
                    val height = page.optInt("h", 1).coerceAtLeast(1)
                    if (page.optInt("rot") % 180 == 0) height.toFloat() / width else width.toFloat() / height
                }
                document = opened
                val bookmarks = opened.outline
                val bookmarkError = opened.outlineError
                runOnUiThread {
                    if (destroyed) return@runOnUiThread
                    count = ratios.size
                    aspectRatios = ratios
                    outline = bookmarks
                    outlineError = bookmarkError
                    requestedPage = restorePage.coerceIn(0, count - 1)
                    seek.max = (count - 1).coerceAtLeast(0)
                    list.adapter = PageAdapter()
                    enableNavigation(true)
                    viewport.post {
                        if (destroyed) return@post
                        resizeStrip(scale, 0f, 0f)
                        list.post restore@{
                            if (destroyed) return@restore
                            layout.scrollToPositionWithOffset(requestedPage, (restoreOffset * list.width).roundToInt())
                            viewport.scrollTo(((list.width - viewport.width).coerceAtLeast(0) * restorePan).roundToInt(), 0)
                            updateStatus()
                        }
                    }
                    Log.i("BookReader", "DjVu opened: pages=$count outline=${outline.size}")
                }
            } catch (error: Exception) {
                opened.close()
                Log.e("BookReader", "DjVu open failed", error)
                runOnUiThread { if (!destroyed) status.text = "无法打开 DjVu 文件" }
            }
        }
    }

    private fun dp(value: Int) = (value * resources.displayMetrics.density).roundToInt()
    private fun button(label: String, action: () -> Unit) = Button(this).apply {
        text = label
        contentDescription = label
        textSize = 13f
        minWidth = dp(48)
        minimumWidth = dp(48)
        setPadding(dp(8), 0, dp(8), 0)
        setOnClickListener { action() }
    }

    private fun enableNavigation(enabled: Boolean) {
        listOf(menu, contents, jump, status, previous, next).forEach { it.isEnabled = enabled }
        seek.isEnabled = enabled && count > 1
        if (enabled) updateStatus()
    }

    private fun currentPage(): Int = layout.findFirstVisibleItemPosition().takeIf { it >= 0 }
        ?: requestedPage

    private fun updateStatus() {
        if (count == 0 || seekDragging) return
        val page = currentPage().coerceIn(0, count - 1)
        requestedPage = page
        status.text = "${page + 1} / $count"
        status.contentDescription = "当前第 ${page + 1} 页，共 $count 页，点击跳页"
        seek.progress = page
        previous.isEnabled = page > 0
        next.isEnabled = page + 1 < count
    }

    private fun goToPage(page: Int) {
        if (page !in 0 until count) return
        discardQueuedRenders()
        list.adapter?.notifyDataSetChanged()
        requestedPage = page
        list.stopScroll()
        viewport.scrollTo(0, 0)
        layout.scrollToPositionWithOffset(page, 0)
        list.post { if (!destroyed) { updateStatus(); savePosition() } }
        Log.i("BookReader", "DjVu jump: page=${page + 1}/$count")
    }

    private fun showPageDialog() {
        if (count == 0) return
        val input = EditText(this).apply {
            inputType = InputType.TYPE_CLASS_NUMBER
            setSingleLine(true)
            setText((currentPage() + 1).toString())
            selectAll()
            hint = "1–$count"
            contentDescription = "输入页码"
        }
        val box = LinearLayout(this).apply { setPadding(dp(24), dp(8), dp(24), 0); addView(input, ViewGroup.LayoutParams(-1, -2)) }
        val dialog = AlertDialog.Builder(this).setTitle("跳到指定页（共 $count 页）")
            .setView(box).setNegativeButton("取消", null).setPositiveButton("跳转", null).create()
        dialog.setOnShowListener {
            dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener {
                val page = input.text.toString().trim().toIntOrNull()
                if (page == null || page !in 1..count) input.error = "请输入 1 到 $count 之间的页码"
                else { goToPage(page - 1); dialog.dismiss() }
            }
            input.requestFocus()
        }
        dialog.show()
    }

    private fun showMenu() {
        PopupMenu(this, menu).apply {
            listOf("目录", "跳页", "放大", "缩小", "重置缩放").forEachIndexed { index, label ->
                this.menu.add(0, index, index, label)
            }
            setOnMenuItemClickListener {
                when (it.itemId) {
                    0 -> showContents()
                    1 -> showPageDialog()
                    2 -> changeScale(scale * 1.25f)
                    3 -> changeScale(scale / 1.25f)
                    4 -> changeScale(1f)
                }
                true
            }
        }.show()
    }

    private data class OutlineRow(val node: DjvuOutlineItem, val depth: Int)
    private fun showContents() {
        if (outline.isEmpty()) {
            AlertDialog.Builder(this).setTitle("目录")
                .setMessage(if (outlineError == null) "此文件没有嵌入目录，可使用页码跳转。" else "此文件的目录数据无法解析，可使用页码跳转。")
                .setPositiveButton("跳页") { _, _ -> showPageDialog() }.setNegativeButton("关闭", null).show()
            return
        }
        val rows = ArrayList<OutlineRow>()
        fun append(nodes: List<DjvuOutlineItem>, depth: Int) {
            for (node in nodes) { rows.add(OutlineRow(node, depth)); append(node.children, depth + 1) }
        }
        append(outline, 0)
        val adapter = object : BaseAdapter() {
            override fun getCount() = rows.size
            override fun getItem(position: Int) = rows[position]
            override fun getItemId(position: Int) = position.toLong()
            override fun areAllItemsEnabled() = false
            override fun isEnabled(position: Int) = rows[position].node.page in 0 until this@DjvuReaderActivity.count
            override fun getView(position: Int, convertView: View?, parent: ViewGroup): View {
                val row = rows[position]
                return (convertView as? TextView ?: TextView(parent.context)).apply {
                    text = row.node.title.ifBlank { "未命名目录项" } +
                        if (row.node.page >= 0) "   ·   ${row.node.page + 1} 页" else ""
                    textSize = 16f
                    setPadding(dp(20 + row.depth.coerceAtMost(10) * 16), dp(14), dp(12), dp(14))
                    maxLines = 3
                    ellipsize = TextUtils.TruncateAt.END
                    alpha = if (row.node.page in 0 until this@DjvuReaderActivity.count) 1f else 0.6f
                }
            }
        }
        AlertDialog.Builder(this).setTitle("目录").setAdapter(adapter) { _, which ->
            goToPage(rows[which].node.page)
        }.setNegativeButton("关闭", null).show()
    }

    private inner class PageAdapter : RecyclerView.Adapter<PageHolder>() {
        override fun getItemCount() = count
        override fun onCreateViewHolder(parent: ViewGroup, viewType: Int) = PageHolder(PageImageView(parent.context))
        override fun onBindViewHolder(holder: PageHolder, position: Int) {
            holder.image.page = position
            holder.image.aspect = aspectRatios[position]
            holder.image.contentDescription = "第 ${position + 1} 页"
            holder.image.failed = false
            val key = RenderKey(position, renderWidth())
            val bitmap = cache.get(key) ?: cache.snapshot().entries.filter { it.key.page == position }
                .maxByOrNull { it.key.width }?.value
            holder.image.setImageBitmap(bitmap)
            if (cache.get(key) == null && !viewport.pinching) render(key)
        }
        override fun onViewRecycled(holder: PageHolder) { holder.image.setImageDrawable(null) }
    }

    private class PageHolder(val image: PageImageView) : RecyclerView.ViewHolder(image)
    private inner class PageImageView(context: Context) : ImageView(context) {
        var aspect = 1.414f
            set(value) { field = value.coerceIn(0.05f, 20f); requestLayout() }
        var page = 0
        var failed = false
        private val paint = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.DKGRAY; textSize = dp(16).toFloat(); textAlign = Paint.Align.CENTER }
        init {
            layoutParams = RecyclerView.LayoutParams(-1, -2)
            scaleType = ScaleType.FIT_CENTER
            setBackgroundColor(Color.WHITE)
            setPadding(0, dp(2), 0, dp(8))
        }
        override fun onMeasure(widthMeasureSpec: Int, heightMeasureSpec: Int) {
            val width = MeasureSpec.getSize(widthMeasureSpec).coerceAtLeast(1)
            setMeasuredDimension(width, (width * aspect).roundToInt() + paddingTop + paddingBottom)
        }
        override fun onDraw(canvas: Canvas) {
            super.onDraw(canvas)
            if (drawable == null) canvas.drawText(if (failed) "第 ${page + 1} 页解码失败" else "正在加载第 ${page + 1} 页…", width / 2f, (height / 2f).coerceAtMost(dp(300).toFloat()), paint)
        }
    }

    private fun renderWidth() = (((viewport.width.coerceAtLeast(512) * scale).roundToInt() + 127) / 128 * 128).coerceAtMost(2000)
    private fun render(key: RenderKey) {
        if (destroyed || jobs.containsKey(key)) return
        val requestGeneration = generation
        jobs[key] = decoder.submit {
            if (destroyed) return@submit
            val bitmap = runCatching {
                val bytes = document?.render(key.page, key.width, "jpg") ?: return@runCatching null
                BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
            }.onFailure { Log.w("BookReader", "DjVu page ${key.page + 1} failed", it) }.getOrNull()
            runOnUiThread {
                if (destroyed || generation != requestGeneration) return@runOnUiThread
                jobs.remove(key)
                if (bitmap != null) {
                    cache.put(key, bitmap)
                    list.adapter?.notifyItemChanged(key.page)
                } else {
                    (list.findViewHolderForAdapterPosition(key.page) as? PageHolder)?.image?.let {
                        it.failed = true; it.invalidate()
                    }
                }
            }
        }
    }

    private fun discardQueuedRenders() {
        generation++
        jobs.values.forEach { it.cancel(false) }
        jobs.clear()
    }

    private fun changeScale(value: Float) {
        resizeStrip(value, viewport.width / 2f, viewport.height / 2f)
    }

    private fun resizeStrip(value: Float, focusX: Float, focusY: Float) {
        if (!::viewport.isInitialized || viewport.width <= 0) return
        val newScale = value.coerceIn(1f, 4f)
        val page = layout.findFirstVisibleItemPosition().coerceAtLeast(0)
        val top = layout.findViewByPosition(page)?.top ?: 0
        val oldWidth = list.width.coerceAtLeast(viewport.width)
        val newWidth = (viewport.width * newScale).roundToInt()
        val ratio = newWidth.toFloat() / oldWidth
        val scrollX = ((viewport.scrollX + focusX) * ratio - focusX).roundToInt()
        scale = newScale
        list.layoutParams = list.layoutParams.apply { width = newWidth }
        layout.scrollToPositionWithOffset(page, (focusY - (focusY - top) * ratio).roundToInt())
        viewport.post { if (!destroyed) {
            viewport.scrollTo(scrollX.coerceAtLeast(0), 0)
            if (!viewport.pinching) finishZoom()
        } }
    }

    private fun finishZoom() {
        discardQueuedRenders()
        list.adapter?.notifyDataSetChanged()
        savePosition()
    }

    /** Observes gestures without swallowing RecyclerView's normal DOWN/MOVE/UP stream. */
    private inner class ZoomViewport(context: Context) : HorizontalScrollView(context) {
        var pinching = false
            private set
        private var downX = 0f
        private var downY = 0f
        private val scaler = ScaleGestureDetector(context, object : ScaleGestureDetector.SimpleOnScaleGestureListener() {
            override fun onScaleBegin(detector: ScaleGestureDetector): Boolean { list.stopScroll(); return true }
            override fun onScale(detector: ScaleGestureDetector): Boolean {
                resizeStrip(scale * detector.scaleFactor, detector.focusX, detector.focusY)
                return true
            }
        })
        private val taps = GestureDetector(context, object : GestureDetector.SimpleOnGestureListener() {
            override fun onDown(event: MotionEvent) = true
            override fun onDoubleTap(event: MotionEvent): Boolean { changeScale(1f); return true }
        })
        override fun dispatchTouchEvent(event: MotionEvent): Boolean {
            if (event.actionMasked == MotionEvent.ACTION_DOWN) pinching = false
            scaler.onTouchEvent(event)
            taps.onTouchEvent(event)
            if (event.pointerCount > 1 || scaler.isInProgress) {
                if (!pinching) {
                    pinching = true
                    MotionEvent.obtain(event).also { cancel ->
                        cancel.action = MotionEvent.ACTION_CANCEL
                        super.dispatchTouchEvent(cancel)
                        cancel.recycle()
                    }
                }
            }
            if (pinching) {
                if (event.actionMasked == MotionEvent.ACTION_UP || event.actionMasked == MotionEvent.ACTION_CANCEL) {
                    pinching = false
                    finishZoom()
                }
                return true
            }
            return super.dispatchTouchEvent(event)
        }
        override fun onInterceptTouchEvent(event: MotionEvent): Boolean {
            if (event.actionMasked == MotionEvent.ACTION_DOWN) { downX = event.x; downY = event.y }
            if (event.actionMasked == MotionEvent.ACTION_MOVE && abs(event.y - downY) > abs(event.x - downX)) return false
            return super.onInterceptTouchEvent(event)
        }
    }

    private fun savePosition() {
        if (count == 0 || !::list.isInitialized) return
        val page = layout.findFirstVisibleItemPosition().coerceAtLeast(0)
        val offset = (layout.findViewByPosition(page)?.top ?: 0).toFloat() / list.width.coerceAtLeast(1)
        val pan = viewport.scrollX.toFloat() / (list.width - viewport.width).coerceAtLeast(1)
        prefs.edit().putInt("$positionKey.page", page).putFloat("$positionKey.offset", offset)
            .putFloat("$positionKey.scale", scale).putFloat("$positionKey.pan", pan.coerceIn(0f, 1f)).apply()
    }

    override fun onPause() { savePosition(); super.onPause() }
    override fun onSaveInstanceState(outState: Bundle) {
        savePosition()
        outState.putInt("page", prefs.getInt("$positionKey.page", 0))
        outState.putFloat("offset", prefs.getFloat("$positionKey.offset", 0f))
        outState.putFloat("scale", scale)
        outState.putFloat("pan", prefs.getFloat("$positionKey.pan", 0f))
        super.onSaveInstanceState(outState)
    }

    override fun onDestroy() {
        destroyed = true
        discardQueuedRenders()
        decoder.execute { document?.close(); document = null }
        decoder.shutdown()
        cache.evictAll()
        super.onDestroy()
    }

    companion object {
        const val EXTRA_PATH = "path"
        fun open(context: Context, path: String) {
            context.startActivity(Intent(context, DjvuReaderActivity::class.java).putExtra(EXTRA_PATH, path))
        }
    }
}
