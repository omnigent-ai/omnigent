package android.webkit

class TestWebResourceError : WebResourceError() {
    override fun getErrorCode(): Int = WebViewClient.ERROR_UNKNOWN

    override fun getDescription(): CharSequence = "Test WebView error"
}
