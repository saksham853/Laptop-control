package com.sharedclipboard.app

import org.json.JSONObject
import java.net.ConnectException
import java.net.HttpURLConnection
import java.net.SocketTimeoutException
import java.net.URL
import java.net.UnknownHostException
import java.security.MessageDigest
import java.security.cert.CertificateException
import java.security.cert.X509Certificate
import javax.net.ssl.HostnameVerifier
import javax.net.ssl.HttpsURLConnection
import javax.net.ssl.SSLContext
import javax.net.ssl.SSLException
import javax.net.ssl.SSLSocketFactory
import javax.net.ssl.TrustManager
import javax.net.ssl.X509TrustManager

class ApiException(message: String) : Exception(message)

class Response(val code: Int, val body: String, val cookies: List<String>)

/**
 * Talks to the laptop. The laptop's certificate is self-signed, so the app does not use the
 * normal certificate check. It accepts only the certificate whose fingerprint was in the QR code.
 */
class Api(private val store: Store) {

    private fun pinnedFactory(): SSLSocketFactory {
        val expected = store.fingerprint.lowercase()
        val manager = object : X509TrustManager {
            override fun checkClientTrusted(chain: Array<X509Certificate>, authType: String) {}

            override fun checkServerTrusted(chain: Array<X509Certificate>, authType: String) {
                val digest = MessageDigest.getInstance("SHA-256").digest(chain[0].encoded)
                val actual = digest.joinToString("") { "%02x".format(it) }
                if (actual != expected) {
                    throw CertificateException("The certificate does not match the paired laptop")
                }
            }

            override fun getAcceptedIssuers(): Array<X509Certificate> = arrayOf()
        }
        val context = SSLContext.getInstance("TLS")
        context.init(null, arrayOf<TrustManager>(manager), null)
        return context.socketFactory
    }

    private fun connect(path: String, method: String): HttpsURLConnection {
        val url = URL("https://" + store.host + ":" + store.port + path)
        val conn = url.openConnection() as HttpsURLConnection
        conn.sslSocketFactory = pinnedFactory()
        // The fingerprint already proves which laptop this is, so the name is not checked.
        conn.hostnameVerifier = HostnameVerifier { _, _ -> true }
        conn.connectTimeout = 5000
        conn.readTimeout = 8000
        conn.requestMethod = method
        if (store.token.isNotEmpty()) {
            conn.setRequestProperty("Cookie", "sc_token=" + store.token)
        }
        return conn
    }

    private fun request(path: String, method: String, json: JSONObject?): Response {
        val conn = connect(path, method)
        try {
            if (json != null) {
                conn.doOutput = true
                conn.setRequestProperty("Content-Type", "application/json")
                conn.outputStream.use { it.write(json.toString().toByteArray(Charsets.UTF_8)) }
            }
            val code = conn.responseCode
            val stream = if (code >= 400) conn.errorStream else conn.inputStream
            val body = stream?.bufferedReader()?.use { it.readText() } ?: ""
            val cookies: List<String> = conn.headerFields["Set-Cookie"] ?: emptyList()
            return Response(code, body, cookies)
        } finally {
            conn.disconnect()
        }
    }

    private fun login() {
        val reply = request("/login", "POST", JSONObject().put("pin", store.pin))
        if (reply.code == 429) {
            throw ApiException("Too many wrong attempts. Wait a few minutes.")
        }
        if (reply.code != 200) {
            throw ApiException("The laptop rejected the PIN. Pair again.")
        }
        val cookie = reply.cookies.firstOrNull { it.startsWith("sc_token=") }
            ?: throw ApiException("The laptop sent no login token.")
        store.token = cookie.substringAfter("sc_token=").substringBefore(";")
    }

    private fun authorised(path: String, method: String, json: JSONObject?): Response {
        if (store.token.isEmpty()) {
            login()
        }
        var reply = request(path, method, json)
        if (reply.code == 401) {
            login()
            reply = request(path, method, json)
        }
        if (reply.code != 200) {
            val detail = try {
                JSONObject(reply.body).optString("error")
            } catch (e: Exception) {
                ""
            }
            throw ApiException(
                if (detail.isNotEmpty()) detail else "The laptop answered with error " + reply.code + "."
            )
        }
        return reply
    }

    /** Signs in with the stored PIN and keeps the login token. */
    fun loginNow() {
        login()
    }

    fun getText(): String {
        return JSONObject(authorised("/get", "GET", null).body).getString("text")
    }

    fun remote(action: String) {
        authorised("/remote", "POST", JSONObject().put("action", action))
    }

    fun setText(text: String) {
        authorised("/set", "POST", JSONObject().put("text", text))
    }

    companion object {
        fun describe(e: Exception): String {
            return when (e) {
                is ApiException -> e.message ?: "Error"
                is SSLException -> "The laptop's certificate changed. Pair again."
                is SocketTimeoutException, is ConnectException, is UnknownHostException ->
                    "Cannot reach the laptop. Check the Wi-Fi and that the laptop app is running."
                else -> "Error: " + e.message
            }
        }
    }
}
