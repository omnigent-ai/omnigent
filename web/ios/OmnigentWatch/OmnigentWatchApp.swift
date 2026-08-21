import SwiftUI

@main
struct OmnigentWatchApp: App {
  @StateObject private var connection = WatchConnectionManager()

  var body: some Scene {
    WindowGroup {
      WatchContentView()
        .environmentObject(connection)
        .tint(OmnigentWatchTheme.brandAccent)
        .preferredColorScheme(.dark)
    }
  }
}
