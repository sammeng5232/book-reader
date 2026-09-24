package com.bookreader

import android.content.Context
import android.net.Uri
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import androidx.webkit.WebMessageCompat
import androidx.webkit.WebViewClientCompat
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewFeature
import com.chaquo.python.Python
import org.json.JSONObject
import java.io.ByteArrayInputStream
import java.util.Locale

/**
 * The Android counterpart of the desktop's `webhost.py`: serves one open book into a
 * WebView and runs `assets/reader.js` in it.
 *
 * Differences from the desktop, and why they are safe:
 *
 * * **Origin**: the desktop registers a custom `epub://` scheme; Android cannot do
 *   that, so the book is served from `https://<per-book-host>.bookreader.app/` through
 *   [WebViewAssetLoader]'s path handler.  An https origin is a secure context and is
 *   not opaque, so CORS and fetch behave like the desktop's.
 * * **Bridge**: QWebChannel is replaced by [WebViewCompat.addWebMessageListener].  JS
 *   posts a JSON string `{kind, payload}`; host→JS messages go back with
 *   [WebViewCompat.postWebMessage].  The injected facade presents the same
 *   `window.epubReaderHost` surface `reader.js` already writes against.
 * * **Injection**: `addDocumentStartJavaScript` runs the boot in the main world
 *   before any page script.  `reader.js` is appended to the boot so there is a single
 *   injection point, exactly like the desktop's one DocumentCreation script.
 */
class BookHost(private val context: Context, private val webView: WebView) {

    interface Listener {
        /** A book document finished loading; call `init` now. */
        fun onPageLoaded(url: String) {}
        /** The reading layer confirmed init (reader.js `ready`). */
        fun onReady(state: JSONObject) {}
        /** The engine reported a new reading position/state object. */
        fun onPosition(state: JSONObject) {}
        /** A link was activated; `zip`/`fragment` resolved, or `url` for external. */
        fun onLink(zip: String, fragment: String, url: String?) {}
        /** console-ish text from the page. */
        fun onLog(message: String) {}
        fun onReaderCommand(command: String) {}
    }

    var listener: Listener? = null
    val fontLibrary = FontLibrary(context)
    fun fontCss(): String = fontLibrary.css()

    private var bookHost = DEFAULT_HOST
    private var bookOpen = false

    // ------------------------------------------------------------------
    // Python access (Chaquopy); one open book per process, reader_android holds it
    // ------------------------------------------------------------------
    private fun py(): com.chaquo.python.PyObject {
        Converter.start(context)            // idempotent; starts Python and store dirs
        return Python.getInstance().getModule("reader_android")
    }

    fun open(path: String): JSONObject {
        val info = JSONObject(py().callAttr("open_book", path).toString())
        if (info.optBoolean("ok")) {
            bookOpen = true
            bookHost = hostIdFor(path)
        }
        return info
    }

    fun close() {
        if (bookOpen) {
            runCatching { py().callAttr("close_book") }
            bookOpen = false
        }
    }

    fun savePosition(locatorJson: String, percent: Double) {
        if (bookOpen) runCatching { py().callAttr("save_position", locatorJson, percent) }
    }

    // ------------------------------------------------------------------
    // URL model
    // ------------------------------------------------------------------
    /** `https://<bookHost>.bookreader.app/<percent-encoded zip entry>[#fragment]` */
    fun urlFor(zipName: String, fragment: String = ""): String {
        val path = Uri.encode(zipName, "/")
        return "https://$bookHost$DOMAIN_SUFFIX/$path" + if (fragment.isEmpty()) "" else "#" + Uri.encode(fragment, "")
    }

    private fun hostIdFor(path: String): String {
        // stable per file; the WebView origin changes when the file does
        val md = java.security.MessageDigest.getInstance("SHA-256")
        md.update(path.toByteArray(Charsets.UTF_8))
        runCatching {
            val f = java.io.File(path)
            md.update("${f.length()}|${f.lastModified()}".toByteArray())
        }
        return "b" + md.digest().joinToString("") { "%02x".format(it) }.substring(0, 12)
    }

    // ------------------------------------------------------------------
    // Serving book entries
    // ------------------------------------------------------------------
    private fun shouldServe(url: Uri): Boolean =
        url.scheme == "https" && (url.host == bookHost + DOMAIN_SUFFIX || url.host == DEFAULT_HOST + DOMAIN_SUFFIX)

    private fun serve(path: String): WebResourceResponse? {
        if (!bookOpen) return null
        val zipName = path.removePrefix("/")
        if (zipName.isEmpty()) return null
        if (zipName.startsWith("__er_fonts/")) {
            return fontLibrary.serve(zipName.removePrefix("__er_fonts/"))
        }
        val module = py()
        val entry = run {
            // Try to resolve relative/encoded hrefs the way the desktop does.
            val resolved = runCatching {
                JSONObject(module.callAttr("resolve", zipName, "").toString()).optString("zip")
            }.getOrDefault("")
            when {
                resolved.isNotEmpty() && module.callAttr("has", resolved).toBoolean() -> resolved
                module.callAttr("has", zipName).toBoolean() -> zipName
                else -> return null
            }
        }
        val ext = entry.substringAfterLast('.', "").lowercase(Locale.US)
        val mime = MIME[ext] ?: OCTET
        return if (mime in TEXT_MIME) {
            val text = runCatching { module.callAttr("read_text", entry).toString() }.getOrNull()
                ?: runCatching {
                    String(module.callAttr("read", entry).toJava(ByteArray::class.java), Charsets.UTF_8)
                }.getOrNull() ?: return null
            val body = text.removePrefix("﻿").toByteArray(Charsets.UTF_8)
            WebResourceResponse(mime, "utf-8", ByteArrayInputStream(body)).apply {
                if (mime in DOCUMENT_MIME) responseHeaders = mapOf("Content-Security-Policy" to CSP)
            }
        } else {
            val bytes = runCatching { module.callAttr("read", entry).toJava(ByteArray::class.java) }.getOrNull()
                ?: return null
            WebResourceResponse(mime, null, ByteArrayInputStream(bytes))
        }
    }

    // ------------------------------------------------------------------
    // WebView wiring
    // ------------------------------------------------------------------
    /** Wire the WebView up.  Call once before any loadUrl. */
    fun install() {
        if (context.applicationInfo.flags and android.content.pm.ApplicationInfo.FLAG_DEBUGGABLE != 0) {
            WebView.setWebContentsDebuggingEnabled(true)
        }
        val s = webView.settings
        s.javaScriptEnabled = true
        // The reader's font slider is the single size control. Android inherits
        // the system font_scale as textZoom (75% on the verified phone), which
        // shrinks glyphs but leaves em-sized image geometry unchanged. Pin the
        // WebView text zoom so formula glyphs and surrounding text share 1 em.
        s.textZoom = 100
        s.domStorageEnabled = false                 // books must not persist anything
        s.allowFileAccess = false
        s.allowContentAccess = false
        s.mediaPlaybackRequiresUserGesture = true
        @Suppress("DEPRECATION")
        s.allowFileAccessFromFileURLs = false
        @Suppress("DEPRECATION")
        s.allowUniversalAccessFromFileURLs = false
        // The reading engine measures the CSS viewport to size its pages.  Without a
        // real device-width viewport the WebView reports a huge canvas and reader.js
        // lays the whole book out as one tall-but-unscrollable block.
        s.useWideViewPort = true
        s.loadWithOverviewMode = true
        s.setSupportZoom(false)                     // zoom is the app's, not pinch on the page

        webView.webViewClient = object : WebViewClientCompat() {
            override fun shouldInterceptRequest(view: WebView, request: WebResourceRequest): WebResourceResponse? {
                val url = request.url
                return if (shouldServe(url)) serve(url.path ?: "") else null
            }

            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val url = request.url
                if (shouldServe(url)) return false
                listener?.onLink("", "", url.toString())
                return true
            }

            override fun onPageFinished(view: WebView, url: String) {
                if (shouldServe(Uri.parse(url))) listener?.onPageLoaded(url)
            }
        }
        webView.webChromeClient = object : android.webkit.WebChromeClient() {
            override fun onConsoleMessage(m: android.webkit.ConsoleMessage): Boolean {
                if (m.messageLevel() == android.webkit.ConsoleMessage.MessageLevel.ERROR) {
                    android.util.Log.w("BookReader", "console error: ${m.message()} @${m.sourceId()}:${m.lineNumber()}")
                }
                return true
            }
        }

        // Bridge: the page posts {kind, payload} JSON on the "epubReaderHost" listener.
        // The listener object is injected into the page as `epubReaderHostPort` (the
        // name given here); the boot facade posts to it from `send()`.
        if (WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER)) {
            WebViewCompat.addWebMessageListener(
                webView, "epubReaderHostPort", setOf("https://*$DOMAIN_SUFFIX")
            ) { _, message, _, _, _ ->
                val data = message.data ?: return@addWebMessageListener
                runCatching { dispatch(JSONObject(data)) }
            }
        } else {
            android.util.Log.w("BookReader", "WEB_MESSAGE_LISTENER unsupported on this WebView")
        }

        injectBoot()
    }

    // ------------------------------------------------------------------
    // JS bridge
    // ------------------------------------------------------------------
    private fun dispatch(msg: JSONObject) {
        when (msg.optString("kind")) {
            "ready" -> listener?.onReady(msg.optJSONObject("payload") ?: JSONObject())
            "positionChanged" -> msg.optJSONObject("payload")?.let { listener?.onPosition(it) }
            "linkClicked" -> onLinkClicked(msg.opt("payload")?.toString() ?: "")
            "log" -> listener?.onLog(msg.opt("payload")?.toString() ?: "")
            "keyUnhandled" -> listener?.onReaderCommand(msg.opt("payload")?.toString() ?: "")
        }
    }

    private fun onLinkClicked(href: String) {
        val h = href.trim()
        if (h.isEmpty()) return
        val lower = h.lowercase(Locale.US)
        if (lower.startsWith("http://") || lower.startsWith("https://") ||
            lower.startsWith("mailto:") || lower.startsWith("tel:")
        ) {
            listener?.onLink("", "", h)
            return
        }
        val module = py()
        val r = runCatching {
            JSONObject(module.callAttr("resolve", h, currentZip).toString())
        }.getOrNull() ?: return
        val zip = r.optString("zip")
        if (zip.isEmpty() || !module.callAttr("has", zip).toBoolean()) return
        listener?.onLink(zip, r.optString("fragment"), null)
    }

    /** JS → host `epubReaderHost` facade + reader.js, injected at document start. */
    private fun injectBoot() {
        val readerJs = context.assets.open("assets/reader.js").bufferedReader().use { it.readText() }
        val readerCss = context.assets.open("assets/reader.css").bufferedReader().use { it.readText() } + fontCss()
        // reader.css must reach the page as a <style> element: it is what turns the
        // --er-fs / --er-ff / --er-* custom properties reader.js sets on :root into
        // actual font-size / font-family / colours.  Without it the settings do
        // nothing.  (The desktop injects it the same way via webhost's boot script.)
        val boot = """
(function () {
  if (window.__epubReaderBoot) { return; }
  window.__epubReaderBoot = 1;

  var READER_CSS = ${JSONObject.quote(readerCss)};
  function injectStyles() {
    var parent = document.head || document.documentElement;
    if (!parent) { return false; }
    var s = document.getElementById('__er_reader');
    if (!s) {
      s = document.createElement('style');
      s.id = '__er_reader';
      parent.appendChild(s);
    }
    s.textContent = READER_CSS;
    return true;
  }
  if (!injectStyles()) {
    var obs = new MutationObserver(function () { if (injectStyles()) { obs.disconnect(); } });
    obs.observe(document, { childList: true, subtree: true });
    document.addEventListener('DOMContentLoaded', injectStyles);
  }

  function send(kind, payload) {
    var msg;
    try { msg = JSON.stringify({ kind: kind, payload: payload === undefined ? null : payload }); }
    catch (e) { msg = JSON.stringify({ kind: kind, payload: String(payload) }); }
    var port = window.epubReaderHostPort;
    if (port && typeof port.postMessage === 'function') {
      port.postMessage(msg);
    } else {
      pending.push(msg);
      if (pending.length > 500) { pending.shift(); }
    }
  }
  var pending = [];
  // The injected port object can arrive a tick after this script; flush when it does.
  var portWait = setInterval(function () {
    var port = window.epubReaderHostPort;
    if (port && typeof port.postMessage === 'function') {
      while (pending.length) { port.postMessage(pending.shift()); }
      clearInterval(portWait);
    }
  }, 20);
  setTimeout(function () { clearInterval(portWait); }, 10000);

  var listeners = [];
  window.epubReaderHost = {
    send: send,
    on: function (fn) { if (typeof fn === 'function') { listeners.push(fn); } },
    off: function (fn) { var i = listeners.indexOf(fn); if (i >= 0) { listeners.splice(i, 1); } },
    ready: function (s) { send('ready', s || null); },
    positionChanged: function (s) { send('positionChanged', s || null); },
    linkClicked: function (h) { send('linkClicked', h == null ? '' : String(h)); },
    selectionChanged: function (i) { send('selectionChanged', i == null ? null : i); },
    noteRequested: function (id) { send('noteRequested', id == null ? '' : String(id)); },
    keyUnhandled: function (k) { send('keyUnhandled', k == null ? '' : String(k)); },
    domReady: function () { send('domReady', null); },
    log: function (m) { send('log', m == null ? '' : String(m)); },
    isConnected: function () { return true; }
  };
  window.__epubReaderDeliver = function (text) {
    var msg = text;
    try { msg = JSON.parse(text); } catch (e) { /* keep the raw string */ }
    var ls = listeners.slice();
    for (var i = 0; i < ls.length; i++) { try { ls[i](msg); } catch (e) {} }
  };

  // Force a device-width CSS viewport so the reading engine measures the real screen.
  (function ensureViewport() {
    var m = document.querySelector('meta[name="viewport"]');
    if (!m) {
      m = document.createElement('meta');
      m.setAttribute('name', 'viewport');
      (document.head || document.documentElement).appendChild(m);
    }
    m.setAttribute('content', 'width=device-width, initial-scale=1');
  })();

  // Clicks on schemes Chromium has no handler for (mailto:, custom:) still reach us.
  window.addEventListener('click', function (e) {
    if (e.defaultPrevented || e.button !== 0) { return; }
    var t = e.target;
    var a = (t && t.closest) ? t.closest('a[href]') : null;
    if (!a || typeof a.protocol !== 'string') { return; }
    var p = a.protocol.toLowerCase();
    if (p === 'https:' || p === 'http:') { return; }
    e.preventDefault();
    if (p !== 'javascript:') { send('linkClicked', a.getAttribute('href')); }
  }, true);
})();

""".trimIndent()
        if (WebViewFeature.isFeatureSupported(WebViewFeature.DOCUMENT_START_SCRIPT)) {
            WebViewCompat.addDocumentStartJavaScript(
                webView, boot + readerJs, setOf("https://*$DOMAIN_SUFFIX")
            )
        }
    }

    // ------------------------------------------------------------------
    // Driving the reading layer
    // ------------------------------------------------------------------
    var currentZip = ""
        private set

    fun navigate(zipName: String, fragment: String = "") {
        currentZip = zipName
        webView.loadUrl(urlFor(zipName, fragment))
    }

    /** Call `window.epubReader.<method>(args…)`; `callback` gets the decoded JSON. */
    fun callReader(method: String, vararg args: Any?, callback: ((Any?) -> Unit)? = null) {
        val packed = args.joinToString(", ") { a ->
            when (a) {
                null -> "null"
                is Number, is Boolean -> a.toString()
                // JSONObject/JSONArray carry their own JSON text: pass it raw, not quoted.
                is JSONObject, is org.json.JSONArray -> a.toString()
                else -> JSONObject.quote(a.toString())
            }
        }
        val expr = "(window.epubReader && typeof window.epubReader.$method === 'function')" +
            " ? JSON.stringify(window.epubReader.$method($packed)) : 'null'"
        webView.evaluateJavascript(expr) { value ->
            if (callback == null) return@evaluateJavascript
            if (value == null || value == "null") { callback(null); return@evaluateJavascript }
            runCatching {
                // evaluateJavascript hands back a JSON string literal of our JSON text.
                val inner = org.json.JSONTokener(value).nextValue() as? String
                callback(if (inner == null) null else org.json.JSONTokener(inner).nextValue())
            }.onFailure { callback(null) }
        }
    }

    companion object {
        private const val DOMAIN_SUFFIX = ".bookreader.app"
        private const val DEFAULT_HOST = "book"

        private const val CSP = "default-src 'none'; img-src 'self' data: blob:; " +
            "style-src 'self' 'unsafe-inline'; font-src 'self' data:; media-src 'self' data:; " +
            "connect-src 'self'; script-src 'none'; object-src 'none'; frame-src 'none'; " +
            "base-uri 'none'; form-action 'none'"

        private const val OCTET = "application/octet-stream"

        private val MIME = mapOf(
            "xhtml" to "application/xhtml+xml", "xht" to "application/xhtml+xml",
            "html" to "text/html", "htm" to "text/html", "xml" to "application/xml",
            "opf" to "application/oebps-package+xml", "ncx" to "application/x-dtbncx+xml",
            "css" to "text/css", "js" to "application/javascript", "mjs" to "application/javascript",
            "json" to "application/json", "txt" to "text/plain",
            "png" to "image/png", "jpg" to "image/jpeg", "jpeg" to "image/jpeg",
            "gif" to "image/gif", "webp" to "image/webp", "bmp" to "image/bmp",
            "avif" to "image/avif", "svg" to "image/svg+xml", "ico" to "image/x-icon",
            "ttf" to "font/ttf", "otf" to "font/otf", "ttc" to "font/collection",
            "woff" to "font/woff", "woff2" to "font/woff2",
        )

        private val TEXT_MIME = setOf(
            "application/xhtml+xml", "text/html", "text/css", "text/plain",
            "application/javascript", "application/xml", "application/oebps-package+xml",
            "application/x-dtbncx+xml", "application/json", "image/svg+xml",
        )
        private val DOCUMENT_MIME = setOf("application/xhtml+xml", "text/html")
    }
}
