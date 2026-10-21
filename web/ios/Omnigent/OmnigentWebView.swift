import GameController
import SwiftUI
import UIKit
import WebKit

struct OmnigentWebView: UIViewRepresentable {
  let initialURL: URL
  @ObservedObject var model: WebViewModel
  @ObservedObject var settings: SettingsStore
  let databricksInternalFeaturesEnabled: Bool
  /// A nil message returns to setup after cancellation without showing an error.
  let loadFailed: (URL, String?) -> Void
  let loadSucceeded: () -> Void
  /// Compose and push the current server-picker payload to the SPA.
  let pushServerPicker: () -> Void
  /// Switch the shell to a picker-listed server. The owner validates the
  /// target against the managed/recent allow list before acting.
  let requestSwitchServer: (String) -> Void
  /// Return the shell to its "connect to server" setup page.
  let openServerSetup: () -> Void
  var connectionIntent: DatabricksConnectionIntent = .connect
  var recoveryPageURL: URL?
  var recoverWorkspace: ((URL) -> Void)?
  var workspaceReady: (() -> Void)?
  var reauthenticateWorkspace: ((URL) -> Void)?
  var signedOut: ((DatabricksWebContext, Task<Void, Error>) -> Void)?
  /// The clean server URL (no conversation path); its mount locates the OIDC auth routes.
  var serverURL: URL?
  /// An OIDC session can't be renewed without the sign-in sheet: the page to return to and why.
  var requireSignIn: ((URL, String) -> Void)?
  /// The user signed out of a native-OIDC server: the server and the Connect screen message.
  var serverSignedOut: ((URL, String) -> Void)?

  static func connectionErrorMessage(
    for error: Error, databricksInternalFeaturesEnabled: Bool
  ) -> String {
    let nsError = error as NSError
    if databricksInternalFeaturesEnabled, nsError.domain == NSURLErrorDomain,
      [NSURLErrorCannotFindHost, NSURLErrorDNSLookupFailed].contains(nsError.code)
    {
      return "Couldn’t reach the server. Check your device’s compliance status in Jamf."
    }
    return error.localizedDescription
  }

  func makeCoordinator() -> Coordinator {
    Coordinator(self)
  }

  func makeUIView(context: Context) -> WKWebView {
    let contentController = WKUserContentController()
    contentController.add(context.coordinator, name: "omnigentNative")
    contentController.addUserScript(
      WKUserScript(
        source: Self.nativeBridgeScript(managesWorkspace: context.coordinator.webStore != nil),
        injectionTime: .atDocumentStart,
        forMainFrameOnly: true
      )
    )

    let configuration = WKWebViewConfiguration()
    configuration.userContentController = contentController
    configuration.allowsInlineMediaPlayback = true
    configuration.websiteDataStore = context.coordinator.websiteDataStore

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

  static func nativeBridgeScript(managesWorkspace: Bool) -> String {
    """
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
          canSignOut: payload.canSignOut === true,
        };
        for (const resolve of serverPickerWaiters) {
          try { resolve(info); } catch {}
        }
        serverPickerWaiters.clear();
      });
      const signOutWaiters = new Set();
      defineEmit("__omnigentNativeEmitSignOutResult", (handled) => {
        for (const resolve of signOutWaiters) {
          try { resolve(handled === true); } catch {}
        }
        signOutWaiters.clear();
      });
      const insetCallbacks = new Set();
      const keyboardViewportCallbacks = new Set();
      let keyboardViewport = null;
      defineEmit("__omnigentNativeEmitKeyboardViewport", (width, height) => {
        if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) return;
        keyboardViewport = { width, height };
        for (const callback of keyboardViewportCallbacks) {
          try { callback(); } catch {}
        }
      });
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
        setDocumentScrollEnabled(enabled) {
          window.webkit.messageHandlers.omnigentNative.postMessage({
            method: "setDocumentScrollEnabled", enabled: !!enabled,
          });
        },
        getKeyboardViewport() { return keyboardViewport; },
        onKeyboardViewportChanged(callback) {
          if (typeof callback !== "function") return () => {};
          keyboardViewportCallbacks.add(callback);
          return () => keyboardViewportCallbacks.delete(callback);
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
        \(managesWorkspace ? "signOut() { window.webkit.messageHandlers.omnigentNative.postMessage({ method: 'signOut' }); }," : "")
        signOutOfServer() {
          // Native answers true once it has taken over the sign-out, false when it can't.
          const pending = new Promise((resolve) => { signOutWaiters.add(resolve); });
          window.webkit.messageHandlers.omnigentNative.postMessage({
            method: "signOutOfServer",
          });
          return pending;
        },
        openServerSetup() {
          window.webkit.messageHandlers.omnigentNative.postMessage({
            method: "openServerSetup",
          });
        },
      });
      window.webkit.messageHandlers.omnigentNative.postMessage({
        method: "requestKeyboardViewport",
      });
    })();
    """
  }

  @MainActor
  final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler,
    UIGestureRecognizerDelegate, UIScrollViewDelegate
  {
    var parent: OmnigentWebView
    private weak var webView: WKWebView?
    /// The server this web view is pinned to; nil before the first load. Doubles as
    /// the identity `updateUIView` compares against, so a re-render only reloads
    /// when SwiftUI hands over a different server.
    private(set) var pinnedURL: URL?
    /// Derived, never stored: a cached copy would be one more thing to keep in sync
    /// with `pinnedURL`.
    private var effectiveOrigin: String?
    private var pinnedOrigin: String? { effectiveOrigin }
    private var pinnedAuthentication: ServerAuthentication {
      ServerAuthentication(origin: pinnedOrigin)
    }
    /// Bare-root → mount bounces since the last app page loaded; see
    /// `workspaceRootBounceTarget` for why they're capped.
    private var rootBounces = 0
    private static let maxRootBounces = 1
    private var urlObservation: NSKeyValueObservation?
    /// Legacy ticket sign-in for OIDC servers without native sign-in in their manifest.
    /// Deprecated: removal targeted for iOS 0.5.0.
    private var oidcLoginManager = OidcLoginManager()
    private let oidcCredentials = OidcCredentials.shared
    /// Set once a native-OIDC connect succeeds; the shell then owns the session's lifecycle.
    private var oidcConnection: OidcConnection?
    /// The last app page under the mount, reloaded after a renewal the page asked for.
    private var oidcPageURL: URL?
    private var oidcRenewalTask: Task<Void, Never>?
    private var oidcRenewalTimer: Task<Void, Never>?
    private var oidcRenewalGuard = OidcRenewalGuard()
    private var oidcReloadRequested = false
    /// Why a background renewal lost the grant, kept for the next "Sign in again?" prompt.
    private var oidcRenewalCause: OidcSignInError?
    /// The "Sign in again?" alert is up; the stalled page must not trigger more renewals.
    private var oidcSignInRequired = false
    private var windowWaiter: (id: UUID, continuation: CheckedContinuation<UIWindow, Error>)?
    let websiteDataStore: WKWebsiteDataStore
    private let contextResult: Result<DatabricksWebContext?, Error>
    fileprivate let webStore: DatabricksWebStore?
    private let workspaceBootstrap: DatabricksWorkspaceBootstrap
    private var workspaceSession: DatabricksWebSession?
    private var authenticationTask: Task<Void, Never>?
    private var navigationID = UUID()
    private var lastWorkspacePageURL: URL?
    private var activationObserver: NSObjectProtocol?
    private var activationTask: Task<Void, Never>?
    private var reportedWorkspaceReady = false

    init(
      _ parent: OmnigentWebView, context: DatabricksWebContext? = nil,
      bootstrap: DatabricksWorkspaceBootstrap? = nil
    ) {
      self.parent = parent
      let result = Result { try context ?? DatabricksWebContext.resolve(parent.initialURL) }
      contextResult = result
      workspaceBootstrap = bootstrap ?? DatabricksWorkspaceBootstrap()
      switch result {
      case .success(let context?):
        let store = DatabricksWebStore(identifier: context.storeIdentifier)
        webStore = store
        websiteDataStore = store.websiteDataStore
      case .success(nil):
        webStore = nil
        websiteDataStore = .default()
      case .failure:
        webStore = nil
        websiteDataStore = .nonPersistent()
      }
    }

    func attach(_ webView: WKWebView) {
      webView.scrollView.delegate = self
      self.webView = webView
      activationObserver = NotificationCenter.default.addObserver(
        forName: UIApplication.didBecomeActiveNotification, object: nil, queue: .main
      ) { [weak self] _ in
        Task { @MainActor in
          self?.checkWorkspaceSessionOnActivation()
          self?.checkOidcSessionOnActivation()
        }
      }
      // In-page navigation: the SPA swapped the URL with pushState / replaceState,
      // or the user moved through history. No page is loaded, so no navigation
      // delegate callback runs — KVO on `url` is the only way to observe the user
      // routing client-side back to the workspace root.
      urlObservation = webView.observe(\.url) { [weak self] _, _ in
        Task { @MainActor in
          guard let self, let webView = self.webView else { return }
          if let session = self.workspaceSession, let url = webView.url {
            if session.isSignOutURL(url) {
              self.requestSignOut()
              return
            }
            if session.navigationURL(for: url) == nil {
              if session.isAuthenticationURL(url) {
                self.requestWorkspaceRecovery()
              } else {
                self.showWorkspaceFailure(DatabricksSessionError.workspaceChanged)
              }
              return
            }
            self.lastWorkspacePageURL = session.navigationURL(for: url)
          }
          if let url = webView.url { self.rememberOidcPage(url) }
          self.bounceIfWorkspaceRoot(webView)
        }
      }
    }

    func detach() {
      webView?.scrollView.delegate = nil
      navigationID = UUID()
      activationTask?.cancel()
      activationTask = nil
      if let activationObserver { NotificationCenter.default.removeObserver(activationObserver) }
      activationObserver = nil
      authenticationTask?.cancel()
      workspaceBootstrap.cancel()
      let detachedWebView = webView
      (detachedWebView as? AccessoryFreeWebView)?.onWindowAvailable = nil
      detachedWebView?.stopLoading()
      detachedWebView?.navigationDelegate = nil
      detachedWebView?.uiDelegate = nil
      let model = parent.model
      if model.webView === detachedWebView {
        model.cancelServerSwitcherWatchdog()
        model.cancelAuthentication = nil
        model.signOut = nil
        #if DEBUG
          model.injectDebugFault = nil
          model.injectOidcDebugFault = nil
        #endif
        model.webView = nil
        // SwiftUI dismantles representables while mutating its graph. Publishing here would
        // re-enter graph invalidation, so wait until teardown completes and skip replacements.
        DispatchQueue.main.async { [weak model] in
          guard model?.webView == nil else { return }
          model?.isAuthenticating = false
          model?.hidesPage = false
        }
      }
      urlObservation = nil
      oidcLoginManager.cancel()
      stopOidcSession()
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
      navigationID = UUID()
      activationTask?.cancel()
      activationTask = nil
      reportedWorkspaceReady = false
      lastWorkspacePageURL = nil
      authenticationTask?.cancel()
      authenticationTask = nil
      workspaceBootstrap.cancel()
      oidcLoginManager.cancel()
      oidcLoginManager = OidcLoginManager()
      stopOidcSession()
      (webView as? AccessoryFreeWebView)?.onWindowAvailable = nil
      webView.stopLoading()
      // Another server's page must not stay usable while this one's manifest is read.
      let coversPreviousPage =
        contextResult.isGenericOidc(url)
        && webView.url?.omnigentOrigin.map { $0 != url.omnigentOrigin } == true
      pinnedURL = url
      effectiveOrigin = nil
      workspaceSession = nil
      rootBounces = 0
      publishModelChanges { [weak self] model in
        model.currentURL = self?.workspaceSession?.pageURL ?? url
        model.isAuthenticating = self?.webStore != nil && self?.workspaceSession == nil
        model.serverSwitcherHidden = !model.isAuthenticating
        model.isLoading = true
        model.hidesPage = coversPreviousPage
        model.bottomBarVisible = false
        model.signOut = self?.webStore == nil ? nil : { self?.requestSignOut() }
        #if DEBUG
          model.injectOidcDebugFault = nil
          model.injectDebugFault =
            self?.webStore == nil
            ? nil
            : { [weak self] fault in
              guard let self else { return "This workspace view is no longer attached." }
              return await injectDebugFault(fault)
            }
        #endif
        if model.isAuthenticating {
          model.cancelAuthentication = {
            self?.showWorkspaceFailure(DatabricksSessionError.cancelled)
          }
        } else {
          model.cancelAuthentication = nil
        }
      }
      switch contextResult {
      case .success(nil) where contextResult.isGenericOidc(url):
        connectOidcServer(url, in: webView)
      case .success(nil):
        effectiveOrigin = url.omnigentOrigin
        webView.load(URLRequest(url: url))
      case .failure(let error):
        showWorkspaceFailure(error)
      case .success(let context?):
        guard
          let current = try? DatabricksWebContext(url: url, configuration: context.configuration),
          current.storeIdentifier == context.storeIdentifier
        else {
          showWorkspaceFailure(DatabricksSessionError.workspaceChanged)
          return
        }
        if let window = webView.window {
          startWorkspace(current, in: webView, window: window)
        } else if let hosted = webView as? AccessoryFreeWebView {
          hosted.onWindowAvailable = { [weak self, weak webView] window in
            guard let self, let webView else { return }
            self.startWorkspace(current, in: webView, window: window)
          }
        } else {
          showWorkspaceFailure(DatabricksSessionError.presentationUnavailable)
        }
      }
    }

    private func startWorkspace(
      _ context: DatabricksWebContext, in webView: WKWebView, window: UIWindow
    ) {
      guard let webStore, authenticationTask == nil else { return }
      let id = navigationID
      authenticationTask = Task { [weak self, weak webView] in
        guard let self, let webView else { return }
        defer { if navigationID == id { authenticationTask = nil } }
        do {
          let session = try await workspaceBootstrap.prepare(
            context: context, store: webStore, anchor: window,
            intent: parent.connectionIntent, pageURL: parent.recoveryPageURL)
          try Task.checkCancellation()
          guard navigationID == id, self.webView === webView, parent.model.webView === webView
          else { return }
          workspaceSession = session
          lastWorkspacePageURL = session.pageURL
          effectiveOrigin = session.pageURL.omnigentOrigin
          parent.model.isAuthenticating = false
          parent.model.cancelAuthentication = nil
          parent.model.currentURL = session.pageURL
          webView.load(URLRequest(url: session.pageURL))
        } catch {
          guard navigationID == id, self.webView === webView else { return }
          if error as? DatabricksSessionError == .reauthenticationRequired,
            let reauthenticate = parent.reauthenticateWorkspace
          {
            parent.model.isAuthenticating = false
            parent.model.isLoading = false
            parent.model.cancelAuthentication = nil
            reauthenticate(parent.recoveryPageURL ?? context.pageURL)
          } else {
            showWorkspaceFailure(
              error is CancellationError ? DatabricksSessionError.cancelled : error)
          }
        }
      }
    }

    private func requestWorkspaceRecovery() {
      guard let session = workspaceSession else { return }
      let pageURL = lastWorkspacePageURL ?? session.pageURL
      guard let recover = parent.recoverWorkspace else {
        showWorkspaceFailure(DatabricksSessionError.recoveryExhausted)
        return
      }
      navigationID = UUID()
      activationTask?.cancel()
      activationTask = nil
      workspaceSession = nil
      effectiveOrigin = nil
      webView?.stopLoading()
      recover(pageURL)
    }

    private func checkWorkspaceSessionOnActivation() {
      guard activationTask == nil, authenticationTask == nil, let session = workspaceSession,
        let webStore, let view = webView, isCurrent(view)
      else { return }
      let id = navigationID
      let pageURL = lastWorkspacePageURL ?? session.pageURL
      activationTask = Task { [weak self] in
        let cookies = await webStore.cookies()
        guard let self, !Task.isCancelled, navigationID == id, let view = webView, isCurrent(view)
        else { return }
        activationTask = nil
        if !cookies.contains(where: {
          $0.name == "DBAUTH" && !$0.value.isEmpty && $0.isSecure && $0.isHTTPOnly
            && DatabricksSessionClient.cookie($0, appliesTo: pageURL)
        }) {
          requestWorkspaceRecovery()
        }
      }
    }

    private func requestSignOut() {
      guard case .success(let context?) = contextResult, let webStore else { return }
      do {
        let cleanup = try workspaceBootstrap.beginSignOut(context: context, store: webStore)
        let callback = parent.signedOut
        effectiveOrigin = nil
        workspaceSession = nil
        detach()
        if let callback {
          callback(context, cleanup)
        } else {
          parent.loadFailed(parent.initialURL, nil)
        }
      } catch { showWorkspaceFailure(error) }
    }

    // MARK: Native OIDC sign-in

    /// Reads the manifest, then either signs in natively before loading or loads as before.
    private func connectOidcServer(_ pageURL: URL, in webView: WKWebView) {
      let id = navigationID
      let serverURL =
        parent.serverURL.flatMap { $0.omnigentOrigin == pageURL.omnigentOrigin ? $0 : nil }
        ?? pageURL
      let interactive = parent.connectionIntent == .connect
      let returnURL =
        parent.recoveryPageURL.flatMap { OidcWebSession.isPage($0, of: serverURL) ? $0 : nil }
        ?? pageURL
      authenticationTask = Task { [weak self, weak webView] in
        guard let self else { return }
        defer { if navigationID == id { authenticationTask = nil } }
        guard let manifest = try? await ServerManifest.fetch(for: serverURL),
          navigationID == id, let webView, isCurrent(webView)
        else { return }
        guard let cookieName = manifest.nativeSignInCookieName else {
          effectiveOrigin = pageURL.omnigentOrigin
          webView.load(URLRequest(url: pageURL))
          return
        }
        let connection = OidcConnection(serverURL: serverURL, cookieName: cookieName)
        do {
          try await ensureOidcSession(connection, interactive: interactive, in: webView, id: id)
          try Task.checkCancellation()
          guard navigationID == id, isCurrent(webView) else { return }
          oidcConnection = connection
          oidcPageURL = OidcWebSession.isPage(returnURL, of: serverURL) ? returnURL : serverURL
          effectiveOrigin = connection.origin
          let model = parent.model
          model.isAuthenticating = false
          model.cancelAuthentication = nil
          model.currentURL = returnURL
          model.signOut = { [weak self] in self?.requestOidcSignOut() }
          #if DEBUG
            model.injectOidcDebugFault = { [weak self] fault in
              guard let self else { return "This server view is no longer attached." }
              return await injectOidcDebugFault(fault)
            }
          #endif
          webView.load(URLRequest(url: returnURL))
          scheduleOidcRenewal()
        } catch {
          guard navigationID == id, self.webView === webView else { return }
          if !interactive, !OidcWebSession.isNetworkFailure(error), !(error is CancellationError),
            let requireSignIn = parent.requireSignIn
          {
            endOidcProgress()
            requireSignIn(
              returnURL,
              OidcWebSession.reauthenticationMessage(for: error, host: connection.host))
          } else {
            if OidcWebSession.stopsAutoOpening(after: error) {
              parent.settings.suppressAutoOpening(oidcServer: connection.serverURL)
            }
            showWorkspaceFailure(error)
          }
        }
      }
    }

    /// Reuses an accepted session cookie, else renews from the stored grant, else (only when
    /// `interactive`) signs in through the system sheet. Leaves an accepted cookie installed.
    private func ensureOidcSession(
      _ connection: OidcConnection, interactive: Bool, in webView: WKWebView, id: UUID
    ) async throws {
      let store = webView.configuration.websiteDataStore.httpCookieStore
      if let existing = OidcWebSession.liveSessionCookie(
        in: await OidcWebSession.persistedCookies(in: webView.configuration.websiteDataStore),
        named: connection.cookieName,
        serverURL: connection.serverURL, now: Date()),
        try await oidcCredentials.isAccepted(
          token: existing.value, cookieName: connection.cookieName,
          serverURL: connection.serverURL)
      {
        return
      }
      try Task.checkCancellation()
      guard navigationID == id else { throw CancellationError() }
      let model = parent.model
      model.isAuthenticating = true
      model.serverSwitcherHidden = false
      model.cancelAuthentication = { [weak self] in
        self?.showWorkspaceFailure(CancellationError())
      }
      let renewalError: Error
      do {
        try await renewOidcCookie(connection, in: store)
        return
      } catch {
        if !interactive || error is CancellationError || OidcWebSession.isNetworkFailure(error) {
          throw error
        }
        renewalError = error
      }
      let generation = OidcSignOutGenerations.shared.current(origin: connection.origin)
      let minted: OidcSessionToken
      do {
        let window = try await presentationWindow(for: webView, host: connection.host)
        minted = try await oidcCredentials.signIn(serverURL: connection.serverURL, anchor: window)
      } catch is CancellationError where !Task.isCancelled {
        // Closing the sheet keeps the reason it opened, unless there simply was no sign-in.
        throw OidcWebSession.cancelledSignInCause(renewalError) ?? CancellationError()
      }
      guard OidcSignOutGenerations.shared.isCurrent(generation, origin: connection.origin) else {
        // Signed out while the sheet was open: drop the grant this sign-in just saved.
        _ = try? oidcCredentials.signOut(serverURL: connection.serverURL)
        throw CancellationError()
      }
      try await installOidcCookie(minted, for: connection, generation: generation, in: store)
    }

    private func renewOidcCookie(_ connection: OidcConnection, in store: WKHTTPCookieStore)
      async throws
    {
      let generation = OidcSignOutGenerations.shared.current(origin: connection.origin)
      let minted = try await oidcCredentials.refresh(serverURL: connection.serverURL)
      try await installOidcCookie(minted, for: connection, generation: generation, in: store)
    }

    /// Installs a session the server accepts, unless the user signed out since `generation`.
    private func installOidcCookie(
      _ minted: OidcSessionToken, for connection: OidcConnection, generation: Int,
      in store: WKHTTPCookieStore
    ) async throws {
      guard
        try await oidcCredentials.isAccepted(
          token: minted.token, cookieName: connection.cookieName, serverURL: connection.serverURL)
      else { throw OidcSignInError.sessionRejected(host: connection.host) }
      guard OidcSignOutGenerations.shared.isCurrent(generation, origin: connection.origin) else {
        throw CancellationError()
      }
      guard
        let cookie = minted.sessionCookie(
          named: connection.cookieName, serverURL: connection.serverURL)
      else { throw OidcSignInError.sessionRejected(host: connection.host) }
      await store.setCookie(cookie)
      // A sign-out during the write may have cleared cookies before this one landed.
      guard OidcSignOutGenerations.shared.isCurrent(generation, origin: connection.origin) else {
        await store.deleteCookie(cookie)
        throw CancellationError()
      }
      // Read it back so the page never loads before WebKit holds the session.
      let installed = OidcWebSession.liveSessionCookie(
        in: await store.allCookies(), named: connection.cookieName,
        serverURL: connection.serverURL, now: Date())
      guard installed?.value == minted.token else {
        throw OidcSignInError.sessionRejected(host: connection.host)
      }
    }

    /// The window to present the sign-in sheet from, waiting for the web view to join one.
    private func presentationWindow(for webView: WKWebView, host: String) async throws -> UIWindow {
      if let window = webView.window { return window }
      guard let hosted = webView as? AccessoryFreeWebView else {
        throw OidcSignInError.browserUnavailable(host: host)
      }
      // Each wait settles only its own continuation, never a newer connect's.
      let id = UUID()
      return try await withTaskCancellationHandler {
        try await withCheckedThrowingContinuation { continuation in
          guard !Task.isCancelled else {
            continuation.resume(throwing: CancellationError())
            return
          }
          windowWaiter?.continuation.resume(throwing: CancellationError())
          windowWaiter = (id, continuation)
          hosted.onWindowAvailable = { [weak self] window in
            self?.settleWindowWaiter(id, with: .success(window))
          }
        }
      } onCancel: {
        Task { @MainActor [weak self] in
          self?.settleWindowWaiter(id, with: .failure(CancellationError()))
        }
      }
    }

    private func settleWindowWaiter(_ id: UUID, with result: Result<UIWindow, Error>) {
      guard let waiter = windowWaiter, waiter.id == id else { return }
      windowWaiter = nil
      waiter.continuation.resume(with: result)
    }

    /// Clears the connect-time overlays, so "Sign in again?" never sits over a spinner.
    private func endOidcProgress() {
      let model = parent.model
      model.isAuthenticating = false
      model.cancelAuthentication = nil
      model.isLoading = false
      model.hidesPage = false
      model.cancelServerSwitcherWatchdog()
    }

    /// Ends this view's native-OIDC lifecycle; the stored grant and cookie are left alone.
    private func stopOidcSession() {
      oidcConnection = nil
      oidcPageURL = nil
      oidcRenewalTask?.cancel()
      oidcRenewalTask = nil
      oidcRenewalTimer?.cancel()
      oidcRenewalTimer = nil
      oidcRenewalGuard = OidcRenewalGuard()
      oidcReloadRequested = false
      oidcRenewalCause = nil
      oidcSignInRequired = false
      windowWaiter?.continuation.resume(throwing: CancellationError())
      windowWaiter = nil
    }

    private func rememberOidcPage(_ url: URL) {
      guard let connection = oidcConnection, OidcWebSession.isPage(url, of: connection.serverURL)
      else { return }
      oidcPageURL = url
    }

    /// Renews before the cookie expires; a missing or expired cookie renews now. A cookie with
    /// no expiry gets no timer: the page's own sign-in request recovers it.
    private func scheduleOidcRenewal() {
      oidcRenewalTimer?.cancel()
      oidcRenewalTimer = nil
      guard let connection = oidcConnection, !oidcSignInRequired, oidcRenewalTask == nil,
        let store = webView?.configuration.websiteDataStore.httpCookieStore
      else { return }
      let id = navigationID
      oidcRenewalTimer = Task { [weak self] in
        let cookies = await store.allCookies()
        guard let self, !Task.isCancelled, navigationID == id, oidcConnection == connection
        else { return }
        let now = Date()
        var delay: TimeInterval = 0
        if let cookie = OidcWebSession.liveSessionCookie(
          in: cookies, named: connection.cookieName, serverURL: connection.serverURL, now: now)
        {
          guard let expiresAt = cookie.expiresDate else { return }
          delay = OidcWebSession.renewalDelay(expiresAt: expiresAt, now: now)
        }
        if delay > 0 {
          try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
          guard !Task.isCancelled, navigationID == id, oidcConnection == connection else { return }
        }
        oidcRenewalTimer = nil
        renewOidcSession()
      }
    }

    private func checkOidcSessionOnActivation() {
      guard oidcConnection != nil, !oidcSignInRequired, oidcRenewalTask == nil,
        let view = webView, isCurrent(view)
      else { return }
      scheduleOidcRenewal()
    }

    /// The page asked to sign in again (`/auth/login` or a redirect to the IdP): renew and
    /// reload it, unless it asked again right after a renewal.
    private func recoverOidcSession() {
      guard let connection = oidcConnection, !oidcSignInRequired else { return }
      guard oidcRenewalGuard.shouldRenew(now: Date(), renewalPending: oidcRenewalTask != nil)
      else {
        failOidcSession(OidcSignInError.sessionRejected(host: connection.host))
        return
      }
      oidcReloadRequested = true
      renewOidcSession()
    }

    /// Renews from the stored grant. A renewal the page asked for reloads it or fails to the
    /// alert; a background one keeps the current cookie and retries an unreachable server.
    private func renewOidcSession() {
      guard let connection = oidcConnection, !oidcSignInRequired, oidcRenewalTask == nil,
        let view = webView, isCurrent(view)
      else { return }
      oidcRenewalTimer?.cancel()
      oidcRenewalTimer = nil
      let id = navigationID
      let store = view.configuration.websiteDataStore.httpCookieStore
      oidcRenewalTask = Task { [weak self] in
        var failure: Error?
        do {
          try await self?.renewOidcCookie(connection, in: store)
        } catch {
          failure = error
        }
        guard let self, !Task.isCancelled, navigationID == id, oidcConnection == connection,
          !oidcSignInRequired
        else { return }
        oidcRenewalTask = nil
        let reload = oidcReloadRequested
        oidcReloadRequested = false
        if let failure {
          if reload {
            failOidcSession(failure)
          } else if OidcWebSession.isNetworkFailure(failure) {
            retryOidcRenewal()
          } else if let cause = OidcWebSession.rememberedRenewalCause(failure) {
            oidcRenewalCause = cause
          }
          return
        }
        oidcRenewalCause = nil
        scheduleOidcRenewal()
        if reload, let webView {
          webView.load(URLRequest(url: oidcPageURL ?? connection.serverURL))
        }
      }
    }

    private func retryOidcRenewal() {
      oidcRenewalTimer?.cancel()
      let id = navigationID
      let connection = oidcConnection
      oidcRenewalTimer = Task { [weak self] in
        try? await Task.sleep(nanoseconds: UInt64(OidcWebSession.retryDelay * 1_000_000_000))
        guard let self, !Task.isCancelled, navigationID == id, oidcConnection == connection
        else { return }
        oidcRenewalTimer = nil
        renewOidcSession()
      }
    }

    /// The session can't continue: an unreachable server returns to setup with the connection
    /// error; anything else asks "Sign in again?" and never opens the sheet on its own.
    private func failOidcSession(_ error: Error) {
      guard let connection = oidcConnection else { return }
      guard !OidcWebSession.isNetworkFailure(error), !(error is CancellationError),
        let requireSignIn = parent.requireSignIn
      else {
        showWorkspaceFailure(error)
        return
      }
      oidcSignInRequired = true
      oidcRenewalTimer?.cancel()
      oidcRenewalTimer = nil
      webView?.stopLoading()
      endOidcProgress()
      let cause = OidcWebSession.reauthenticationCause(for: error, remembered: oidcRenewalCause)
      oidcRenewalCause = nil
      requireSignIn(
        oidcPageURL ?? connection.serverURL,
        OidcWebSession.reauthenticationMessage(for: cause, host: connection.host))
    }

    /// Forgets the grant (synchronously), clears the session cookie, then hands the Connect
    /// screen its message. Revocation finishes in the background.
    private func requestOidcSignOut() {
      guard let connection = oidcConnection,
        let store = webView?.configuration.websiteDataStore.httpCookieStore
      else { return }
      OidcSignOutGenerations.shared.signOut(origin: connection.origin)
      var complete = true
      do {
        try oidcCredentials.signOut(serverURL: connection.serverURL)
      } catch {
        complete = false
      }
      let message = OidcWebSession.signedOutMessage(host: connection.host, complete: complete)
      let signedOut = parent.serverSignedOut
      let loadFailed = parent.loadFailed
      let initialURL = parent.initialURL
      effectiveOrigin = nil
      detach()
      Task { @MainActor in
        for cookie in OidcWebSession.sessionCookies(
          in: await store.allCookies(), named: connection.cookieName,
          serverURL: connection.serverURL, now: Date())
        {
          await store.deleteCookie(cookie)
        }
        if let signedOut {
          signedOut(connection.serverURL, message)
        } else {
          loadFailed(initialURL, message)
        }
      }
    }

    #if DEBUG
      private func injectOidcDebugFault(_ fault: OidcDebugFault) async -> String {
        guard let connection = oidcConnection,
          let store = webView?.configuration.websiteDataStore.httpCookieStore
        else { return "This server does not use native OIDC sign-in." }
        switch fault {
        case .sessionCookie:
          for cookie in OidcWebSession.sessionCookies(
            in: await store.allCookies(), named: connection.cookieName,
            serverURL: connection.serverURL, now: Date())
          {
            await store.deleteCookie(cookie)
          }
        case .refreshToken:
          guard oidcCredentials.hasStoredGrant(serverURL: connection.serverURL) else {
            return "No saved refresh token for this server. Sign in first."
          }
          do {
            try oidcCredentials.forgetGrantForTesting(serverURL: connection.serverURL)
          } catch { return error.localizedDescription }
        }
        return fault.expectation
      }
    #endif

    #if DEBUG
      /// Break the live session on request so a tester can watch recovery, refresh, and the sign-in
      /// prompt without waiting for a real expiry. Returns what to expect next. Debug builds only.
      private func injectDebugFault(_ fault: DatabricksDebugFault) async -> String {
        guard case .success(let context?) = contextResult, let webStore else {
          return "This server does not use native workspace sign-in."
        }
        do {
          guard try await workspaceBootstrap.inject(fault, context: context, store: webStore) else {
            return "No saved credentials for this workspace. Sign in first."
          }
          return fault.expectation
        } catch { return error.localizedDescription }
      }
    #endif

    /// Nil stays silent after cancellation. Everything else shares the page-load wording, so an
    /// unreachable managed host reads the same during native sign-in as during a page load.
    static func workspaceErrorMessage(
      _ error: Error, databricksInternalFeaturesEnabled: Bool = false
    ) -> String? {
      if error is CancellationError || error as? DatabricksSessionError == .cancelled { return nil }
      return OmnigentWebView.connectionErrorMessage(
        for: error, databricksInternalFeaturesEnabled: databricksInternalFeaturesEnabled)
    }

    /// Also ends a native-OIDC connection or attempt, which shares the same return to setup.
    private func showWorkspaceFailure(_ error: Error) {
      navigationID = UUID()
      effectiveOrigin = nil
      workspaceSession = nil
      let id = navigationID
      authenticationTask?.cancel()
      authenticationTask = nil
      workspaceBootstrap.cancel()
      stopOidcSession()
      (webView as? AccessoryFreeWebView)?.onWindowAvailable = nil
      webView?.stopLoading()
      Task { @MainActor [weak self] in
        guard let self, navigationID == id, webView != nil else { return }
        parent.model.isAuthenticating = false
        parent.model.cancelAuthentication = nil
        parent.model.isLoading = false
        parent.model.hidesPage = false
        parent.model.cancelServerSwitcherWatchdog()
        parent.loadFailed(
          parent.initialURL,
          Self.workspaceErrorMessage(
            error,
            databricksInternalFeaturesEnabled: parent.databricksInternalFeaturesEnabled))
      }
    }

    func scrollViewDidScroll(_ scrollView: UIScrollView) {
      guard scrollView === webView?.scrollView, !scrollView.isScrollEnabled,
        scrollView.contentOffset != .zero
      else { return }
      // Focus scrolling can ignore isScrollEnabled. Clamp before the native
      // frame is displayed instead of correcting the pan later in JavaScript.
      scrollView.setContentOffset(.zero, animated: false)
    }

    func userContentController(
      _ userContentController: WKUserContentController, didReceive message: WKScriptMessage
    ) {
      guard isTrustedBridgeMessage(message) else { return }
      guard let body = message.body as? [String: Any],
        let method = body["method"] as? String
      else { return }
      // Document-start geometry must not wait for slow subresources or count
      // as proof that the web app has mounted for the switcher watchdog.
      if method == "requestKeyboardViewport" {
        (webView as? AccessoryFreeWebView)?.emitKeyboardViewport(force: true)
        return
      }
      // Any trusted message proves the page is alive and driving the bridge, so
      // stand down the liveness watchdog — the page owns the switcher from here.
      parent.model.cancelServerSwitcherWatchdog()
      switch method {
      case "setDocumentScrollEnabled":
        guard let enabled = body["enabled"] as? Bool else { return }
        webView?.scrollView.isScrollEnabled = enabled
      case "signOut":
        requestSignOut()
      case "signOutOfServer":
        // The same sign-out as the native menu, for workspaces and native-OIDC servers.
        guard let signOut = parent.model.signOut else {
          parent.model.emitSignOutResult(false)
          return
        }
        parent.model.emitSignOutResult(true)
        signOut()
      case "setColorScheme":
        guard let scheme = body["scheme"] as? String,
          let source = ThemeSource(rawValue: scheme)
        else { return }
        ThemeController.shared.apply(source)
        markWorkspaceReady()
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
        markWorkspaceReady()
        parent.pushServerPicker()
      case "switchServer":
        guard let urlString = body["url"] as? String else { return }
        parent.requestSwitchServer(urlString)
      case "openServerSetup":
        parent.openServerSetup()
      case "setViewMode":
        markWorkspaceReady()
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

    private func markWorkspaceReady() {
      guard workspaceSession != nil, !reportedWorkspaceReady else { return }
      reportedWorkspaceReady = true
      parent.workspaceReady?()
    }

    private func acceptWorkspaceNavigation(_ url: URL, in webView: WKWebView) -> Bool {
      guard let session = workspaceSession,
        ["http", "https"].contains(url.scheme?.lowercased() ?? "")
      else { return true }
      if session.isSignOutURL(url) {
        webView.stopLoading()
        requestSignOut()
        return false
      }
      if session.navigationURL(for: url) != nil { return true }
      webView.stopLoading()
      if session.isAuthenticationURL(url) || url.omnigentOrigin != pinnedOrigin {
        requestWorkspaceRecovery()
      } else {
        showWorkspaceFailure(DatabricksSessionError.workspaceChanged)
      }
      return false
    }

    func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
      guard isCurrent(webView) else { return }
      guard webStore == nil || workspaceSession != nil else { return }
      if let url = webView.url, !acceptWorkspaceNavigation(url, in: webView) { return }
      if let url = webView.url,
        ["http", "https"].contains(url.scheme?.lowercased() ?? ""),
        url.omnigentOrigin != pinnedOrigin,
        pinnedAuthentication == .oidc,
        webStore == nil
      {
        webView.stopLoading()
        if oidcConnection != nil {
          recoverOidcSession()
        } else {
          startLogin(in: webView)
        }
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
      guard isCurrent(webView) else { return }
      // Failed or cancelled provisional loads leave the old document's lock intact.
      webView.scrollView.isScrollEnabled = true
      guard webStore == nil || workspaceSession != nil else { return }
      if let url = webView.url, !acceptWorkspaceNavigation(url, in: webView) { return }
      if let session = workspaceSession, let url = webView.url,
        session.navigationURL(for: url) != nil
      {
        effectiveOrigin = url.omnigentOrigin
        lastWorkspacePageURL = session.navigationURL(for: url)
      }
      if let url = webView.url { rememberOidcPage(url) }
      parent.model.hidesPage = false
      parent.model.currentURL = webView.url ?? parent.model.currentURL
      // Workspace roots are caught here too, not only in decidePolicyFor: that
      // callback is skipped for loads the shell starts itself, and the Databricks
      // login chain hands the session back with a form POST landing on the root.
      // didCommit sees every committed main-frame load.
      bounceIfWorkspaceRoot(webView)
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
      guard isCurrent(webView), webStore == nil || workspaceSession != nil else { return }
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
        (webView as? AccessoryFreeWebView)?.emitKeyboardViewport(force: true)
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
      guard isCurrent(webView) else { return }
      webView.reload()
    }

    func webView(
      _ webView: WKWebView,
      decidePolicyFor navigationAction: WKNavigationAction,
      decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
      guard isCurrent(webView) else {
        decisionHandler(.cancel)
        return
      }
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

      if webStore != nil, navigationAction.targetFrame?.isMainFrame == true,
        ["http", "https"].contains(scheme)
      {
        if let session = workspaceSession, session.isSignOutURL(url) {
          decisionHandler(.cancel)
          requestSignOut()
          return
        }
        guard let session = workspaceSession, let destination = session.navigationURL(for: url)
        else {
          decisionHandler(.cancel)
          if workspaceSession?.isAuthenticationURL(url) == true {
            requestWorkspaceRecovery()
          } else if navigationAction.navigationType == .linkActivated {
            openExternal(url)
          } else if workspaceSession != nil, url.omnigentOrigin != pinnedOrigin {
            requestWorkspaceRecovery()
          } else {
            showWorkspaceFailure(DatabricksSessionError.workspaceChanged)
          }
          return
        }
        if destination != url {
          decisionHandler(.cancel)
          bounce(webView, to: destination)
          return
        }
        lastWorkspacePageURL = destination
        // Native bootstrap already established this workspace's allowed page origins.
        decisionHandler(.allow)
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

      // The IdP must never load in the web view: the shell renews or signs out instead.
      if let connection = oidcConnection, navigationAction.targetFrame?.isMainFrame == true,
        ["http", "https"].contains(scheme),
        let route = OidcAuthRoute(url: url, serverURL: connection.serverURL)
      {
        decisionHandler(.cancel)
        switch route {
        case .login: recoverOidcSession()
        case .logout: requestOidcSignOut()
        }
        return
      }

      if navigationAction.targetFrame?.isMainFrame == true,
        ["http", "https"].contains(scheme),
        url.omnigentOrigin != pinnedOrigin
      {
        if pinnedAuthentication.usesInWebViewAuth {
          // Apps still use inline platform SSO; links tapped on the app page remain external.
          if webView.url?.omnigentOrigin == pinnedOrigin,
            navigationAction.navigationType == .linkActivated
          {
            openExternal(url)
            decisionHandler(.cancel)
          } else {
            decisionHandler(.allow)
          }
          return
        }

        if navigationAction.navigationType == .linkActivated {
          openExternal(url)
        } else if oidcConnection != nil {
          // A server redirect to the IdP: renew silently rather than sign in in the page.
          recoverOidcSession()
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
      _ webView: WKWebView, decidePolicyFor response: WKNavigationResponse,
      decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void
    ) {
      guard isCurrent(webView) else {
        decisionHandler(.cancel)
        return
      }
      if response.isForMainFrame, let http = response.response as? HTTPURLResponse,
        handleWorkspaceHTTPStatus(http.statusCode)
      {
        decisionHandler(.cancel)
        return
      }
      decisionHandler(.allow)
    }

    func handleWorkspaceHTTPStatus(_ status: Int) -> Bool {
      guard webStore != nil else { return false }
      if status == 401 {
        requestWorkspaceRecovery()
        return true
      }
      if status == 403 {
        showWorkspaceFailure(DatabricksSessionError.rejected(status))
        return true
      }
      return false
    }

    func webView(
      _ webView: WKWebView,
      createWebViewWith configuration: WKWebViewConfiguration,
      for navigationAction: WKNavigationAction,
      windowFeatures: WKWindowFeatures
    ) -> WKWebView? {
      guard isCurrent(webView) else { return nil }
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
      guard isCurrent(webView), Self.isAllowedMediaCaptureType(type),
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
      if let session = workspaceSession, let url = webView?.url,
        session.navigationURL(for: url) == nil
      {
        return false
      }
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

    /// The legacy ticket flow. Deprecated: removal targeted for iOS 0.5.0.
    private func startLogin(in webView: WKWebView) {
      guard let pinnedOrigin else { return }
      // The page is left as it is while Safari signs in, so nothing should cover it.
      parent.model.hidesPage = false
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

    private func isCurrent(_ webView: WKWebView) -> Bool {
      self.webView === webView && parent.model.webView === webView
    }

    private func handleLoadFailure(_ webView: WKWebView, error: Error) {
      guard isCurrent(webView) else { return }
      let nsError = error as NSError
      guard nsError.code != NSURLErrorCancelled else { return }
      if webStore != nil {
        showWorkspaceFailure(DatabricksSessionError.networkUnavailable)
        return
      }
      parent.model.isLoading = false
      parent.model.hidesPage = false
      parent.model.cancelServerSwitcherWatchdog()

      let failedURL = failedURL(from: nsError) ?? webView.url ?? pinnedURL ?? parent.initialURL
      guard failedURL.omnigentOrigin == pinnedOrigin else { return }
      parent.loadFailed(
        failedURL,
        OmnigentWebView.connectionErrorMessage(
          for: error, databricksInternalFeaturesEnabled: parent.databricksInternalFeaturesEnabled)
      )
    }

    private func publishModelChanges(_ update: @escaping @MainActor (WebViewModel) -> Void) {
      let model = parent.model
      let id = navigationID
      Task { @MainActor [weak self] in
        guard let self, navigationID == id, let webView, model.webView === webView else { return }
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

extension Result where Success == DatabricksWebContext?, Failure == Error {
  /// A server that isn't a Databricks host, so its manifest decides how it signs in.
  fileprivate func isGenericOidc(_ url: URL) -> Bool {
    if case .success(nil) = self {
      return ServerAuthentication(origin: url.omnigentOrigin) == .oidc
    }
    return false
  }
}

final class AccessoryFreeWebView: WKWebView {
  var onWindowAvailable: ((UIWindow) -> Void)?
  private let keyboardViewport = KeyboardViewportProbe()
  private var lastKeyboardViewportSize: CGSize?

  override init(frame: CGRect, configuration: WKWebViewConfiguration) {
    super.init(frame: frame, configuration: configuration)
    // Without following undocked keyboards, UIKit reports the floating iPad
    // toolbar as a full-width docked area.
    keyboardLayoutGuide.usesBottomSafeArea = false
    keyboardLayoutGuide.followsUndockedKeyboard = true
    keyboardViewport.isUserInteractionEnabled = false
    keyboardViewport.accessibilityElementsHidden = true
    keyboardViewport.translatesAutoresizingMaskIntoConstraints = false
    insertSubview(keyboardViewport, at: 0)
    let probeBottom = keyboardViewport.bottomAnchor.constraint(
      equalTo: keyboardLayoutGuide.topAnchor)
    probeBottom.priority = .defaultHigh
    NSLayoutConstraint.activate([
      keyboardViewport.topAnchor.constraint(equalTo: topAnchor),
      keyboardViewport.leadingAnchor.constraint(equalTo: keyboardLayoutGuide.leadingAnchor),
      keyboardViewport.trailingAnchor.constraint(equalTo: keyboardLayoutGuide.trailingAnchor),
      keyboardViewport.heightAnchor.constraint(greaterThanOrEqualToConstant: 0),
      probeBottom,
    ])
    keyboardViewport.onLayout = { [weak self] in self?.emitKeyboardViewport() }
    for name in [Notification.Name.GCKeyboardDidConnect, .GCKeyboardDidDisconnect] {
      NotificationCenter.default.addObserver(
        self, selector: #selector(hardwareKeyboardChanged), name: name, object: nil)
    }
  }

  required init?(coder: NSCoder) {
    fatalError("init(coder:) has not been implemented")
  }

  override func layoutSubviews() {
    super.layoutSubviews()
    emitKeyboardViewport()
  }

  @objc private func hardwareKeyboardChanged() {
    emitKeyboardViewport(force: true)
  }

  func emitKeyboardViewport(force: Bool = false) {
    let size = CGSize(
      width: bounds.width,
      height: keyboardViewportHeight(
        in: bounds, keyboardFrame: keyboardLayoutGuide.layoutFrame,
        hasIPadHardwareKeyboard: traitCollection.userInterfaceIdiom == .pad
          && GCKeyboard.coalesced != nil))
    guard size.width > 0, size.height > 0, force || size != lastKeyboardViewportSize else { return }
    lastKeyboardViewportSize = size
    evaluateJavaScript(
      "window.__omnigentNativeEmitKeyboardViewport?.(\(size.width), \(size.height));")
  }

  override func didMoveToWindow() {
    super.didMoveToWindow()
    if let window, let callback = onWindowAvailable {
      onWindowAvailable = nil
      callback(window)
    }
  }

  override var inputAccessoryView: UIView? {
    nil
  }
}

func keyboardViewportHeight(
  in bounds: CGRect, keyboardFrame: CGRect, hasIPadHardwareKeyboard: Bool = false
) -> CGFloat {
  // iPadOS initially reports the hardware toolbar as a short full-width frame
  // before publishing its floating bounds. Ignore that accessory-only area.
  if hasIPadHardwareKeyboard && keyboardFrame.height <= 80 {
    return bounds.height
  }
  // Only a keyboard spanning the bottom edge reduces the app's usable height.
  // Floating keyboards and hardware-keyboard controls overlay the app instead.
  let docked =
    keyboardFrame.minX <= bounds.minX + 1
    && keyboardFrame.maxX >= bounds.maxX - 1
    && keyboardFrame.maxY >= bounds.maxY - 1
  return docked ? max(0, min(bounds.height, keyboardFrame.minY - bounds.minY)) : bounds.height
}

private final class KeyboardViewportProbe: UIView {
  var onLayout: (() -> Void)?

  override func layoutSubviews() {
    super.layoutSubviews()
    onLayout?()
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
        Self.apply(source, to: window)
      }
    }
  }

  static func apply(_ source: ThemeSource, to window: UIWindow) {
    window.overrideUserInterfaceStyle = source.userInterfaceStyle
    guard window.windowLevel == .normal else { return }
    window.backgroundColor = DesignTokens.nativeBackground
    window.rootViewController?.view.backgroundColor = DesignTokens.nativeBackground
  }
}
