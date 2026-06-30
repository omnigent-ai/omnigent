# Keep the @JavascriptInterface bridge: R8 must not rename/strip methods the
# injected JS calls by name (see NativeBridge / NativeBridgeScript).
-keepclassmembers class ai.omnigent.android.NativeBridge {
    @android.webkit.JavascriptInterface <methods>;
}
