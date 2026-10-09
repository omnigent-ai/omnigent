import SwiftUI

struct WebShellView: View {
  let initialURL: URL
  let connectToNewServer: () -> Void
  let switchToServer: (URL) -> Void
  let loadFailed: (URL, String?) -> Void
  let loadSucceeded: () -> Void
  var signedOut: ((DatabricksWebContext, Task<Void, Error>) -> Void)?
  /// The clean server URL behind `initialURL`, which may carry a conversation path.
  var serverURL: URL?
  /// Signed out of a native-OIDC server: the server and the Connect screen message.
  var serverSignedOut: ((URL, String) -> Void)?

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
  @State private var deferredNotificationPath: String?
  @State private var connectionID = UUID()
  /// Set while recovering; it applies only to the destination it was started for.
  @State private var recoveryIntent: RecoveryIntent?
  @State private var recoveryPage: RecoveryPage?
  @State private var recoveryPolicy = DatabricksRecoveryPolicy()
  @State private var needsReauthentication = false
  /// The "Sign in again?" message; nil uses the workspace wording.
  @State private var reauthenticationMessage: String?
  @State private var workspacePageLoaded = false

  private var isWorkspace: Bool {
    ServerAuthentication(origin: initialURL.omnigentOrigin) == .databricksWorkspace
  }
  func showsServerSwitcher(for model: WebViewModel) -> Bool { !model.serverSwitcherHidden }

  var body: some View {
    let showsServerSwitcher = self.showsServerSwitcher(for: model)
    GeometryReader { geometry in
      ZStack(alignment: .top) {
        OmnigentWebView(
          initialURL: initialURL,
          model: model,
          settings: settings,
          databricksInternalFeaturesEnabled: managedConfiguration.databricksInternalFeaturesEnabled,
          loadFailed: loadFailed,
          loadSucceeded: {
            workspacePageLoaded = true
            loadSucceeded()
          },
          pushServerPicker: pushServerPicker,
          requestSwitchServer: switchServerIfListed,
          openServerSetup: connectToNewServer,
          connectionIntent: recoveryIntent?.intent(for: initialURL) ?? .connect,
          recoveryPageURL: recoveryPage?.url(for: initialURL),
          recoverWorkspace: recoverWorkspace,
          workspaceReady: { recoveryPolicy.markReady() },
          reauthenticateWorkspace: { page in
            recoveryPage = RecoveryPage(url: page, initialURL: initialURL)
            reauthenticationMessage = nil
            needsReauthentication = true
          },
          signedOut: signedOut,
          serverURL: serverURL,
          requireSignIn: { page, message in
            recoveryPage = RecoveryPage(url: page, initialURL: initialURL)
            reauthenticationMessage = message
            needsReauthentication = true
          },
          serverSignedOut: serverSignedOut
        )
        .id(DatabricksWebContext.viewIdentity(for: initialURL) + connectionID.uuidString)
        .ignoresSafeArea()

        if model.isAuthenticating {
          VStack(spacing: 16) {
            ProgressView(isWorkspace ? "Connecting to workspace…" : "Signing in…")
            Button("Cancel") { model.cancelAuthentication?() }
          }
          .frame(maxWidth: .infinity, maxHeight: .infinity)
          .background(DesignTokens.background(colorScheme))
        } else if model.hidesPage {
          ProgressView()
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(DesignTokens.background(colorScheme))
        }

        ServerSwitcher(
          currentURL: initialURL,
          recents: ManagedServers.merged(
            managed: managedConfiguration.serverURLs, recents: settings.recentServers),
          isLoading: model.isLoading,
          maxWidth: ServerSwitcherMetrics.maxWidth(for: geometry.size.width),
          switchServer: switchServer,
          connectToNewServer: connectToNewServer,
          reload: reload,
          signOut: model.signOut
        )
        .padding(.top, InsetMetrics.serverSwitcherTopPadding)
        .opacity(showsServerSwitcher ? 1 : 0)
        .scaleEffect(showsServerSwitcher ? 1 : 0.96, anchor: .top)
        .allowsHitTesting(showsServerSwitcher)
        .accessibilityHidden(!showsServerSwitcher)
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
      #if DEBUG
        .overlay(alignment: .bottomLeading) {
          if isWorkspace, !model.isAuthenticating {
            SessionDebugMenu(inject: { (fault: DatabricksDebugFault) in
              await model.injectDebugFault?(fault)
            })
            .padding(.leading, 12)
            .padding(.bottom, InsetMetrics.bottomBarFootprint + 10)
          } else if !isWorkspace, !model.isAuthenticating, model.injectOidcDebugFault != nil {
            SessionDebugMenu(inject: { (fault: OidcDebugFault) in
              await model.injectOidcDebugFault?(fault)
            })
            .padding(.leading, 12)
            .padding(.bottom, InsetMetrics.bottomBarFootprint + 10)
          }
        }
      #endif
    }
    .alert("Sign in again?", isPresented: $needsReauthentication) {
      Button("Sign In") {
        recoveryIntent = nil
        workspacePageLoaded = false
        connectionID = UUID()
      }
      Button("Cancel", role: .cancel) { loadFailed(initialURL, nil) }
    } message: {
      Text(
        reauthenticationMessage
          ?? DatabricksSessionError.reauthenticationRequired.localizedDescription)
    }
    .onChange(of: router.pendingNotificationPath) { _, _ in
      if let path = router.consumeNotificationPath() {
        deferredNotificationPath = path
        flushDeferredNavigation()
      }
    }
    .onChange(of: router.pendingOpenPath) { _, _ in
      guard let path = router.consumeOpenPath() else { return }
      // If the SPA is still booting (a cold-start deep link to the current
      // server, or one that arrived mid-navigation), defer the path until the
      // page finishes loading — emitting now would fire into a page whose
      // `onOpenPath` subscriber isn't mounted yet and be lost.
      deferredOpenPath = path
      flushDeferredNavigation()
    }
    .onChange(of: model.isLoading) { _, _ in flushDeferredNavigation() }
    // A page recorded for another destination must not resurface on a later load.
    .onChange(of: initialURL) { _, _ in
      recoveryPage = nil
      recoveryIntent = nil
    }
    .onChange(of: workspacePageLoaded) { _, _ in flushDeferredNavigation() }
    .onChange(of: model.isLoading) { _, loading in
      // Re-push the native bar footprints and the server-picker payload once
      // each load completes; the JS bridge caches both so later-mounting
      // subscribers still get them.
      if !loading {
        model.emitInsets(
          topBar: InsetMetrics.topBarFootprint,
          bottomBar: InsetMetrics.bottomBarFootprint
        )
        pushServerPicker()
      }
    }
  }

  private func flushDeferredNavigation() {
    guard !model.isLoading, !model.isAuthenticating, !needsReauthentication,
      !isWorkspace || workspacePageLoaded
    else { return }
    if let path = deferredOpenPath {
      deferredOpenPath = nil
      model.emitOpenPath(path)
    }
    if let path = deferredNotificationPath {
      deferredNotificationPath = nil
      model.emitNotificationActivation(path)
    }
  }

  private func recoverWorkspace(_ pageURL: URL) {
    guard recoveryPolicy.begin() else {
      loadFailed(initialURL, DatabricksSessionError.recoveryExhausted.localizedDescription)
      return
    }
    recoveryPage = RecoveryPage(url: pageURL, initialURL: initialURL)
    recoveryIntent = RecoveryIntent(initialURL: initialURL)
    workspacePageLoaded = false
    connectionID = UUID()
  }

  private func reload() {
    guard isWorkspace else {
      model.reload()
      return
    }
    recoveryPage = RecoveryPage(url: model.currentURL ?? initialURL, initialURL: initialURL)
    recoveryPolicy = DatabricksRecoveryPolicy()
    recoveryIntent = nil
    workspacePageLoaded = false
    connectionID = UUID()
  }

  /// Every server the picker may offer or switch to — administrator-preset
  /// ones first, then recents. Doubles as the switch allow list.
  private var pickerServers: [String] {
    ManagedServers.merged(
      managed: managedConfiguration.serverURLs, recents: settings.recentServers)
  }

  private func pushServerPicker() {
    model.emitServerPicker(
      currentOrigin: (model.currentURL ?? initialURL).omnigentOrigin,
      managedServers: managedConfiguration.serverURLs.map(\.absoluteString),
      recentServers: ManagedServers.recents(
        settings.recentServers, excludingManaged: managedConfiguration.serverURLs),
      canSignOut: model.signOut != nil
    )
  }

  /// Switch only to a server the picker itself offered — the same allow-list
  /// gate the desktop shell applies, so page script can't steer the shell to
  /// an arbitrary origin through the bridge.
  private func switchServerIfListed(_ urlString: String) {
    guard pickerServers.contains(urlString) else { return }
    switchServer(urlString)
  }

  private func switchServer(_ urlString: String) {
    guard let url = URL(string: urlString) else { return }
    switchToServer(url)
  }
}

/// A page to return to after recovery or "Sign in again?", valid only for the destination it was
/// recorded under: non-workspace servers share one view, so it must not leak to a later load.
struct RecoveryPage: Equatable {
  let url: URL
  let initialURL: URL

  func url(for currentInitialURL: URL) -> URL? {
    currentInitialURL == initialURL ? url : nil
  }
}

/// A recovery load started for one destination. Any other destination connects normally, so a
/// later explicit Connect is never treated as a silent recovery.
struct RecoveryIntent: Equatable {
  let initialURL: URL

  func intent(for currentInitialURL: URL) -> DatabricksConnectionIntent {
    currentInitialURL == initialURL ? .recover : .connect
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
  let signOut: (() -> Void)?

  @Environment(\.colorScheme) private var colorScheme

  var body: some View {
    Menu {
      Button {
      } label: {
        Label(DatabricksWebContext.serverLabel(for: currentURL), systemImage: "checkmark")
      }
      .disabled(true)

      let otherServers = recents.filter {
        guard let url = URL(string: $0) else { return false }
        if ServerAuthentication(origin: currentURL.omnigentOrigin) == .databricksWorkspace {
          return DatabricksWebContext.contextIdentity(for: url)
            != DatabricksWebContext.contextIdentity(for: currentURL)
        }
        return url.omnigentOrigin != currentURL.omnigentOrigin
      }
      if !otherServers.isEmpty {
        Divider()
        ForEach(otherServers, id: \.self) { recent in
          Button {
            switchServer(recent)
          } label: {
            Text(URL(string: recent).map { DatabricksWebContext.serverLabel(for: $0) } ?? recent)
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
      if let signOut {
        Divider()
        Button(role: .destructive, action: signOut) {
          Label(
            ServerAuthentication(origin: currentURL.omnigentOrigin) == .databricksWorkspace
              ? "Sign Out of Workspace" : "Sign Out",
            systemImage: "rectangle.portrait.and.arrow.right")
        }
      }
    } label: {
      HStack(spacing: 6) {
        Text(DatabricksWebContext.serverLabel(for: currentURL))
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

#if DEBUG
  /// A debug-menu fault: what the menu item says and what to expect after injecting it.
  protocol SessionDebugFault: CaseIterable, Identifiable, Sendable {
    var title: String { get }
    var systemImage: String { get }
  }

  extension DatabricksDebugFault: SessionDebugFault {}
  extension OidcDebugFault: SessionDebugFault {}

  /// Breaks one piece of the live session on demand so recovery, refresh, and the sign-in
  /// prompt can be exercised by hand. Compiled out of release builds.
  private struct SessionDebugMenu<Fault: SessionDebugFault>: View {
    let inject: (Fault) async -> String?

    @Environment(\.colorScheme) private var colorScheme
    @State private var status: String?
    @State private var busy = false

    var body: some View {
      VStack(alignment: .leading, spacing: 6) {
        if let status {
          Text(status)
            .font(.system(size: 11))
            .foregroundStyle(DesignTokens.foreground(colorScheme))
            .padding(.horizontal, 9)
            .padding(.vertical, 7)
            .frame(maxWidth: 240, alignment: .leading)
            .background(
              .ultraThinMaterial, in: RoundedRectangle(cornerRadius: 9, style: .continuous)
            )
            .onTapGesture { self.status = nil }
            .accessibilityHint("Tap to dismiss")
        }

        Menu {
          ForEach(Array(Fault.allCases)) { fault in
            Button(role: .destructive) {
              run(fault)
            } label: {
              Label(fault.title, systemImage: fault.systemImage)
            }
          }
        } label: {
          HStack(spacing: 5) {
            Image(systemName: "ladybug")
              .font(.system(size: 11, weight: .semibold))
            Text("Debug")
              .font(.system(size: 12))
            if busy {
              ProgressView().controlSize(.mini)
            }
          }
          .foregroundStyle(DesignTokens.foreground(colorScheme))
          .padding(.horizontal, 10)
          .frame(height: InsetMetrics.serverSwitcherHeight)
          .contentShape(RoundedRectangle(cornerRadius: 9, style: .continuous))
        }
        .buttonStyle(.plain)
        .disabled(busy)
        // Chrome stays outside the label closure; see ServerSwitcher for why.
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 9, style: .continuous))
        .overlay {
          RoundedRectangle(cornerRadius: 9, style: .continuous)
            .stroke(Color.primary.opacity(colorScheme == .dark ? 0.16 : 0.10), lineWidth: 0.5)
        }
        .shadow(color: .black.opacity(colorScheme == .dark ? 0.22 : 0.08), radius: 10, y: 4)
        .accessibilityLabel("Break session for testing")
      }
    }

    private func run(_ fault: Fault) {
      guard !busy else { return }
      busy = true
      status = nil
      Task {
        let message = await inject(fault)
        busy = false
        status = message ?? "The server view is not ready yet."
      }
    }
  }
#endif

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
