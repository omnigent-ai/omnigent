package ai.omnigent.android

/**
 * Android `WebView` hands `http(s)` downloads to `DownloadManager`, but
 * `blob:` / `data:` URLs — how the agent's generated files come down — are
 * dropped by default (the same gap that leaves omnigent-ai/omnigent#969
 * unfixed on iOS). A blob URL is origin-scoped and only readable from page
 * context, so we fetch it there, base64-encode it, and post it over the same
 * origin-allowlisted bridge ([OmnigentBridgeListener]) for [BlobSaver] to write.
 *
 * This script is evaluated by the native side in the main frame only (from the
 * download listener), so it runs with main-frame trust.
 */
object BlobDownloadScript {
    /** JS that reads [url] (a blob:/data: URL) and posts it to the native side. */
    fun fetchAsBase64(
        url: String,
        suggestedName: String,
    ): String {
        val u = jsString(url)
        val name = jsString(suggestedName)
        return """
            (() => {
              fetch($u)
                .then((r) => r.blob())
                .then((blob) => new Promise((resolve, reject) => {
                  const reader = new FileReader();
                  reader.onloadend = () => resolve({ data: reader.result, type: blob.type });
                  reader.onerror = reject;
                  reader.readAsDataURL(blob);
                }))
                .then(({ data, type }) => {
                  // data is a data: URL: "data:<mime>;base64,<payload>".
                  const comma = data.indexOf(",");
                  const base64 = comma >= 0 ? data.slice(comma + 1) : data;
                  const bridge = window.${OmnigentBridgeListener.JS_OBJECT_NAME};
                  if (bridge) {
                    bridge.postMessage(JSON.stringify({
                      method: "blobBase64",
                      base64,
                      mimeType: type || "application/octet-stream",
                      name: $name,
                    }));
                  }
                })
                .catch(() => {});
            })();
            """.trimIndent()
    }
}
