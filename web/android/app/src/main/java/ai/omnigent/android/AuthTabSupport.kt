package ai.omnigent.android

import android.content.Context
import androidx.browser.auth.AuthTabIntent
import androidx.browser.customtabs.CustomTabsClient

/**
 * Resolves the device's Auth-Tab-capable Custom Tabs provider and builds
 * launches pinned to that package. The browser performs the Digital Asset
 * Links check client-side; if it fails, the shell keeps the in-WebView login
 * flow instead and never downgrades to a custom-scheme callback.
 */
object AuthTabSupport {
    fun providerPackage(context: Context): String? {
        val resolved = CustomTabsClient.getPackageName(context, null)
        if (resolved == null) {
            authLog("Auth Tab provider unresolved; Custom Tabs service may be hidden or absent")
        }
        return supportedProviderPackage(resolved) { provider ->
            CustomTabsClient.isAuthTabSupported(context, provider)
        }
    }

    internal fun supportedProviderPackage(
        providerPackage: String?,
        isSupported: (String) -> Boolean,
    ): String? {
        val provider = providerPackage ?: return null
        return provider.takeIf { runCatching { isSupported(it) }.getOrDefault(false) }
    }

    internal fun launchIntent(providerPackage: String): AuthTabIntent =
        AuthTabIntent.Builder().build().also { it.intent.setPackage(providerPackage) }
}
