package ai.omnigent.android

import android.os.Handler
import android.os.Looper
import android.webkit.WebView
import androidx.webkit.Profile
import androidx.webkit.ProfileStore
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewFeature
import java.util.concurrent.CompletableFuture

internal interface WebProfileBackend {
    fun setAcceptCookie(accept: Boolean)

    fun removeAllCookies(callback: (Boolean) -> Unit)

    fun setCookie(
        url: String,
        value: String,
        callback: (Boolean) -> Unit,
    )

    fun getCookie(url: String): String?

    fun flushCookies()

    fun clearWebStorage()
}

private class AndroidWebProfileBackend(
    private val profile: Profile,
) : WebProfileBackend {
    override fun setAcceptCookie(accept: Boolean) = profile.cookieManager.setAcceptCookie(accept)

    override fun removeAllCookies(callback: (Boolean) -> Unit) =
        profile.cookieManager.removeAllCookies(callback)

    override fun setCookie(
        url: String,
        value: String,
        callback: (Boolean) -> Unit,
    ) = profile.cookieManager.setCookie(url, value, callback)

    override fun getCookie(url: String): String? = profile.cookieManager.getCookie(url)

    override fun flushCookies() = profile.cookieManager.flush()

    override fun clearWebStorage() = profile.webStorage.deleteAllData()
}

/** One persistent Android WebView profile for one credential scope. */
internal class DatabricksWebProfile(
    val name: String,
    private val backend: WebProfileBackend,
) {
    fun bind(webView: WebView) {
        WebViewCompat.setProfile(webView, name)
    }

    fun install(session: DatabricksWebSession): CompletableFuture<Void> =
        enqueue(name) {
            val future = CompletableFuture<Void>()
            backend.setAcceptCookie(true)
            backend.removeAllCookies {
                setCookies(session.cookies.iterator()) { error ->
                    if (error != null) {
                        future.completeExceptionally(error)
                    } else {
                        backend.flushCookies()
                        val visible =
                            parseCookieHeader(backend.getCookie(session.pageUri.toString()))
                        val expected =
                            session.cookies.lastOrNull {
                                it.name == "DBAUTH" &&
                                    !it.isDeletion &&
                                    it.appliesTo(session.pageUri)
                            }
                        if (expected == null || visible["DBAUTH"] != expected.value) {
                            future.completeExceptionally(DatabricksSessionException.MissingCookie())
                        } else {
                            future.complete(null)
                        }
                    }
                }
            }
            future
        }

    fun hasSessionCookie(pageUri: java.net.URI): CompletableFuture<Boolean> {
        val result = CompletableFuture<Boolean>()
        main.post {
            try {
                val value = parseCookieHeader(backend.getCookie(pageUri.toString()))["DBAUTH"]
                result.complete(!value.isNullOrEmpty())
            } catch (error: Throwable) {
                result.completeExceptionally(error)
            }
        }
        return result
    }

    fun clear(): CompletableFuture<Void> =
        enqueue(name) {
            val future = CompletableFuture<Void>()
            backend.removeAllCookies {
                backend.flushCookies()
                backend.clearWebStorage()
                future.complete(null)
            }
            future
        }

    private fun setCookies(
        iterator: Iterator<SessionCookie>,
        completion: (Throwable?) -> Unit,
    ) {
        if (!iterator.hasNext()) {
            completion(null)
            return
        }
        val cookie = iterator.next()
        backend.setCookie(cookie.sourceUri.toString(), cookie.setCookieHeader) { accepted ->
            if (!accepted) {
                completion(DatabricksSessionException.UnsafeCookie())
            } else {
                setCookies(iterator, completion)
            }
        }
    }

    companion object {
        private val main = Handler(Looper.getMainLooper())
        private val tails = mutableMapOf<String, CompletableFuture<Void>>()

        fun open(context: DatabricksWebContext): DatabricksWebProfile {
            if (!WebViewFeature.isFeatureSupported(WebViewFeature.MULTI_PROFILE)) {
                throw DatabricksSessionException.UnsupportedWebView()
            }
            val profile = ProfileStore.getInstance().getOrCreateProfile(context.profileName)
            return DatabricksWebProfile(context.profileName, AndroidWebProfileBackend(profile))
        }

        @Synchronized
        private fun enqueue(
            name: String,
            operation: () -> CompletableFuture<Void>,
        ): CompletableFuture<Void> {
            val result = CompletableFuture<Void>()
            val previous = tails[name] ?: CompletableFuture.completedFuture(null)
            val tail =
                previous.handle { _, _ -> null }.thenCompose {
                    val started = CompletableFuture<Void>()
                    main.post {
                        try {
                            operation().whenComplete { _, error ->
                                if (error == null) {
                                    started.complete(null)
                                } else {
                                    started.completeExceptionally(error)
                                }
                            }
                        } catch (error: Throwable) {
                            started.completeExceptionally(error)
                        }
                    }
                    started
                }
            tails[name] = tail
            tail.whenComplete { _, error ->
                synchronized(this) {
                    if (tails[name] === tail) tails.remove(name)
                }
                if (error == null) result.complete(null) else result.completeExceptionally(error)
            }
            return result
        }

        private fun parseCookieHeader(header: String?): Map<String, String> =
            header
                ?.split(';')
                ?.mapNotNull { field ->
                    val parts = field.trim().split('=', limit = 2)
                    parts.takeIf { it.size == 2 }?.let { it[0] to it[1] }
                }?.toMap()
                .orEmpty()
    }
}
