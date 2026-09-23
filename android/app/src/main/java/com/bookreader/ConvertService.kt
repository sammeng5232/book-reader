package com.bookreader

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder

/**
 * A minimal foreground service that lives exactly as long as a conversion.
 *
 * It runs nothing itself: the conversion keeps running on MainActivity's worker
 * thread, in this same process.  What the service contributes is foreground-
 * service priority for the process plus an ongoing notification, so battery
 * managers (Samsung's Freecess in particular) do not freeze the app halfway
 * through a long typeset.  Start it with [start], stop it with [stop].
 */
class ConvertService : Service() {

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (Build.VERSION.SDK_INT >= 26) {
            nm.createNotificationChannel(NotificationChannel(
                CHANNEL, "Book conversions", NotificationManager.IMPORTANCE_LOW).apply {
                description = "Shows while a book is being converted"
            })
        }
        val note = Notification.Builder(this, CHANNEL)
            .setContentTitle("Converting book")
            .setContentText("Turning the book into LaTeX and a PDF — this can take a few minutes.")
            .setSmallIcon(android.R.drawable.stat_sys_download)
            .setOngoing(true)
            .build()
        startForeground(NOTE_ID, note)
        return START_NOT_STICKY
    }

    companion object {
        private const val CHANNEL = "conversion"
        private const val NOTE_ID = 41

        /** Bring the process to foreground-service priority (safe to call repeatedly). */
        fun start(context: Context) {
            val intent = Intent(context, ConvertService::class.java)
            if (Build.VERSION.SDK_INT >= 26) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }

        /** The conversion is over; the notification goes away with the priority. */
        fun stop(context: Context) {
            context.stopService(Intent(context, ConvertService::class.java))
        }
    }
}
