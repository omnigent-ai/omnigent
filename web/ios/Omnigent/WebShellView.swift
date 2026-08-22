import SwiftUI

struct WebShellView: View {
  let serverURL: URL
  let initialURL: URL
  let connectToNewServer: () -> Void
  let switchToServer: (URL) -> Void
  let loadFailed: (URL, String) -> Void
  let loadSucceeded: () -> Void

  @Environment(\.colorScheme) private var colorScheme
  @EnvironmentObject private var settings: SettingsStore
  @EnvironmentObject private var router: AppRouter
  @EnvironmentObject private var managedConfiguration: ManagedConfigurationProvider
  @StateObject private var model = WebViewModel()
  /// A deep-link path that arrived while the page was still loading — emitted
  /// to the SPA once `isLoading` flips false, so a cold-start / mid-load deep
  /// link to the current server isn't lost (its `onOpenPath` subscriber isn't
  /// mounted until the SPA finishes booting).
  @State private var deferredOpenPath: String?

  var body: some View {
    ZStack {
      OmnigentWebView(
        serverURL: serverURL,
        initialURL: initialURL,
        managedServers: managedConfiguration.serverURLs.map(\.absoluteString),
        recentServers: ManagedServers.recents(
          settings.recentServers, excludingManaged: managedConfiguration.serverURLs),
        model: model,
        settings: settings,
        switchToServer: switchToServer,
        connectToNewServer: connectToNewServer,
        loadFailed: loadFailed,
        loadSucceeded: loadSucceeded
      )
      .ignoresSafeArea()
      .ignoresSafeArea(.keyboard)
      .background(DesignTokens.background(colorScheme).ignoresSafeArea())
      .overlay(alignment: .bottom) {
        // Always present, shown/hidden by opacity rather than insert/remove, so
        // a transient visibility flip never slides the bar in and out. The web
        // layer reserves a fixed footprint for it (`.omnigent-native-bottom-
        // spacer` in index.css), so there's no size round-trip to coordinate.
        ChatTerminalBar(
          mode: $model.viewMode,
          terminalEnabled: model.terminalEnabled,
          terminalStartingUp: model.terminalStartingUp,
          onSelect: { newMode in
            model.viewMode = newMode
            model.emitViewModeChanged(newMode)
          }
        )
        .padding(.bottom, InsetMetrics.barBottomPadding)
        .opacity(model.bottomBarVisible ? 1 : 0)
        .allowsHitTesting(model.bottomBarVisible)
        .accessibilityHidden(!model.bottomBarVisible)
        .animation(.easeInOut(duration: 0.2), value: model.bottomBarVisible)
      }
      .ignoresSafeArea(.keyboard)
    }
    .onChange(of: router.pendingNotificationPath) { _, _ in
      if let path = router.consumeNotificationPath() {
        model.emitNotificationActivation(path)
      }
    }
    .onChange(of: router.pendingOpenPath) { _, _ in
      guard let path = router.consumeOpenPath() else { return }
      // If the SPA is still booting (a cold-start deep link to the current
      // server, or one that arrived mid-navigation), defer the path until the
      // page finishes loading — emitting now would fire into a page whose
      // `onOpenPath` subscriber isn't mounted yet and be lost.
      if model.isLoading {
        deferredOpenPath = path
      } else {
        model.emitOpenPath(path)
      }
    }
    .onChange(of: model.isLoading) { _, loading in
      if !loading, let path = deferredOpenPath {
        deferredOpenPath = nil
        model.emitOpenPath(path)
      }
    }
    .onChange(of: model.isLoading) { _, loading in
      // Re-push the native bar footprints once each load completes; the JS
      // bridge caches the value so a later-mounting subscriber still gets it.
      if !loading {
        model.emitInsets(bottomBar: InsetMetrics.bottomBarFootprint)
      }
    }
  }
}

/// Single source of truth for the floating native bar's dimensions. These drive
/// both the SwiftUI layout (the `.frame`/`.padding` calls above and in
/// `ChatTerminalBar`) and the footprint pushed to the web layer via
/// `WebViewModel.emitInsets`, so the web's content insets can never drift from
/// the bars' real size. Values are CSS points, excluding the OS safe area (the
/// web layer adds that with `env(safe-area-inset-*)`).
enum InsetMetrics {
  // Chat/Terminal bar — the bottom floating capsule. The capsule wraps the
  // segment row (`barSegmentHeight`) in `barCapsulePadding` on every side.
  static let barSegmentHeight: CGFloat = 34
  static let barCapsulePadding: CGFloat = 4
  static let barBottomPadding: CGFloat = 6
  static var bottomBarFootprint: CGFloat {
    barSegmentHeight + barCapsulePadding * 2 + barBottomPadding
  }
}
