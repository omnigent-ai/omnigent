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
              Image("OmnigentLogoReverse").resizable().scaledToFit().frame(width: 130)
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
  private(set) var credentials = WatchKeychain.load()

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
    guard let data = context[WatchCredentials.contextKey] as? Data else { return }
    let credentials = try? JSONDecoder().decode(WatchCredentials.self, from: data)
    Task { @MainActor in
      self.credentials = credentials
      WatchKeychain.save(credentials)
    }
  }
}

enum WatchKeychain {
  private static var query: [String: Any] {
    [
      kSecClass as String: kSecClassGenericPassword,
      kSecAttrService as String: "ai.omnigent.ios.watch",
      kSecAttrAccount as String: WatchCredentials.contextKey,
    ]
  }

  static func load() -> WatchCredentials? {
    var result: CFTypeRef?
    let search = query.merging([kSecReturnData as String: true]) { $1 }
    guard SecItemCopyMatching(search as CFDictionary, &result) == errSecSuccess,
      let data = result as? Data
    else { return nil }
    return try? JSONDecoder().decode(WatchCredentials.self, from: data)
  }

  static func save(_ credentials: WatchCredentials?) {
    SecItemDelete(query as CFDictionary)
    guard let credentials, let data = try? JSONEncoder().encode(credentials) else { return }
    let item = query.merging([
      kSecValueData as String: data,
      kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
    ]) { $1 }
    SecItemAdd(item as CFDictionary, nil)
  }
}
