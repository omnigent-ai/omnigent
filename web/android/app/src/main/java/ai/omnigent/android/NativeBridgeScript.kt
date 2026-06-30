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

          // Apply the OS safe area to the layout from the native side. emitInsets
          // feeds --omnigent-safe-top/bottom (the app's own inset vars), but on a
          // server whose web build predates the Android shell the inset-aware rules
          // lose the cascade: their semantic selectors (.chat-conversation-content
          // etc., specificity 0,1,0) tie with the Tailwind utility classes on the
          // same elements and lose on source order, so the OS inset is dropped and
          // content bleeds under the status bar / behind the gesture nav. Re-assert
          // the inset paddings here with !important so they win regardless, keyed to
          // the same vars (mirrors the [data-android-native] rules in index.css).
          // The header additionally reads a raw env(safe-area-inset-top), which
          // Android WebView reports as 0, so it needs the override even pre-Tailwind.
          const ensureInsetStyles = () => {
            if (document.getElementById("omnigent-android-insets")) return;
            const T = "var(--omnigent-safe-top, 0px)";
            const B = "var(--omnigent-safe-bottom, 0px)";
            const style = document.createElement("style");
            style.id = "omnigent-android-insets";
            style.textContent = [
              ".chat-header{top:max(0px, calc(" + T + " - 0.5rem)) !important}",
              ".chat-conversation-content{padding-top:calc(var(--omnigent-header-height, 3.5rem) + 1.5rem + " + T + ") !important}",
              ".main-terminal-view{padding-top:calc(3.25rem + " + T + ") !important}",
              // Bottom inset belongs on whichever element is bottom-most per mode:
              // the composer in regular chat, the switcher pill in terminal-first
              // (its composer sits above the pill, so it must NOT also add it).
              ".chat-composer-form{padding-bottom:calc(0.75rem + " + B + ") !important}",
              ".chat-composer-form.terminal-first-composer-form{padding-bottom:0.25rem !important}",
              ".terminal-first-switcher-container{padding-bottom:calc(0.35rem + " + B + ") !important}",
              // Drawers/panels span full height — clear both bars.
              ":is(.conversations-sidebar,[data-testid=\"file-viewer\"],[data-testid=\"files-panel-drawer\"],[data-testid=\"terminals-panel\"],[data-testid=\"subagents-panel-drawer\"],[data-testid=\"todos-panel-drawer\"]){padding-top:" + T + " !important;padding-bottom:" + B + " !important}",
            ].join("");
            (document.head || document.documentElement).appendChild(style);
          };
          if (document.head) ensureInsetStyles();
          else document.addEventListener("DOMContentLoaded", ensureInsetStyles, { once: true });

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

          // Android hardware/gesture back: close an open in-page overlay first so
          // back means "dismiss this drawer/dialog", not "leave the app" (the SPA
          // doesn't put every overlay in history). Returns true only when it
          // actually closed a VISIBLE modal/drawer — the native side falls back to
          // WebView history / finish() otherwise, so this never traps the user.
          Object.defineProperty(window, "__omnigentNativeHandleBack", {
            configurable: false, enumerable: false, writable: false,
            value() {
              try {
                // An overlay counts as open only if its CENTER is on-screen. The
                // panel drawers stay in the DOM at full size when CLOSED — just
                // translated off the side (e.g. left:524 on a 523px screen) — so a
                // size + vertical test alone false-detects a closed drawer and
                // swallows Back ("does nothing"). offsetParent is also null for the
                // position:fixed overlays, so it can't be used either.
                const onScreen = (el) => {
                  if (!el) return false;
                  const cs = getComputedStyle(el);
                  if (cs.display === "none" || cs.visibility === "hidden" || parseFloat(cs.opacity) === 0) return false;
                  const r = el.getBoundingClientRect();
                  if (r.width < 24 || r.height < 24) return false;
                  const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
                  return cx > 0 && cx < window.innerWidth && cy > 0 && cy < window.innerHeight;
                };
                // One dispatch on document — Radix/most libs listen there and close
                // only the TOP layer per Escape. Dispatching twice (element-bubbled
                // + direct) could collapse two stacked overlays in a single Back.
                const fireEscape = () => {
                  document.dispatchEvent(new KeyboardEvent("keydown", {
                    key: "Escape", code: "Escape", keyCode: 27, which: 27,
                    bubbles: true, cancelable: true,
                  }));
                };
                // Below Tailwind's md breakpoint the side surfaces are DRAWERS
                // (overlays) Back should dismiss; at md+ they dock as persistent
                // rails (md:relative md:translate-x-0, still data-state="open")
                // that Back must NOT close. Modal dialogs dismiss at any width.
                const narrow = window.innerWidth < 768;
                // 1. Conversations sidebar (a drawer only when narrow; closed =
                // data-collapsed).
                if (narrow) {
                  const sb = document.querySelector(".conversations-sidebar");
                  if (sb && sb.getAttribute("data-collapsed") !== "true" && onScreen(sb)) {
                    const toggle = [...document.querySelectorAll("button")].find(
                      (b) => /close sidebar/i.test(b.getAttribute("aria-label") || ""),
                    );
                    if (toggle) toggle.click(); else fireEscape();
                    return true;
                  }
                }
                // 2. Any open Radix-style overlay. data-state="open" is the
                // authoritative open/closed signal; a closed one is "closed".
                let target = null;
                for (const el of document.querySelectorAll('[data-state="open"]')) {
                  const testid = el.getAttribute("data-testid") || "";
                  const isModal =
                    el.getAttribute("role") === "dialog" ||
                    el.getAttribute("aria-modal") === "true";
                  const isDrawerPanel = /-(drawer|panel|sheet)${'$'}/.test(testid);
                  // Modal dismisses at any width; a drawer/rail panel only while
                  // it's actually a drawer (narrow), not a docked md+ rail.
                  if ((isModal || (isDrawerPanel && narrow)) && onScreen(el)) { target = el; break; }
                }
                // File viewer / terminals panel may not carry data-state — drawers
                // only when narrow (they're persistent rails at md+).
                if (!target && narrow) {
                  for (const el of document.querySelectorAll(
                    '[data-testid="file-viewer"], [data-testid="terminals-panel"]',
                  )) { if (onScreen(el)) { target = el; break; } }
                }
                if (!target) return false;
                const closer = target.querySelector(
                  '[aria-label*="lose" i], [data-testid${'$'}="-close"], [data-testid*="close" i], button[data-dismiss]',
                );
                if (closer) closer.click(); else fireEscape();
                return true;
              } catch (_) { return false; }
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
