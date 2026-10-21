package com.sharedclipboard.app

import android.content.Context

/** Keeps the pairing details in the app's private storage. */
class Store(context: Context) {
    private val prefs = context.getSharedPreferences("pairing", Context.MODE_PRIVATE)

    val host: String get() = prefs.getString("host", "") ?: ""
    val port: Int get() = prefs.getInt("port", 5000)
    val pin: String get() = prefs.getString("pin", "") ?: ""
    val fingerprint: String get() = prefs.getString("fp", "") ?: ""

    var token: String
        get() = prefs.getString("token", "") ?: ""
        set(value) {
            prefs.edit().putString("token", value).apply()
        }

    fun isPaired(): Boolean = host.isNotEmpty() && pin.isNotEmpty() && fingerprint.isNotEmpty()

    fun savePairing(host: String, port: Int, pin: String, fingerprint: String) {
        prefs.edit()
            .putString("host", host)
            .putInt("port", port)
            .putString("pin", pin)
            .putString("fp", fingerprint)
            .putString("token", "")
            .apply()
    }
}
