# No app-specific keep rules are required.
#
# The web<->native bridge is a WebViewCompat.WebMessageListener
# (OmnigentBridgeListener), invoked through the AndroidX webkit library's own
# interface dispatch — not reflection or @JavascriptInterface — so R8 keeps it
# through ordinary reachability, and androidx.webkit ships consumer rules for
# the WebView-compat surface. NativeBridgeScript is a plain JS string constant
# with no reflective entry points.
#
# isMinifyEnabled = false today, so nothing here is active yet; this file is
# kept for whenever minification is enabled.
