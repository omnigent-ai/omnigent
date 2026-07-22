import Foundation

@MainActor
final class SettingsStore: ObservableObject {
  @Published var serverURL: String? {
    didSet { defaults.set(serverURL, forKey: Keys.serverURL) }
  }

  @Published private(set) var recentServers: [String] {
    didSet { defaults.set(recentServers, forKey: Keys.recentServers) }
  }

  private let defaults: UserDefaults
  private let maxRecentServers = 5

  init(defaults: UserDefaults = .standard) {
    self.defaults = defaults
    #if DEBUG
      serverURL =
        ProcessInfo.processInfo.omnigentArgumentValue(after: "--omnigent-server-url")
        ?? ProcessInfo.processInfo.environment["OMNIGENT_SCREENSHOT_APP_URL"]
        ?? defaults.string(forKey: Keys.serverURL)
    #else
      serverURL = defaults.string(forKey: Keys.serverURL)
    #endif
    recentServers = defaults.stringArray(forKey: Keys.recentServers) ?? []
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
  }
}

#if DEBUG
  extension ProcessInfo {
    fileprivate func omnigentArgumentValue(after argumentName: String) -> String? {
      guard let index = arguments.firstIndex(of: argumentName) else { return nil }
      let valueIndex = arguments.index(after: index)
      guard arguments.indices.contains(valueIndex) else { return nil }

      let value = arguments[valueIndex].trimmingCharacters(in: .whitespacesAndNewlines)
      return value.isEmpty ? nil : value
    }
  }
#endif
