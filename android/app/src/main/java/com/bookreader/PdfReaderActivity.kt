package com.bookreader

import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.pdf.PdfRenderer
import android.os.Bundle
import android.os.ParcelFileDescriptor
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import java.io.File
import kotlin.math.abs

/**
 * A minimal offline PDF reader for converted books (DjVu→PDF, and any PDF the user
 * opens).  Rendered with the platform's PdfRenderer; tap the edges to turn pages.
 * This is the DjVu reading surface: DjVu has no HTML spine, so it is read as the PDF
 * the converter produces.
 */
class PdfReaderActivity : AppCompatActivity() {

    private lateinit var image: ImageView
    private lateinit var status: TextView
    private var renderer: PdfRenderer? = null
    private var fd: ParcelFileDescriptor? = null
    private var current = 0
    private var count = 0

    private var downX = 0f
    private var downT = 0L

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val path = intent.getStringExtra(EXTRA_PATH) ?: run { finish(); return }
        val title = intent.getStringExtra(EXTRA_TITLE) ?: File(path).nameWithoutExtension
        setTitle(title)

        val root = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        image = ImageView(this).apply {
            scaleType = ImageView.ScaleType.FIT_CENTER
            setBackgroundColor(android.graphics.Color.WHITE)
        }
        status = TextView(this).apply {
            textSize = 12f
            gravity = Gravity.CENTER
            setPadding(0, 6, 0, 10)
        }
        root.addView(image, LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f))
        root.addView(status)
        setContentView(root)

        runCatching {
            fd = ParcelFileDescriptor.open(File(path), ParcelFileDescriptor.MODE_READ_ONLY)
            renderer = PdfRenderer(fd!!)
            count = renderer!!.pageCount
        }.onFailure {
            status.text = "Could not open the PDF."
            return
        }

        image.setOnTouchListener(object : View.OnTouchListener {
            override fun onTouch(v: View, e: MotionEvent): Boolean {
                when (e.actionMasked) {
                    MotionEvent.ACTION_DOWN -> { downX = e.x; downT = e.eventTime }
                    MotionEvent.ACTION_UP -> {
                        val dx = e.x - downX
                        if (e.eventTime - downT < 500 && abs(dx) < 24) {
                            val w = v.width
                            when {
                                e.x < w * 0.3f -> { show(current - 1); return true }
                                e.x > w * 0.7f -> { show(current + 1); return true }
                            }
                        }
                    }
                }
                return false
            }
        })
        show(0)
    }

    private fun show(index: Int) {
        val r = renderer ?: return
        if (index < 0 || index >= count) return
        current = index
        val page = r.openPage(index)
        val scale = resources.displayMetrics.density
        val w = (page.width * scale).toInt().coerceAtLeast(1)
        val h = (page.height * scale).toInt().coerceAtLeast(1)
        val bmp = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888)
        bmp.eraseColor(android.graphics.Color.WHITE)
        page.render(bmp, null, null, PdfRenderer.Page.RENDER_MODE_FOR_DISPLAY)
        page.close()
        image.setImageBitmap(bmp)
        status.text = "${index + 1} / $count"
    }

    override fun onDestroy() {
        renderer?.close()
        fd?.close()
        super.onDestroy()
    }

    companion object {
        const val EXTRA_PATH = "path"
        const val EXTRA_TITLE = "title"

        fun open(context: Context, path: String, title: String) {
            context.startActivity(
                Intent(context, PdfReaderActivity::class.java)
                    .putExtra(EXTRA_PATH, path)
                    .putExtra(EXTRA_TITLE, title)
            )
        }
    }
}
