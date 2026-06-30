package ai.omnigent.android

/**
 * The JavaScript injected into the main frame on every load to expose
 * `window.omnigentNative` with `kind: "android"`, mirroring the iOS shell's
 * bridge (`web/ios/Omnigent/OmnigentWebView.swift`). The web layer consumes
 * this through `web/src/lib/nativeBridge.ts` — same object shape, same
 * `__omnigentNativeEmit*` callback names — so no web change is needed beyond
 * accepting the `"android"` discriminator.
 *
 * web -> native goes through [OmnigentBridgeListener.JS_OBJECT_NAME], the
 * transport object injected by `WebViewCompat.addWebMessageListener` only into
 * frames on the pinned origin. `notify()` resolves `true` optimistically (as on
 * iOS) since the post is fire-and-forget. native -> web is driven by
 * `evaluateJavascript` into the `window.__omnigentNativeEmit*` functions here.
 */
object NativeBridgeScript {
    val source: String =
        """
        (() => {
          if (window.omnigentNative && window.omnigentNative.kind === "android") return;

          const ensureViewportFit = () => {
            let meta = document.querySelector('meta[name="viewport"]');
            if (!meta) {
              meta = document.createElement("meta");
              meta.name = "viewport";
              (document.head || document.documentElement).appendChild(meta);
            }
            const content = meta.getAttribute("content") || "width=device-width, initial-scale=1.0";
            const managedKeys = new Set([
              "width", "initial-scale", "minimum-scale",
              "maximum-scale", "user-scalable", "viewport-fit",
            ]);
            const preserved = content
              .split(",").map((p) => p.trim())
              .filter((p) => {
                const key = p.split("=")[0]?.trim().toLowerCase();
                return key && !managedKeys.has(key);
              });
            meta.setAttribute("content", [
              "width=device-width", "initial-scale=1.0", "minimum-scale=1.0",
              "maximum-scale=1.0", "user-scalable=no", "viewport-fit=cover",
              ...preserved,
            ].join(", "));
          };
          if (document.head) ensureViewportFit();
          else document.addEventListener("DOMContentLoaded", ensureViewportFit, { once: true });

          const post = (payload) => {
            try {
              const bridge = window.${OmnigentBridgeListener.JS_OBJECT_NAME};
              if (bridge) bridge.postMessage(JSON.stringify(payload));
            } catch (_) {}
          };

          const notificationCallbacks = new Set();
          // An activation is a fire-once event, but the native side may emit it
          // (cold-start tap, replayed at page-ready) BEFORE the React listener
          // mounts. So if there is no subscriber yet, stash the path and hand it
          // to the FIRST subscriber once, then clear it — never re-deliver.
          let pendingNotificationPath = null;
          Object.defineProperty(window, "__omnigentNativeEmitNotificationActivated", {
            configurable: false, enumerable: false, writable: false,
            value(path) {
              if (typeof path !== "string" || !path.startsWith("/")) return;
              if (notificationCallbacks.size === 0) { pendingNotificationPath = path; return; }
              for (const cb of notificationCallbacks) { try { cb(path); } catch (_) {} }
            },
          });

          const insetCallbacks = new Set();
          // Cache the last footprint so a subscriber that registers AFTER native
          // first emitted (the React app mounts later than document-start) still
          // gets the current value immediately on subscribe.
          let lastInsets = null;
          Object.defineProperty(window, "__omnigentNativeEmitInsets", {
            configurable: false, enumerable: false, writable: false,
            value(topBar, bottomBar) {
              const insets = {
                topBar: typeof topBar === "number" && Number.isFinite(topBar) ? topBar : 0,
                bottomBar: typeof bottomBar === "number" && Number.isFinite(bottomBar) ? bottomBar : 0,
              };
              lastInsets = insets;
              for (const cb of insetCallbacks) { try { cb(insets); } catch (_) {} }
            },
          });

          window.omnigentNative = Object.freeze({
            kind: "android",
            setBadgeCount(count) {
              // Note: unlike iOS, the native side ignores count <= 0 — Android has
              // no badge-clear API, so a previously-set badge can't be cleared
              // from the web (see NativeNotificationManager.setBadgeCount).
              post({ method: "setBadgeCount", count: Number.isFinite(count) ? count : 0 });
            },
            notify(params) {
              post({
                method: "notify",
                params: {
                  title: params && typeof params.title === "string" ? params.title : "",
                  body: params && typeof params.body === "string" ? params.body : "",
                  navigatePath:
                    params && typeof params.navigatePath === "string" ? params.navigatePath : "",
                },
              });
              return Promise.resolve(true);
            },
            onNotificationActivated(callback) {
              if (typeof callback !== "function") return () => {};
              notificationCallbacks.add(callback);
              if (pendingNotificationPath) {
                const p = pendingNotificationPath;
                pendingNotificationPath = null;
                try { callback(p); } catch (_) {}
              }
              return () => notificationCallbacks.delete(callback);
            },
            onNativeInsets(callback) {
              if (typeof callback !== "function") return () => {};
              insetCallbacks.add(callback);
              if (lastInsets) { try { callback(lastInsets); } catch (_) {} }
              return () => insetCallbacks.delete(callback);
            },
          });
        })();
        """.trimIndent()
}
