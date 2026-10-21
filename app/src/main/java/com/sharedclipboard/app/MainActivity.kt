package com.sharedclipboard.app

import android.net.Uri
import android.net.http.SslError
import android.os.Build
import android.os.Bundle
import android.view.View
import android.webkit.CookieManager
import android.webkit.SslErrorHandler
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import com.google.mlkit.vision.barcode.common.Barcode
import com.google.mlkit.vision.codescanner.GmsBarcodeScannerOptions
import com.google.mlkit.vision.codescanner.GmsBarcodeScanning
import java.security.MessageDigest

/**
 * The app is a thin shell. It pairs with the laptop by QR code, signs in with the stored PIN,
 * and shows the laptop's own page (clipboard, media, mouse, keyboard) in a WebView.
 * New features need only a new laptop program, not a new APK.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var store: Store
    private lateinit var api: Api
    private lateinit var webView: WebView
    private lateinit var status: TextView
    private var loaded = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        store = Store(this)
        api = Api(store)
        webView = findViewById(R.id.webView)
        status = findViewById(R.id.status)

        webView.settings.javaScriptEnabled = true
        webView.settings.domStorageEnabled = true
        webView.webViewClient = object : WebViewClient() {
            override fun onReceivedSslError(view: WebView, handler: SslErrorHandler, error: SslError) {
                // The laptop's certificate is self-signed. Accept it only if it is the paired one.
                if (certificateMatches(error)) {
                    handler.proceed()
                } else {
                    handler.cancel()
                }
            }
        }

        findViewById<Button>(R.id.btnPair).setOnClickListener { scanCode() }
        findViewById<Button>(R.id.btnReload).setOnClickListener { openLaptop() }
    }

    override fun onResume() {
        super.onResume()
        if (!loaded) {
            openLaptop()
        }
    }

    private fun certificateMatches(error: SslError): Boolean {
        if (Build.VERSION.SDK_INT < 29) {
            return false
        }
        val certificate = error.certificate.x509Certificate ?: return false
        val digest = MessageDigest.getInstance("SHA-256").digest(certificate.encoded)
        val actual = digest.joinToString("") { "%02x".format(it) }
        return actual == store.fingerprint.lowercase()
    }

    private fun openLaptop() {
        if (!store.isPaired()) {
            webView.visibility = View.GONE
            status.text = "Not paired. Tap Pair and scan the code in the laptop window."
            return
        }
        status.text = "Connecting to " + store.host + "..."
        Thread {
            try {
                api.loginNow()
                runOnUiThread {
                    val base = "https://" + store.host + ":" + store.port
                    val cookies = CookieManager.getInstance()
                    cookies.setAcceptCookie(true)
                    cookies.setCookie(base, "sc_token=" + store.token + "; Path=/; Secure")
                    cookies.flush()
                    webView.visibility = View.VISIBLE
                    webView.loadUrl(base + "/")
                    status.text = "Connected to " + store.host
                    loaded = true
                }
            } catch (e: Exception) {
                val reason = Api.describe(e)
                runOnUiThread { status.text = reason }
            }
        }.start()
    }

    private fun scanCode() {
        val options = GmsBarcodeScannerOptions.Builder()
            .setBarcodeFormats(Barcode.FORMAT_QR_CODE)
            .build()
        GmsBarcodeScanning.getClient(this, options)
            .startScan()
            .addOnSuccessListener { barcode -> pair(barcode.rawValue ?: "") }
            .addOnFailureListener { e -> status.text = "Scan stopped: " + e.message }
    }

    private fun pair(code: String) {
        val uri = Uri.parse(code)
        val host = uri.getQueryParameter("host")
        val port = uri.getQueryParameter("port")?.toIntOrNull()
        val pin = uri.getQueryParameter("pin")
        val fingerprint = uri.getQueryParameter("fp")
        if (uri.scheme != "sharedclip" || host == null || port == null || pin == null || fingerprint == null) {
            status.text = "This is not a Shared Clipboard code."
            return
        }
        store.savePairing(host, port, pin, fingerprint.lowercase())
        webView.clearSslPreferences()
        loaded = false
        openLaptop()
    }
}
