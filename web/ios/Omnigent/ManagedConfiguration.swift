import Foundation
import ManagedApp

/// Server URLs an administrator preset for a managed install.
///
/// Arrives on either of two channels, both carrying the same flat dictionary:
/// the `AppConfig` payload of a `com.apple.configuration.app.managed`
/// declaration, or the classic `com.apple.configuration.managed` defaults key.
///
/// Validation lives in `init(from:)` on purpose: the framework reports a thrown
/// `ManagedAppConfigurationDecodingError` back to the management service and
/// logs it in the device event log, so a bad value in an admin console becomes
/// actionable feedback instead of a server that silently never shows up. The
/// published spec (`web/ios/docs/managed-app-configuration.md`) documents every
/// code below, so treat them as API.
struct OmnigentManagedConfiguration: Decodable, Equatable {
  private(set) var serverURLs: [URL]

  static let empty = OmnigentManagedConfiguration(serverURLs: [])

  /// Bounds the payload so a runaway list can't crowd the user's own servers
  /// off the connect screen. Apple also asks configurations to stay small.
  static let maxServerURLs = 10

  enum CodingKeys: String, CodingKey {
    case serverURLs = "serverUrls"
  }

  init(serverURLs: [URL]) {
    self.serverURLs = serverURLs
  }

  init(from decoder: Decoder) throws {
    let values = try decoder.container(keyedBy: CodingKeys.self)

    // The key is optional: a configuration that sets nothing is valid and
    // simply offers no servers.
    guard values.contains(.serverURLs) else {
      serverURLs = []
      return
    }

    let raw: [String]
    do {
      raw = try values.decode([String].self, forKey: .serverURLs)
    } catch {
      throw Failure(
        code: Self.wrongTypeCode,
        message: "'serverUrls' must be an array of strings.")
    }

    guard raw.count <= Self.maxServerURLs else {
      throw Failure(
        code: Self.tooManyEntriesCode,
        message:
          "'serverUrls' has \(raw.count) entries; at most \(Self.maxServerURLs) are allowed.")
    }

    var origins: Set<String> = []
    var urls: [URL] = []
    for entry in raw {
      let url = try Self.normalize(entry)
      // Same origin comparison the rest of the shell uses, so a duplicate is
      // dropped rather than offered twice. Note this does not canonicalize a
      // redundant `:443`, matching `URL.omnigentOrigin` everywhere else.
      guard let origin = url.omnigentOrigin else { throw Self.invalidURL(entry) }
      if origins.insert(origin).inserted {
        urls.append(url)
      }
    }
    serverURLs = urls
  }

  /// Reuses the shell's own URL rules so a preset server and a typed one are
  /// held to exactly the same standard. `allowsInsecureHTTP: false` even in
  /// debug builds: release builds keep App Transport Security defaults, so an
  /// `http://` preset would fail at the network layer on a real managed device
  /// no matter what a debug build accepts. Rejecting it here turns that into a
  /// console error the admin can act on.
  private static func normalize(_ entry: String) throws -> URL {
    do {
      return try ServerURL.normalize(entry, allowsInsecureHTTP: false)
    } catch let error as ServerURLError {
      switch error {
      case .unsupportedScheme, .insecureHTTPNotAllowed:
        throw Failure(
          code: notHTTPSCode,
          message: "Server URL '\(entry)' must use https://.")
      case .empty, .invalid:
        throw invalidURL(entry)
      }
    }
  }

  private static func invalidURL(_ entry: String) -> Failure {
    Failure(code: invalidURLCode, message: "'\(entry)' is not a valid server URL.")
  }

  /// The defaults key a management service writes for classic (non-declarative)
  /// managed app configuration. Read-only from the app's side.
  static let legacyDefaultsKey = "com.apple.configuration.managed"

  /// Reads the classic managed app configuration, if a service pushed one.
  ///
  /// Returns `.empty` for anything unusable. Unlike a declarative
  /// configuration, this channel has no way to report a decoding error back to
  /// the administrator, so a bad value can only be ignored — which is why the
  /// declarative path is preferred when both are present.
  static func read(legacyFrom defaults: UserDefaults) -> OmnigentManagedConfiguration {
    guard let dictionary = defaults.dictionary(forKey: legacyDefaultsKey) else { return .empty }
    do {
      let data = try PropertyListSerialization.data(
        fromPropertyList: dictionary, format: .binary, options: 0)
      return try PropertyListDecoder().decode(Self.self, from: data)
    } catch {
      return .empty
    }
  }

  struct Failure: ManagedAppConfigurationDecodingError {
    var code: ManagedAppConfigurationDecodingErrorCode
    var message: String
  }

  // Published error codes, part of the spec admins read. `init(rawValue:)`
  // rejects anything the system reserves, so a value that drifted into the
  // reserved range would be nil here — `ManagedConfigurationTests` asserts each
  // one stays below `firstReserved` so that surfaces as a test failure.
  static let invalidURLCode = ManagedAppConfigurationDecodingErrorCode(rawValue: 100)!
  static let notHTTPSCode = ManagedAppConfigurationDecodingErrorCode(rawValue: 101)!
  static let tooManyEntriesCode = ManagedAppConfigurationDecodingErrorCode(rawValue: 102)!
  static let wrongTypeCode = ManagedAppConfigurationDecodingErrorCode(rawValue: 103)!
}

/// Merges administrator-preset servers with the user's own recents for display.
///
/// Preset servers are combined at read time and never written to
/// `SettingsStore`, so withdrawing a configuration removes them cleanly and
/// they never consume the recents cap and evict a server the user chose.
enum ManagedServers {
  /// `recents` minus any origin the administrator already presets, so a preset
  /// server is offered once rather than twice.
  static func recents(_ recents: [String], excludingManaged managed: [URL]) -> [String] {
    guard !managed.isEmpty else { return recents }
    let managedOrigins = Set(managed.compactMap(\.omnigentOrigin))
    return recents.filter { value in
      guard let origin = URL(string: value)?.omnigentOrigin else { return true }
      return !managedOrigins.contains(origin)
    }
  }

  /// Every server to offer, administrator-preset ones first.
  static func merged(managed: [URL], recents: [String]) -> [String] {
    managed.map(\.absoluteString) + Self.recents(recents, excludingManaged: managed)
  }

  /// Picks between the two configuration channels. A declarative configuration
  /// wins because it is validated with feedback to the administrator; the
  /// classic key is the fallback for services that don't send declarations.
  static func resolve(declarative: [URL], legacy: [URL]) -> [URL] {
    declarative.isEmpty ? legacy : declarative
  }
}
