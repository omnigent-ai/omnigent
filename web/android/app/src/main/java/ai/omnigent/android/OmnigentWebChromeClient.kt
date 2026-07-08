package ai.omnigent.android

import android.net.Uri
import android.webkit.PermissionRequest
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebView

/**
 * Supplies the two browser affordances a bare `WebView` lacks but the SPA needs,
 * delegating the parts that require Activity-scoped result launchers / runtime
 * permissions back to [MainActivity]:
 *
 *  - **File upload** (`<input type=file>`): without `onShowFileChooser` the
 *    picker never opens and the attach button silently does nothing.
 *  - **Microphone** (`getUserMedia({audio:true})`): without `onPermissionRequest`
 *    the WebView denies capture, so voice input is dead.
 */
class OmnigentWebChromeClient(
    /** Open a file picker for [acceptTypes]; return true if it was launched. */
    private val onChooseFiles: (
        callback: ValueCallback<Array<Uri>>,
        acceptTypes: Array<String>,
    ) -> Boolean,
    /** Grant or deny a WebView permission request (currently audio capture). */
    private val onPermission: (PermissionRequest) -> Unit,
) : WebChromeClient() {
    override fun onShowFileChooser(
        webView: WebView,
        filePathCallback: ValueCallback<Array<Uri>>,
        fileChooserParams: FileChooserParams,
    ): Boolean = onChooseFiles(filePathCallback, fileChooserParams.acceptTypes ?: emptyArray())

    override fun onPermissionRequest(request: PermissionRequest) {
        onPermission(request)
    }
}
