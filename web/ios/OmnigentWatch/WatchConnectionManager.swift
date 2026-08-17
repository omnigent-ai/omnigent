import Foundation
import WatchConnectivity

@MainActor
final class WatchConnectionManager: NSObject, ObservableObject, WCSessionDelegate {
  @Published private(set) var credentials: WatchCredentials?

  override init() {
    credentials = WatchCredentialStore.load()
    super.init()
    guard WCSession.isSupported() else { return }
    WCSession.default.delegate = self
    WCSession.default.activate()
    apply(WCSession.default.receivedApplicationContext)
  }

  nonisolated func session(
    _ session: WCSession,
    activationDidCompleteWith activationState: WCSessionActivationState,
    error: Error?
  ) {
    guard activationState == .activated else { return }
    let context = session.receivedApplicationContext
    Task { @MainActor [weak self] in self?.apply(context) }
  }

  nonisolated func session(
    _ session: WCSession,
    didReceiveApplicationContext applicationContext: [String: Any]
  ) {
    Task { @MainActor [weak self] in self?.apply(applicationContext) }
  }

  private func apply(_ context: [String: Any]) {
    guard let data = context["credentials"] as? Data,
      let received = try? JSONDecoder().decode(WatchCredentials.self, from: data),
      received.syncedAt > (credentials?.syncedAt ?? .distantPast)
    else { return }

    // Keep this copy when linker-signed simulators reject Keychain writes.
    credentials = received
    do {
      try WatchCredentialStore.save(received)
    } catch {
      NSLog("[omnigent] failed to store watch credentials: \(String(describing: error))")
    }
  }
}
