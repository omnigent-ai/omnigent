package android.webkit;

public final class FakeWebResourceError extends WebResourceError {
    private final int code;
    private final String description;

    public FakeWebResourceError(int code, String description) {
        this.code = code;
        this.description = description;
    }

    @Override
    public int getErrorCode() {
        return code;
    }

    @Override
    public CharSequence getDescription() {
        return description;
    }
}
