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

  func routeNotification(_ path: String) {
    guard path.starts(with: "/") else { return }
    pendingNotificationPath = path
  }

  func consumeNotificationPath() -> String? {
    defer { pendingNotificationPath = nil }
    return pendingNotificationPath
  }
}
