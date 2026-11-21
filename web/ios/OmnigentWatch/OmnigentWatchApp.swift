import OSLog
import Security
import SwiftUI
import WatchConnectivity

@main
struct OmnigentWatchApp: App {
  @State private var link = WatchLink()

  var body: some Scene {
    WindowGroup {
      Group {
        if let credentials = link.credentials {
          // A new server or cookie rebuilds every screen, so nothing keeps the old client.
          SessionListView(client: WatchClient(credentials: credentials)).id(credentials)
        } else {
          ContentUnavailableView {
            Label {
              Text("Open Omnigent on iPhone")
            } icon: {
              Image("OmnigentStarfish").resizable().scaledToFit().frame(width: 72)
            }
          } description: {
            Text("Sign in on your iPhone to see sessions here.")
          }
          .containerBackground(for: .navigation) { BrandBackground() }
        }
      }
      .tint(.brand)
      .fontDesign(.rounded)
    }
  }
}

extension Color {
  /// The web app's default `--brand-accent` and dark `--background`.
  static let brand = Color(red: 0xDF / 255, green: 0x3C / 255, blue: 0x85 / 255)
  static let canvas = Color(red: 0x0E / 255, green: 0x0B / 255, blue: 0x19 / 255)
}

/// The web app's flat dark `--background`.
struct BrandBackground: View {
  var body: some View { Color.canvas.ignoresSafeArea() }
}

/// Receives credentials from the iPhone and keeps them in this watch's Keychain.
@MainActor @Observable
final class WatchLink: NSObject, WCSessionDelegate {
  private static let logger = Logger(subsystem: "ai.omnigent.ios.watch", category: "credentials")
  private var state = WatchCredentialState(credentials: WatchKeychain.shared.load())
  var credentials: WatchCredentials? { state.credentials }

  override init() {
    super.init()
    guard WCSession.isSupported() else { return }
    WCSession.default.delegate = self
    WCSession.default.activate()
  }

  nonisolated func session(
    _ session: WCSession, activationDidCompleteWith state: WCSessionActivationState, error: Error?
  ) {
    receive(session.receivedApplicationContext)
  }

  nonisolated func session(
    _ session: WCSession, didReceiveApplicationContext context: [String: Any]
  ) {
    receive(context)
  }

  private nonisolated func receive(_ context: [String: Any]) {
    guard let data = context[WatchCredentials.contextKey] as? Data,
      let sentAt = context["sentAt"] as? Date
    else { return }
    Task { @MainActor in
      guard state.apply(data, sentAt: sentAt) else { return }
      let status = WatchKeychain.shared.save(state.credentials)
      if status != errSecSuccess {
        Self.logger.error(
          "Watch credential persistence failed (status \(status, privacy: .public))")
      }
    }
  }
}
