import SwiftUI

@main
struct OmnigentApp: App {
  @StateObject private var settings = SettingsStore()
  @StateObject private var router = AppRouter()

  init() {
    NativeNotificationManager.shared.start()
  }

  var body: some Scene {
    WindowGroup {
      AppRootView()
        .environmentObject(settings)
        .environmentObject(router)
        .onAppear {
          NativeNotificationManager.shared.setActivationHandler { path in
            router.routeNotification(path)
          }
        }
    }
  }
}

@MainActor
final class AppRouter: ObservableObject {
  @Published private(set) var pendingNotificationPath: String?
  /// An in-app path (`/c/<id>`) a deep link asked the SPA to open in-place.
  /// Consumed by WebShellView, which emits it to the page via
  /// `__omnigentNativeEmitOpenPath` (deferred until the page finishes loading —
  /// see WebShellView). Mirrors `pendingNotificationPath` on a separate channel
  /// so a deep link isn't mislabeled as a notification.
  @Published private(set) var pendingOpenPath: String?

  func routeNotification(_ path: String) {
    guard path.starts(with: "/") else { return }
    pendingNotificationPath = path
  }

  func consumeNotificationPath() -> String? {
    defer { pendingNotificationPath = nil }
    return pendingNotificationPath
  }

  func requestOpenPath(_ path: String) {
    guard path.starts(with: "/") else { return }
    pendingOpenPath = path
  }

  func consumeOpenPath() -> String? {
    defer { pendingOpenPath = nil }
    return pendingOpenPath
  }
}
