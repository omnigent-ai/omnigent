import Foundation

@MainActor
final class SettingsStore: ObservableObject {
  @Published var serverURL: String? {
    didSet {
      defaults.set(serverURL, forKey: Keys.serverURL)
      // Choosing another server ends a refused server's suppression.
      if let suppressed = autoOpenSuppressedServer, suppressed != serverURL {
        autoOpenSuppressedServer = nil
      }
    }
  }

  /// The saved server whose last sign-in was refused: still the Connect prefill, but not
  /// reopened on launch until it connects successfully or another server is chosen.
  private var autoOpenSuppressedServer: String? {
    get { defaults.string(forKey: Keys.autoOpenSuppressedServer) }
    set { defaults.set(newValue, forKey: Keys.autoOpenSuppressedServer) }
  }

  @Published private(set) var recentServers: [String] {
    didSet { defaults.set(recentServers, forKey: Keys.recentServers) }
  }

  private let defaults: UserDefaults
  private let maxRecentServers = 5

  init(defaults: UserDefaults = .standard) {
    self.defaults = defaults
    #if DEBUG
      // UI tests pass `--omnigent-reset-state` to start each case with NO
      // saved/recent server, so a deep link to `localhost:8000` always hits the
      // unknown-server consent path (not the in-place route for a known server).
      if ProcessInfo.processInfo.arguments.contains("--omnigent-reset-state") {
        for key in [
          Keys.serverURL, Keys.recentServers, Keys.allowedProtocols, Keys.autoOpenSuppressedServer,
        ] {
          defaults.removeObject(forKey: key)
        }
      }
      serverURL =
        ProcessInfo.processInfo.omnigentArgumentValue(after: "--omnigent-server-url")
        ?? ProcessInfo.processInfo.environment["OMNIGENT_SCREENSHOT_APP_URL"]
        ?? defaults.string(forKey: Keys.serverURL)
    #else
      serverURL = defaults.string(forKey: Keys.serverURL)
    #endif
    recentServers = defaults.stringArray(forKey: Keys.recentServers) ?? []
  }

  func stopAutoOpening(_ context: DatabricksWebContext) {
    guard let saved = serverURL, let url = URL(string: saved),
      let scope = try? DatabricksCredentialScope(
        workspaceURL: url, configuration: context.configuration),
      scope == context.scope
    else { return }
    serverURL = nil
  }

  /// After signing out of an OIDC server, don't reconnect to it on relaunch. Its grant is per
  /// origin, so any saved URL on that origin stops; recents stay for an explicit reconnect.
  func stopAutoOpening(oidcServer url: URL) {
    guard let saved = serverURL,
      URL(string: saved)?.omnigentOriginIdentity == url.omnigentOriginIdentity
    else { return }
    serverURL = nil
  }

  /// After an OIDC server refuses a sign-in, keep it as the Connect prefill but don't reopen it
  /// on launch, which would only show the refusal again.
  func suppressAutoOpening(oidcServer url: URL) {
    guard let saved = serverURL,
      URL(string: saved)?.omnigentOriginIdentity == url.omnigentOriginIdentity
    else { return }
    autoOpenSuppressedServer = saved
  }

  /// The saved server to open on launch; nil when there is none or its sign-in was refused.
  var autoOpenServerURL: String? {
    guard let saved = serverURL, saved != autoOpenSuppressedServer else { return nil }
    return saved
  }

  /// A server loaded: a refusal recorded for it no longer applies.
  func connectionSucceeded() {
    autoOpenSuppressedServer = nil
  }

  func rememberRecentServer(_ url: URL) {
    let value = url.absoluteString
    let deduped: [String] = [value] + recentServers.filter { $0 != value }
    recentServers = Array(deduped.prefix(maxRecentServers))
  }

  /// The full server URL (origin, or origin + workspace mount) of a server the
  /// user previously connected to whose origin matches `origin`; nil when none.
  /// Reusing the recorded URL means a deep link to a KNOWN workspace server opens
  /// WITHOUT the network probe — the mount is already in the saved URL. Mirrors
  /// the desktop shell's `findKnownServerUrl` (web/electron/src/main.js).
  func knownServerURL(forOrigin origin: String) -> URL? {
    let candidates = (serverURL.map { [$0] } ?? []) + recentServers
    for value in candidates {
      guard let url = URL(string: value), url.omnigentOrigin == origin else { continue }
      return url
    }
    return nil
  }

  func isProtocolAllowed(_ scheme: String, from origin: String) -> Bool {
    allowedProtocols()[origin]?.contains(scheme.lowercased()) == true
  }

  func allowProtocol(_ scheme: String, from origin: String) {
    var grants = allowedProtocols()
    var schemes = grants[origin] ?? []
    let normalized = scheme.lowercased()
    if !schemes.contains(normalized) {
      schemes.append(normalized)
    }
    grants[origin] = schemes
    defaults.set(grants, forKey: Keys.allowedProtocols)
  }

  private func allowedProtocols() -> [String: [String]] {
    defaults.dictionary(forKey: Keys.allowedProtocols) as? [String: [String]] ?? [:]
  }

  private enum Keys {
    static let serverURL = "omnigent.serverURL"
    static let recentServers = "omnigent.recentServers"
    static let allowedProtocols = "omnigent.allowedProtocols"
    static let autoOpenSuppressedServer = "omnigent.autoOpenSuppressedServer"
  }
}

#if DEBUG
  extension ProcessInfo {
    func omnigentArgumentValue(after argumentName: String) -> String? {
      guard let index = arguments.firstIndex(of: argumentName) else { return nil }
      let valueIndex = arguments.index(after: index)
      guard arguments.indices.contains(valueIndex) else { return nil }

      let value = arguments[valueIndex].trimmingCharacters(in: .whitespacesAndNewlines)
      return value.isEmpty ? nil : value
    }
  }
#endif
