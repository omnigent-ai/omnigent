import SwiftUI
import UIKit
import WebKit

struct OmnigentWebView: UIViewRepresentable {
  let initialURL: URL
  @ObservedObject var model: WebViewModel
  @ObservedObject var settings: SettingsStore
  let loadFailed: (URL, String) -> Void
  let loadSucceeded: (URL) -> Void

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
      if (document.head) {
        ensureViewportFit();
      } else {
        document.addEventListener("DOMContentLoaded", ensureViewportFit, { once: true });
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
      });
    })();
    """

  @MainActor
  final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler,
    UIGestureRecognizerDelegate
  {
    var parent: OmnigentWebView
    private weak var webView: WKWebView?
    private(set) var pinnedURL: URL?
    private var pinnedOrigin: String?

    init(_ parent: OmnigentWebView) {
      self.parent = parent
    }

    func attach(_ webView: WKWebView) {
      self.webView = webView
    }

    func detach() {
      parent.model.cancelServerSwitcherWatchdog()
      webView = nil
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
      pinnedOrigin = url.omnigentOrigin
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
      parent.model.isLoading = true
      parent.model.currentURL = webView.url ?? parent.model.currentURL
      parent.model.serverSwitcherHidden = true
      parent.model.armServerSwitcherWatchdog()
    }

    func webView(_ webView: WKWebView, didCommit navigation: WKNavigation!) {
      parent.model.currentURL = webView.url ?? parent.model.currentURL
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
      parent.model.isLoading = false
      parent.model.currentURL = webView.url ?? parent.model.currentURL
      if webView.url?.path.starts(with: WorkspaceURLExpander.workspaceUIPath) == true {
        injectWorkspaceChromeCSS(webView)
      }
      if webView.url?.omnigentOrigin == pinnedOrigin, let pinnedURL {
        parent.loadSucceeded(pinnedURL)
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
      if let url = error.userInfo[NSURLErrorFailingURLErrorKey] as? URL {
        return url
      }
      if let value = error.userInfo[NSURLErrorFailingURLStringErrorKey] as? String {
        return URL(string: value)
      }
      return nil
    }

    private func injectWorkspaceChromeCSS(_ webView: WKWebView) {
      let css = """
        .omnigent-app {
          position: fixed !important;
          inset: 0 !important;
          z-index: 2147483647 !important;
        }
        """
      let script = """
        (() => {
          if (document.querySelector("style[data-omnigent-workspace-chrome]")) return;
          const style = document.createElement("style");
          style.dataset.omnigentWorkspaceChrome = "true";
          style.textContent = \(WebViewModel.javascriptString(css));
          document.documentElement.appendChild(style);
        })();
        """
      webView.evaluateJavaScript(script)
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
