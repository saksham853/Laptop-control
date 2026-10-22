package com.sharedclipboard.app

import android.app.Activity
import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.widget.Toast

/** Appears in the Android Share menu. Sends the shared text to the laptop and closes at once. */
class ShareActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val appContext = applicationContext
        val text = intent?.getCharSequenceExtra(Intent.EXTRA_TEXT)?.toString()
        val store = Store(appContext)

        if (text.isNullOrEmpty()) {
            Toast.makeText(appContext, "There is no text to send.", Toast.LENGTH_SHORT).show()
        } else if (!store.isPaired()) {
            Toast.makeText(appContext, "Open Lappy Control and pair with the laptop first.", Toast.LENGTH_LONG).show()
        } else {
            Thread {
                val message = try {
                    Api(store).setText(text)
                    "Sent to the laptop"
                } catch (e: Exception) {
                    Api.describe(e)
                }
                Handler(Looper.getMainLooper()).post {
                    Toast.makeText(appContext, message, Toast.LENGTH_LONG).show()
                }
            }.start()
        }
        finish()
    }
}
