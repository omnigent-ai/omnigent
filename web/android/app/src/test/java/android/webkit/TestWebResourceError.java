package android.webkit;

public final class TestWebResourceError extends WebResourceError {
    @Override
    public int getErrorCode() {
        return WebViewClient.ERROR_HOST_LOOKUP;
    }

    @Override
    public CharSequence getDescription() {
        return "host lookup failed";
    }
}
