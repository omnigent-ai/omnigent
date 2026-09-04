import SwiftUI
import UIKit
import WebKit

struct OmnigentWebView: UIViewRepresentable {
  let initialURL: URL
  @ObservedObject var model: WebViewModel
  @ObservedObject var settings: SettingsStore
  let loadFailed: (URL, String) -> Void
  let loadSucceeded: () -> Void
  /// Compose and push the current server-picker payload to the SPA.
  let pushServerPicker: () -> Void
  /// Switch the shell to a picker-listed server. The owner validates the
  /// target against the managed/recent allow list before acting.
  let requestSwitchServer: (String) -> Void
  /// Return the shell to its "connect to server" setup page.
  let openServerSetup: () -> Void

  func makeCoordinator() -> Coordinator {
    Coordinator(self)
  }

  func makeUIView(context: Context) -> WKWebView {
    let contentController = WKUserContentController()
    contentController.add(context.coordinator, name: "omnigentNative")
    contentController.addUserScript(
      WKUserScript(
        source: Self.nativeBridgeScript,
        injectionTime: .atDocumentStart,
        forMainFrameOnly: true
      )
    )

    let configuration = WKWebViewConfiguration()
    configuration.userContentController = contentController
    configuration.allowsInlineMediaPlayback = true

    let webView = AccessoryFreeWebView(frame: .zero, configuration: configuration)
    webView.navigationDelegate = context.coordinator
    webView.uiDelegate = context.coordinator
    // The left-edge swipe is repurposed to open the web app's sidebar (see the
    // edge-pan recognizer below), so the native back/forward gesture is off —
    // the two would otherwise fight over the same edge.
    webView.allowsBackForwardNavigationGestures = false
    webView.isFindInteractionEnabled = true
    webView.isOpaque = false
    webView.backgroundColor = .clear
    webView.underPageBackgroundColor = .clear
    webView.scrollView.backgroundColor = .clear
    webView.scrollView.contentInsetAdjustmentBehavior = .never

    // Allow Safari Web Inspector to attach to the web content. Since iOS 16.4 a
    // WKWebView is inspectable only when this is opt-in. Debug-only so shipping
    // builds aren't inspectable.
    #if DEBUG
      if #available(iOS 16.4, *) {
        webView.isInspectable = true
      }
    #endif

    let edgePan = UIScreenEdgePanGestureRecognizer(
      target: context.coordinator,
      action: #selector(Coordinator.handleLeftEdgePan(_:))
    )
    edgePan.edges = .left
    edgePan.delegate = context.coordinator
    webView.addGestureRecognizer(edgePan)

    model.webView = webView
    context.coordinator.attach(webView)
    context.coordinator.load(initialURL, in: webView)
    return webView
  }

  func updateUIView(_ webView: WKWebView, context: Context) {
    context.coordinator.parent = self
    model.webView = webView
    if context.coordinator.pinnedURL != initialURL {
      context.coordinator.load(initialURL, in: webView)
    }
  }

  static func dismantleUIView(_ uiView: WKWebView, coordinator: Coordinator) {
    uiView.configuration.userContentController.removeScriptMessageHandler(forName: "omnigentNative")
    coordinator.detach()
  }

  private static let nativeBridgeScript = """
    (() => {
      if (window.omnigentNative && window.omnigentNative.kind === "ios") return;
      const ensureViewportFit = () => {
        let meta = document.querySelector('meta[name="viewport"]');
        if (!meta) {
          meta = document.createElement("meta");
          meta.name = "viewport";
          (document.head || document.documentElement).appendChild(meta);
        }
        const content = meta.getAttribute("content") || "width=device-width, initial-scale=1.0";
        const managedKeys = new Set([
          "width",
          "initial-scale",
          "minimum-scale",
          "maximum-scale",
          "user-scalable",
          "viewport-fit",
        ]);
        const preserved = content
          .split(",")
          .map((part) => part.trim())
          .filter((part) => {
            const key = part.split("=")[0]?.trim().toLowerCase();
            return key && !managedKeys.has(key);
          });
        meta.setAttribute(
          "content",
          [
            "width=device-width",
            "initial-scale=1.0",
            "minimum-scale=1.0",
            "maximum-scale=1.0",
            "user-scalable=no",
            "viewport-fit=cover",
            ...preserved,
          ].join(", ")
        );
      };
      // A workspace-hosted page reassigns the whole `content` attribute after it
      // mounts, dropping `viewport-fit=cover` — and with it every
      // `env(safe-area-inset-*)` the web layer pads with, so content lands under the
      // status bar. One-shot injection isn't enough: the host rewrites again on its
      // own re-renders, so re-assert whenever the token goes missing.
      const hasViewportFitCover = () => {
        const meta = document.querySelector('meta[name="viewport"]');
        if (!meta) return false;
        const content = (meta.getAttribute("content") || "").toLowerCase();
        return content.split(" ").join("").includes("viewport-fit=cover");
      };
      const watchViewportFit = () => {
        if (!document.head || typeof MutationObserver === "undefined") return;
        // `ensureViewportFit` writes the attribute we're observing; the guard turns
        // that re-entry into a no-op rather than a loop.
        new MutationObserver(() => {
          if (!hasViewportFitCover()) ensureViewportFit();
        }).observe(document.head, {
          childList: true, // the host may replace the tag instead of editing it
          subtree: true,
          attributes: true,
          attributeFilter: ["content"],
        });
      };
      const applyViewportFit = () => {
        ensureViewportFit();
        watchViewportFit();
      };
      if (document.head) {
        applyViewportFit();
      } else {
        document.addEventListener("DOMContentLoaded", applyViewportFit, { once: true });
      }
      const callbacks = new Set();
      const openPathCallbacks = new Set();
      const viewModeCallbacks = new Set();
      const defineEmit = (name, fn) => {
        Object.defineProperty(window, name, {
          configurable: false,
          enumerable: false,
          writable: false,
          value: fn,
        });
      };
      defineEmit("__omnigentNativeEmitNotificationActivated", (path) => {
        if (typeof path !== "string" || !path.startsWith("/")) return;
        for (const callback of callbacks) {
          try { callback(path); } catch {}
        }
      });
      defineEmit("__omnigentNativeEmitOpenPath", (path) => {
        if (typeof path !== "string" || !path.startsWith("/")) return;
        for (const callback of openPathCallbacks) {
          try { callback(path); } catch {}
        }
      });
      defineEmit("__omnigentNativeEmitViewModeChanged", (mode) => {
        if (mode !== "chat" && mode !== "terminal") return;
        for (const callback of viewModeCallbacks) {
          try { callback(mode); } catch {}
        }
      });
      const serverPickerWaiters = new Set();
      defineEmit("__omnigentNativeEmitServerPicker", (payload) => {
        if (!payload || typeof payload !== "object") return;
        if (typeof payload.currentOrigin !== "string" || !payload.currentOrigin) return;
        const cleanList = (value) =>
          Array.isArray(value) ? value.filter((entry) => typeof entry === "string") : [];
        const info = {
          currentOrigin: payload.currentOrigin,
          managedServers: cleanList(payload.managedServers),
          recentServers: cleanList(payload.recentServers),
        };
        for (const resolve of serverPickerWaiters) {
          try { resolve(info); } catch {}
        }
        serverPickerWaiters.clear();
      });
      const insetCallbacks = new Set();
      // Cache the last footprint so a subscriber that registers AFTER native
      // first emitted (the React app mounts later than document-start) still
      // gets the current value immediately on subscribe.
      let lastInsets = null;
      defineEmit("__omnigentNativeEmitInsets", (topBar, bottomBar) => {
        const insets = {
          topBar: typeof topBar === "number" && Number.isFinite(topBar) ? topBar : 0,
          bottomBar: typeof bottomBar === "number" && Number.isFinite(bottomBar) ? bottomBar : 0,
        };
        lastInsets = insets;
        for (const callback of insetCallbacks) {
          try { callback(insets); } catch {}
        }
      });
      const sidebarDragCallbacks = new Set();
      Object.defineProperty(window, "__omnigentNativeEmitSidebarDrag", {
        configurable: false,
        enumerable: false,
        writable: false,
        value(phase, progress) {
          if (typeof phase !== "string") return;
          const fraction =
            typeof progress === "number" && Number.isFinite(progress)
              ? Math.max(0, Math.min(1, progress))
              : 0;
          for (const callback of sidebarDragCallbacks) {
            try { callback(phase, fraction); } catch {}
          }
        },
      });
      window.omnigentNative = Object.freeze({
        kind: "ios",
        setColorScheme(scheme) {
          if (scheme !== "light" && scheme !== "dark" && scheme !== "system") return;
          window.webkit.messageHandlers.omnigentNative.postMessage({
            method: "setColorScheme",
            scheme,
          });
        },
        setBadgeCount(count) {
          window.webkit.messageHandlers.omnigentNative.postMessage({
            method: "setBadgeCount",
            count: Number.isFinite(count) ? count : 0,
          });
        },
        notify(params) {
          window.webkit.messageHandlers.omnigentNative.postMessage({
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
          callbacks.add(callback);
          return () => callbacks.delete(callback);
        },
        onOpenPath(callback) {
          if (typeof callback !== "function") return () => {};
          openPathCallbacks.add(callback);
          return () => openPathCallbacks.delete(callback);
        },
        onSidebarDrag(callback) {
          if (typeof callback !== "function") return () => {};
          sidebarDragCallbacks.add(callback);
          return () => sidebarDragCallbacks.delete(callback);
        },
        setServerSwitcherHidden(hidden) {
          window.webkit.messageHandlers.omnigentNative.postMessage({
            method: "setServerSwitcherHidden",
            hidden: hidden === true,
          });
        },
        setSidebarOpen(open) {
          window.webkit.messageHandlers.omnigentNative.postMessage({
            method: "setServerSwitcherHidden",
            hidden: open === true,
          });
        },
        setViewMode(params) {
          const mode = params && params.mode === "terminal" ? "terminal" : "chat";
          window.webkit.messageHandlers.omnigentNative.postMessage({
            method: "setViewMode",
            mode,
            terminalEnabled: !!(params && params.terminalEnabled),
            terminalStartingUp: !!(params && params.terminalStartingUp),
            visible: !!(params && params.visible),
          });
        },
        onViewModeChanged(callback) {
          if (typeof callback !== "function") return () => {};
          viewModeCallbacks.add(callback);
          return () => viewModeCallbacks.delete(callback);
        },
        onNativeInsets(callback) {
          if (typeof callback !== "function") return () => {};
          insetCallbacks.add(callback);
          if (lastInsets) { try { callback(lastInsets); } catch {} }
          return () => insetCallbacks.delete(callback);
        },
        getServerPicker() {
          // Always fetch fresh rather than caching: the picker re-reads on
          // every menu open so a runtime MDM profile change appears without a
          // reload, matching the Electron shell's per-call read. Native
          // answers each request with an emit, resolving every waiter.
          const pending = new Promise((resolve) => { serverPickerWaiters.add(resolve); });
          window.webkit.messageHandlers.omnigentNative.postMessage({
            method: "requestServerPicker",
          });
          return pending;
        },
        switchServer(url) {
          if (typeof url === "string") {
            window.webkit.messageHandlers.omnigentNative.postMessage({
              method: "switchServer",
              url,
            });
          }
          return Promise.resolve();
        },
        openServerSetup() {
          window.webkit.messageHandlers.omnigentNative.postMessage({
            method: "openServerSetup",
          });
        },
      });
    })();
    """

  @MainActor
  final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler,
    UIGestureRecognizerDelegate
  {
    var parent: OmnigentWebView
    private weak var webView: WKWebView?
    /// The server this web view is pinned to; nil before the first load. Doubles as
    /// the identity `updateUIView` compares against, so a re-render only reloads
    /// when SwiftUI hands over a different server.
    private(set) var pinnedURL: URL?
    /// Derived, never stored: a cached copy would be one more thing to keep in sync
    /// with `pinnedURL`.
    private var pinnedOrigin: String? { pinnedURL?.omnigentOrigin }
    /// Bare-root → mount bounces since the last app page loaded; see
    /// `workspaceRootBounceTarget` for why they're capped.
    private var rootBounces = 0
    private static let maxRootBounces = 1
    private var urlObservation: NSKeyValueObservation?
    private let oidcLoginManager = OidcLoginManager()

    init(_ parent: OmnigentWebView) {
      self.parent = parent
    }

    func attach(_ webView: WKWebView) {
      self.webView = webView
      // In-page navigation: the SPA swapped the URL with pushState / replaceState,
      // or the user moved through history. No page is loaded, so no navigation
      // delegate callback runs — KVO on `url` is the only way to observe the user
      // routing client-side back to the workspace root.
      urlObservation = webView.observe(\.url) { [weak self] _, _ in
        Task { @MainActor in
          guard let self, let webView = self.webView else { return }
          self.bounceIfWorkspaceRoot(webView)
        }
      }
    }

    func detach() {
      parent.model.cancelServerSwitcherWatchdog()
      urlObservation = nil
      oidcLoginManager.cancel()
      webView = nil
    }

    /// Send a landing on the bare Databricks workspace root to the SPA mount. The
    /// root serves the Databricks landing page, so leaving the user there hides the
    /// app and lets them wander into another workspace app with no way back.
    private func bounceIfWorkspaceRoot(_ webView: WKWebView) {
      guard let url = webView.url, url.omnigentOrigin == pinnedOrigin,
        let target = workspaceRootBounceTarget(for: url)
      else {
        return
      }
      bounce(webView, to: target)
    }

    /// The mount URL to bounce to when `url` is a bare Databricks workspace root, or
    /// nil when there's nothing to do.
    ///
    /// Budgeted, and spent by the caller that acts on it: a workspace that answers
    /// the mount with a redirect back to the root (e.g. it isn't enabled there)
    /// would otherwise loop forever. One bounce per app page load, so a failed
    /// bounce leaves the user on the root and `didFinish` re-arms the budget as soon
    /// as an app page loads.
    private func workspaceRootBounceTarget(for url: URL) -> URL? {
      guard let target = WorkspaceURLExpander.workspaceUIURL(forBareRoot: url) else { return nil }
      guard rootBounces < Self.maxRootBounces else { return nil }
      rootBounces += 1
      return target
    }

    /// Posted, never loaded inline: a load issued while WebKit is committing a
    /// navigation can be dropped.
    private func bounce(_ webView: WKWebView, to target: URL) {
      DispatchQueue.main.async { webView.load(URLRequest(url: target)) }
    }

    // A left-edge swipe drives the web app's sidebar as an interactive drawer.
    // The sidebar's right edge tracks the finger — progress 0→1 maps the drag
    // across the view width to closed→open — and on release we settle open or
    // closed from how far it was dragged and the flick velocity. This replaces
    // the native back gesture (disabled above), which owned this same edge.
    private static let openProgressThreshold = 0.33
    private static let openVelocityThreshold: CGFloat = 600

    @objc func handleLeftEdgePan(_ recognizer: UIScreenEdgePanGestureRecognizer) {
      guard let view = recognizer.view, view.bounds.width > 0 else { return }
      let width = view.bounds.width
      let progress = Double(max(0, min(width, recognizer.translation(in: view).x)) / width)

      switch recognizer.state {
      case .began:
        parent.model.emitSidebarDrag(phase: "begin", progress: progress)
      case .changed:
        parent.model.emitSidebarDrag(phase: "move", progress: progress)
      case .ended:
        let velocity = recognizer.velocity(in: view).x
        let open = progress > Self.openProgressThreshold || velocity > Self.openVelocityThreshold
        parent.model.emitSidebarDrag(phase: open ? "open" : "close", progress: progress)
      case .cancelled, .failed:
        parent.model.emitSidebarDrag(phase: "close", progress: progress)
      default:
        break
      }
    }

    // Let the edge swipe coexist with the page's own scrolling/pan gestures.
    func gestureRecognizer(
      _ gestureRecognizer: UIGestureRecognizer,
      shouldRecognizeSimultaneouslyWith other: UIGestureRecognizer
    ) -> Bool {
      true
    }

    func load(_ url: URL, in webView: WKWebView) {
      pinnedURL = url
      // A new pin is a new server: the previous one's exhausted bounce budget must
      // not suppress the first bounce here.
      rootBounces = 0
      publishModelChanges { model in
        model.currentURL = url
        model.serverSwitcherHidden = true
      }
      webView.load(URLRequest(url: url))
    }

    func userContentController(
      _ userContentController: WKUserContentController, didReceive message: WKScriptMessage
    ) {
      guard isTrustedBridgeMessage(message) else { return }
      // Any trusted message proves the page is alive and driving the bridge, so
      // stand down the liveness watchdog — the page owns the switcher from here.
      parent.model.cancelServerSwitcherWatchdog()
      guard let body = message.body as? [String: Any],
        let method = body["method"] as? String
      else { return }

      switch method {
      case "setColorScheme":
        guard let scheme = body["scheme"] as? String,
          let source = ThemeSource(rawValue: scheme)
        else { return }
        ThemeController.shared.apply(source)
      case "setBadgeCount":
        let count = (body["count"] as? NSNumber)?.intValue ?? 0
        NativeNotificationManager.shared.setBadgeCount(count)
      case "notify":
        guard let params = body["params"] as? [String: Any],
          let title = params["title"] as? String,
          !title.isEmpty
        else { return }
        NativeNotificationManager.shared.notify(
          title: title,
          body: params["body"] as? String,
          navigatePath: params["navigatePath"] as? String
        )
      case "setServerSwitcherHidden":
        parent.model.serverSwitcherHidden = (body["hidden"] as? NSNumber)?.boolValue ?? true
      case "setSidebarOpen":
        parent.model.serverSwitcherHidden = (body["open"] as? NSNumber)?.boolValue ?? true
      case "requestServerPicker":
        parent.pushServerPicker()
      case "switchServer":
        guard let urlString = body["url"] as? String else { return }
        parent.requestSwitchServer(urlString)
      case "openServerSetup":
        parent.openServerSetup()
      case "setViewMode":
        let mode: WebViewMode = (body["mode"] as? String) == "terminal" ? .terminal : .chat
        parent.model.viewMode = mode
        parent.model.terminalEnabled = (body["terminalEnabled"] as? NSNumber)?.boolValue ?? false
        parent.model.terminalStartingUp =
          (body["terminalStartingUp"] as? NSNumber)?.boolValue ?? false
        parent.model.bottomBarVisible = (body["visible"] as? NSNumber)?.boolValue ?? false
      default:
        return
      }
    }

    func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
      if let url = webView.url,
        ["http", "https"].contains(url.scheme?.lowercased() ?? ""),
        url.omnigentOrigin != pinnedOrigin
      {
        webView.stopLoading()
        startLogin(in: webView)
        return
      }
      parent.model.isLoading = true
      parent.model.currentURL = webView.url ?? parent.model.currentURL
      parent.model.serverSwitcherHidden = true
      // Hide the Chat/Terminal bar for the load too: the incoming page pushes
      // its own truth via setViewMode once it mounts (current SPAs keep it
      // hidden — the switcher lives in their header), so a stale visible bar
      // from the previous page must not float over the new one while it boots.
      parent.model.bottomBarVisible = false
      parent.model.armServerSwitcherWatchdog()
    }

    func webView(_ webView: WKWebView, didCommit navigation: WKNavigation!) {
      parent.model.currentURL = webView.url ?? parent.model.currentURL
      // Workspace roots are caught here too, not only in decidePolicyFor: that
      // callback is skipped for loads the shell starts itself, and the Databricks
      // login chain hands the session back with a form POST landing on the root.
      // didCommit sees every committed main-frame load.
      bounceIfWorkspaceRoot(webView)
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
      parent.model.isLoading = false
      parent.model.currentURL = webView.url ?? parent.model.currentURL
      // A page other than the bare root finished loading, so the last bounce (if
      // any) got us somewhere: re-arm the budget for the next landing on the root.
      // Deliberately loose — an auth page also re-arms, which at worst costs one
      // extra bounce attempt rather than stranding the user on the root.
      if let url = webView.url, url.omnigentOrigin == pinnedOrigin,
        WorkspaceURLExpander.workspaceUIURL(forBareRoot: url) == nil
      {
        rootBounces = 0
      }
      // Databricks workspace-hosted Omnigent renders inside the workspace's
      // top-nav chrome (the SPA is a workspace page). Hide it by overlaying
      // Omnigent's own root — see WorkspaceChromeScript, which also explains why
      // this is keyed on the pinned origin and never on the URL's path.
      // Re-applied on every full load (a server switch is a fresh document); the
      // SPA's client-side routing keeps the same document, so the injected
      // stylesheet persists across in-app navigation.
      if pinnedOrigin != nil, webView.url?.omnigentOrigin == pinnedOrigin {
        webView.evaluateJavaScript(WorkspaceChromeScript.source)
        parent.loadSucceeded()
      }
    }

    func webView(
      _ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!,
      withError error: Error
    ) {
      handleLoadFailure(webView, error: error)
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
      handleLoadFailure(webView, error: error)
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
      webView.reload()
    }

    func webView(
      _ webView: WKWebView,
      decidePolicyFor navigationAction: WKNavigationAction,
      decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
      guard let url = navigationAction.request.url,
        let scheme = url.scheme?.lowercased()
      else {
        decisionHandler(.cancel)
        return
      }

      if navigationAction.targetFrame == nil {
        openExternal(url)
        decisionHandler(.cancel)
        return
      }

      // A main-frame landing on the bare workspace root belongs to Databricks
      // rather than the app — cancel it and load the SPA mount instead.
      if navigationAction.targetFrame?.isMainFrame == true, url.omnigentOrigin == pinnedOrigin,
        let target = workspaceRootBounceTarget(for: url)
      {
        decisionHandler(.cancel)
        bounce(webView, to: target)
        return
      }

      if navigationAction.targetFrame?.isMainFrame == true,
        ["http", "https"].contains(scheme),
        url.omnigentOrigin != pinnedOrigin
      {
        if navigationAction.navigationType == .linkActivated {
          openExternal(url)
        } else {
          startLogin(in: webView)
        }
        decisionHandler(.cancel)
        return
      }

      if ["http", "https", "about", "blob", "data"].contains(scheme) {
        decisionHandler(.allow)
        return
      }

      if scheme == "mailto" {
        UIApplication.shared.open(url)
        decisionHandler(.cancel)
        return
      }

      promptForExternalURL(url, scheme: scheme)
      decisionHandler(.cancel)
    }

    func webView(
      _ webView: WKWebView,
      createWebViewWith configuration: WKWebViewConfiguration,
      for navigationAction: WKNavigationAction,
      windowFeatures: WKWindowFeatures
    ) -> WKWebView? {
      if navigationAction.targetFrame == nil, let url = navigationAction.request.url {
        openExternal(url)
      }
      return nil
    }

    func webView(
      _ webView: WKWebView,
      requestMediaCapturePermissionFor origin: WKSecurityOrigin,
      initiatedByFrame frame: WKFrameInfo,
      type: WKMediaCaptureType,
      decisionHandler: @escaping (WKPermissionDecision) -> Void
    ) {
      guard Self.isAllowedMediaCaptureType(type),
        origin.omnigentOrigin == pinnedOrigin,
        webView.url?.omnigentOrigin == pinnedOrigin
      else {
        decisionHandler(.deny)
        return
      }
      decisionHandler(.grant)
    }

    private static func isAllowedMediaCaptureType(_ type: WKMediaCaptureType) -> Bool {
      switch type {
      case .camera, .microphone, .cameraAndMicrophone:
        return true
      @unknown default:
        return false
      }
    }

    private func isTrustedBridgeMessage(_ message: WKScriptMessage) -> Bool {
      guard let pinnedOrigin else { return false }
      guard message.frameInfo.securityOrigin.omnigentOrigin == pinnedOrigin else { return false }
      guard webView?.url?.omnigentOrigin == pinnedOrigin else { return false }
      return message.frameInfo.isMainFrame
    }

    private func openExternal(_ url: URL) {
      guard let scheme = url.scheme?.lowercased() else { return }
      if ["http", "https", "mailto"].contains(scheme) {
        UIApplication.shared.open(url)
        return
      }
      promptForExternalURL(url, scheme: scheme)
    }

    private func startLogin(in webView: WKWebView) {
      guard let pinnedOrigin else { return }
      oidcLoginManager.start(
        origin: pinnedOrigin,
        cookieStore: webView.configuration.websiteDataStore.httpCookieStore
      ) { [weak self, weak webView] in
        guard let self, let webView, let pinnedURL = self.pinnedURL else { return }
        webView.load(URLRequest(url: pinnedURL))
      }
    }

    private func promptForExternalURL(_ url: URL, scheme: String) {
      let onPinnedServer = pinnedOrigin != nil && webView?.url?.omnigentOrigin == pinnedOrigin

      if let pinnedOrigin, onPinnedServer,
        parent.settings.isProtocolAllowed(scheme, from: pinnedOrigin)
      {
        UIApplication.shared.open(url)
        return
      }

      let requester = webView?.url?.omnigentOrigin ?? "This page"
      let alert = UIAlertController(
        title: "Open this \(scheme) link?",
        message: "\(requester) wants to open:\n\n\(url.absoluteString)",
        preferredStyle: .alert
      )
      alert.addAction(UIAlertAction(title: "Cancel", style: .cancel))
      alert.addAction(
        UIAlertAction(title: "Open", style: .default) { _ in
          UIApplication.shared.open(url)
        })
      if let pinnedOrigin, onPinnedServer {
        alert.addAction(
          UIAlertAction(title: "Always Allow", style: .default) { [weak self] _ in
            guard let self else { return }
            self.parent.settings.allowProtocol(scheme, from: pinnedOrigin)
            UIApplication.shared.open(url)
          })
      }
      topViewController()?.present(alert, animated: true)
    }

    private func handleLoadFailure(_ webView: WKWebView, error: Error) {
      let nsError = error as NSError
      guard nsError.code != NSURLErrorCancelled else { return }
      parent.model.isLoading = false
      parent.model.cancelServerSwitcherWatchdog()

      let failedURL = failedURL(from: nsError) ?? webView.url ?? pinnedURL ?? parent.initialURL
      guard failedURL.omnigentOrigin == pinnedOrigin else { return }
      parent.loadFailed(failedURL, error.localizedDescription)
    }

    private func publishModelChanges(_ update: @escaping @MainActor (WebViewModel) -> Void) {
      let model = parent.model
      Task { @MainActor in
        update(model)
      }
    }

    private func failedURL(from error: NSError) -> URL? {
      // The string-keyed variant this used to fall back on is deprecated and
      // redundant on the supported OS versions; callers already fall back to the
      // web view's own URL when the key is absent.
      error.userInfo[NSURLErrorFailingURLErrorKey] as? URL
    }

    private func topViewController() -> UIViewController? {
      let scene = UIApplication.shared.connectedScenes
        .compactMap { $0 as? UIWindowScene }
        .first { $0.activationState == .foregroundActive }
      let root = scene?.windows.first { $0.isKeyWindow }?.rootViewController
      return root?.omnigentTopViewController
    }
  }
}

private final class AccessoryFreeWebView: WKWebView {
  override var inputAccessoryView: UIView? {
    nil
  }
}

extension UIViewController {
  fileprivate var omnigentTopViewController: UIViewController {
    if let presentedViewController {
      return presentedViewController.omnigentTopViewController
    }
    if let navigation = self as? UINavigationController,
      let visible = navigation.visibleViewController
    {
      return visible.omnigentTopViewController
    }
    if let tab = self as? UITabBarController,
      let selected = tab.selectedViewController
    {
      return selected.omnigentTopViewController
    }
    return self
  }
}

/// The user-selected theme source. Mirrors the value space the web app sends
/// through the bridge via `setColorScheme`.
enum ThemeSource: String, Equatable, CaseIterable {
  case system
  case light
  case dark

  /// UIKit interface style override used for windows and WKWebView.
  var userInterfaceStyle: UIUserInterfaceStyle {
    switch self {
    case .system:
      return .unspecified
    case .light:
      return .light
    case .dark:
      return .dark
    }
  }

  /// SwiftUI's equivalent for `.preferredColorScheme`. `nil` means "follow the system".
  var colorScheme: ColorScheme? {
    switch self {
    case .system:
      return nil
    case .light:
      return .light
    case .dark:
      return .dark
    }
  }
}

/// App-wide theme override. The web layer drives this through the bridge so
/// native chrome and the WebView track the in-app theme switcher.
@MainActor
final class ThemeController: ObservableObject {
  static let shared = ThemeController()

  @Published private(set) var source: ThemeSource = .system

  private init() {}

  func apply(_ source: ThemeSource) {
    self.source = source
    for scene in UIApplication.shared.connectedScenes.compactMap({ $0 as? UIWindowScene }) {
      for window in scene.windows {
        window.overrideUserInterfaceStyle = source.userInterfaceStyle
      }
    }
  }
}
