import SwiftUI

struct AppRootView: View {
  @EnvironmentObject private var settings: SettingsStore
  @EnvironmentObject private var router: AppRouter
  @State private var mode: Mode

  /// A deep link to a server the user has never connected to, awaiting the
  /// consent prompt's answer. Connecting pins a new origin (notifications,
  /// badge, mic), so a clicked link must not silently pin an attacker-chosen
  /// server — the alert is the one surface a page-click can't forge.
  @State private var consentDeepLink: DeepLink?
  @State private var showDeepLinkConsent = false

  init() {
    _mode = State(initialValue: .setup(prefill: nil, error: nil))
  }

  var body: some View {
    Group {
      switch mode {
      case .setup(let prefill, let error):
        ConnectView(prefill: prefill ?? settings.serverURL, error: error) { url in
          settings.serverURL = url.absoluteString
          mode = .web(serverURL: url, path: nil)
        }
      case .web(let serverURL, let path):
        WebShellView(
          initialURL: path.map { conversationURL(for: serverURL, path: $0) } ?? serverURL,
          connectToNewServer: {
            mode = .setup(prefill: settings.serverURL, error: nil)
          },
          switchToServer: { nextURL in
            settings.serverURL = nextURL.absoluteString
            mode = .web(serverURL: nextURL, path: nil)
          },
          loadFailed: { failedURL, message in
            mode = .setup(
              prefill: failedURL.omnigentOrigin ?? failedURL.absoluteString, error: message)
          },
          // Record the CLEAN server URL (no /c/<id>) — the conversation path
          // lives only in the load URL, never in recents, so a later deep link
          // resolves against an un-polluted server identity.
          loadSucceeded: { _ in
            settings.rememberRecentServer(serverURL)
          }
        )
      }
    }
    .task {
      guard shouldAutoOpenSavedServer else { return }
      // A deep link that arrived before this task ran already moved us off the
      // setup page (mode is .web), so this guard skips the auto-open and the
      // deep link drives — no redundant default-server load.
      if case .setup(nil, nil) = mode,
        let saved = settings.serverURL,
        let url = URL(string: saved)
      {
        mode = .web(serverURL: url, path: nil)
      }
    }
    .onOpenURL { url in handleDeepLink(url) }
    .alert(
      "Open this Omnigent link?",
      isPresented: $showDeepLinkConsent
    ) {
      Button("Cancel", role: .cancel) { consentDeepLink = nil }
      Button("Open") { confirmUnknownDeepLink() }
    } message: {
      if let consentDeepLink {
        Text(
          """
          This link will connect Omnigent to \(consentDeepLink.origin.hostLabel) and open a conversation.

          Only open links from a server you trust.
          """
        )
      }
    }
  }

  private enum Mode: Equatable {
    case setup(prefill: String?, error: String?)
    // serverURL: the window's server identity (origin or origin + workspace
    // mount), with NO conversation path — used for recents and origin
    // matching. path: an optional deep-link conversation path loaded on top of
    // it (nil for a normal connect / server switch).
    case web(serverURL: URL, path: String?)
  }

  private var shouldAutoOpenSavedServer: Bool {
    #if DEBUG
      let processInfo = ProcessInfo.processInfo
      return processInfo.environment["OMNIGENT_SCREENSHOT_APP_URL"] == nil
        && !processInfo.arguments.contains("-FASTLANE_SNAPSHOT")
    #else
      true
    #endif
  }
}

// MARK: Deep links

extension AppRootView {
  /// Handle an `omnigent://` URL the system routed to the app. The window
  /// handling mirrors the desktop shell (designs/desktop-deep-link.md):
  ///   - SAME server (this window is already on the link's origin) → navigate
  ///     in-place via the SPA router (no reload, no dropped stream), deferred
  ///     by WebShellView until the page finishes loading.
  ///   - KNOWN server (previously connected, no live window on it) → switch to
  ///     it, loading the conversation directly; no consent (the user already
  ///     chose this server).
  ///   - UNKNOWN server → consent, since pinning a new origin is a privilege
  ///     grant; the workspace-mount probe runs only AFTER consent, so a link to
  ///     an attacker-chosen host makes no pre-consent network request.
  private func handleDeepLink(_ url: URL) {
    guard let deepLink = DeepLink.parse(url) else { return }
    let targetOrigin = deepLink.origin

    if currentWebOrigin == targetOrigin {
      // Same server — route in-place. WebShellView defers the path until the
      // SPA finishes loading, so this also covers a cold-start link to the
      // saved server (the SPA is still booting).
      router.requestOpenPath(deepLink.path)
      return
    }

    if let known = settings.knownServerURL(forOrigin: targetOrigin) {
      openServer(serverURL: known, path: deepLink.path)
      return
    }

    consentDeepLink = deepLink
    showDeepLinkConsent = true
  }

  /// Switch this window to a (clean) server URL and load an optional deep-link
  /// conversation path on top of it. `settings.serverURL` and recents stay free
  /// of the `/c/<id>` path — only the load URL carries it.
  private func openServer(serverURL: URL, path: String?) {
    settings.serverURL = serverURL.absoluteString
    mode = .web(serverURL: serverURL, path: path)
  }

  /// Consent was given for an unknown server — discover the workspace mount
  /// (the only network request, AFTER consent), then switch. The origin is
  /// unchanged by the probe (it only appends `/ml/omnigents` under it), so the
  /// consent decision stands. Recording the server happens on load success
  /// (loadSucceeded), so a server that fails to load isn't remembered.
  private func confirmUnknownDeepLink() {
    guard let deepLink = consentDeepLink else { return }
    consentDeepLink = nil
    Task {
      guard let originURL = URL(string: deepLink.origin) else { return }
      let expanded = await WorkspaceURLExpander.expandIfNeeded(originURL)
      await MainActor.run {
        openServer(serverURL: expanded, path: deepLink.path)
      }
    }
  }

  /// The origin this window is currently connected to, or nil while it shows
  /// the bundled connect page. Derived from `mode`'s clean serverURL (stable
  /// across in-app SPA navigation), not the live web-view URL.
  private var currentWebOrigin: String? {
    if case .web(let serverURL, _) = mode { return serverURL.omnigentOrigin }
    return nil
  }

  /// Join a basename-less SPA path (`/c/<id>`) onto a server URL that may carry
  /// a workspace mount (`/ml/omnigents`). The path lives UNDER the mount, so it
  /// is string-concatenated (not URL-resolved, which would anchor against the
  /// origin and drop the mount) — mirroring the desktop's `resolveServerPath`.
  private func conversationURL(for serverURL: URL, path: String) -> URL {
    let base = serverURL.absoluteString.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
    return URL(string: base + path) ?? serverURL
  }
}

extension String {
  /// A short host[:port] label for the consent prompt, parsed from an origin
  /// string like `"https://host"` or `"http://localhost:8000"`.
  fileprivate var hostLabel: String {
    URL(string: self)?.omnigentHostLabel ?? self
  }
}
