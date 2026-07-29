import SwiftUI

struct WebShellView: View {
  let initialURL: URL
  let connectToNewServer: () -> Void
  let switchToServer: (URL) -> Void
  let loadFailed: (URL, String) -> Void
  let loadSucceeded: (URL) -> Void

  @Environment(\.colorScheme) private var colorScheme
  @EnvironmentObject private var settings: SettingsStore
  @EnvironmentObject private var router: AppRouter
  @StateObject private var model = WebViewModel()
  /// A deep-link path that arrived while the page was still loading — emitted
  /// to the SPA once `isLoading` flips false, so a cold-start / mid-load deep
  /// link to the current server isn't lost (its `onOpenPath` subscriber isn't
  /// mounted until the SPA finishes booting).
  @State private var deferredOpenPath: String?

  var body: some View {
    GeometryReader { geometry in
      ZStack(alignment: .top) {
        OmnigentWebView(
          initialURL: initialURL,
          model: model,
          settings: settings,
          loadFailed: loadFailed,
          loadSucceeded: loadSucceeded
        )
        .ignoresSafeArea()

        ServerSwitcher(
          currentURL: model.currentURL ?? initialURL,
          recents: settings.recentServers,
          isLoading: model.isLoading,
          maxWidth: ServerSwitcherMetrics.maxWidth(for: geometry.size.width),
          switchServer: switchServer,
          connectToNewServer: connectToNewServer,
          reload: model.reload
        )
        .padding(.top, InsetMetrics.serverSwitcherTopPadding)
        .opacity(model.serverSwitcherHidden ? 0 : 1)
        .scaleEffect(model.serverSwitcherHidden ? 0.96 : 1, anchor: .top)
        .allowsHitTesting(!model.serverSwitcherHidden)
        .accessibilityHidden(model.serverSwitcherHidden)
      }
      .animation(.easeInOut(duration: 0.16), value: model.serverSwitcherHidden)
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
        model.emitInsets(
          topBar: InsetMetrics.topBarFootprint,
          bottomBar: InsetMetrics.bottomBarFootprint
        )
      }
    }
  }

  private func switchServer(_ urlString: String) {
    guard let url = URL(string: urlString) else { return }
    switchToServer(url)
  }
}

private struct ServerSwitcher: View {
  let currentURL: URL
  let recents: [String]
  let isLoading: Bool
  let maxWidth: CGFloat
  let switchServer: (String) -> Void
  let connectToNewServer: () -> Void
  let reload: () -> Void

  @Environment(\.colorScheme) private var colorScheme

  var body: some View {
    Menu {
      Button {
      } label: {
        Label(currentURL.omnigentHostLabel, systemImage: "checkmark")
      }
      .disabled(true)

      let otherServers = recents.filter {
        URL(string: $0)?.omnigentOrigin != currentURL.omnigentOrigin
      }
      if !otherServers.isEmpty {
        Divider()
        ForEach(otherServers, id: \.self) { recent in
          Button {
            switchServer(recent)
          } label: {
            Text(URL(string: recent)?.omnigentHostLabel ?? recent)
          }
        }
      }

      Divider()

      Button(action: reload) {
        Label("Reload", systemImage: "arrow.clockwise")
      }

      Divider()

      Button(action: connectToNewServer) {
        Label("Connect to New Server", systemImage: "plus")
      }
    } label: {
      HStack(spacing: 6) {
        Text(currentURL.omnigentHostLabel)
          .fontWeight(.medium)
          .lineLimit(1)
          .truncationMode(.middle)

        if isLoading {
          ProgressView()
            .controlSize(.mini)
            .padding(.leading, 2)
        } else {
          Image(systemName: "chevron.down")
            .font(.system(size: 11, weight: .semibold))
            .foregroundStyle(DesignTokens.mutedForeground(colorScheme))
        }
      }
      .font(.system(size: 12))
      .foregroundStyle(DesignTokens.foreground(colorScheme))
      .padding(.horizontal, 10)
      .frame(height: InsetMetrics.serverSwitcherHeight)
      .frame(maxWidth: maxWidth)
      .contentShape(RoundedRectangle(cornerRadius: 9, style: .continuous))
    }
    .buttonStyle(.plain)
    // The material/border/shadow live OUTSIDE the `label:` closure, on the
    // Menu's persistent host view. Applied inside the closure, UIKit's menu
    // presentation snapshots the styled label for its open/dismiss morph and
    // drops the shadow layer — leaving the pill flat (no shadow) for a beat
    // after dismissal. Keeping the chrome on the Menu sidesteps that snapshot.
    .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 9, style: .continuous))
    .overlay {
      RoundedRectangle(cornerRadius: 9, style: .continuous)
        .stroke(Color.primary.opacity(colorScheme == .dark ? 0.16 : 0.10), lineWidth: 0.5)
    }
    .shadow(color: .black.opacity(colorScheme == .dark ? 0.22 : 0.08), radius: 10, y: 4)
    .accessibilityLabel("Switch server")
  }
}

private enum ServerSwitcherMetrics {
  static func maxWidth(for containerWidth: CGFloat) -> CGFloat {
    min(172, max(120, containerWidth * 0.38))
  }
}

/// Single source of truth for the floating native bars' dimensions. These drive
/// both the SwiftUI layout (the `.frame`/`.padding` calls above and in
/// `ChatTerminalBar`) and the footprint pushed to the web layer via
/// `WebViewModel.emitInsets`, so the web's content insets can never drift from
/// the bars' real size. Values are CSS points, excluding the OS safe area (the
/// web layer adds that with `env(safe-area-inset-*)`).
enum InsetMetrics {
  // Server switcher — the top floating pill.
  static let serverSwitcherHeight: CGFloat = 28
  static let serverSwitcherTopPadding: CGFloat = 8
  static var topBarFootprint: CGFloat { serverSwitcherHeight + serverSwitcherTopPadding }

  // Chat/Terminal bar — the bottom floating capsule. The capsule wraps the
  // segment row (`barSegmentHeight`) in `barCapsulePadding` on every side.
  static let barSegmentHeight: CGFloat = 34
  static let barCapsulePadding: CGFloat = 4
  static let barBottomPadding: CGFloat = 6
  static var bottomBarFootprint: CGFloat {
    barSegmentHeight + barCapsulePadding * 2 + barBottomPadding
  }
}
