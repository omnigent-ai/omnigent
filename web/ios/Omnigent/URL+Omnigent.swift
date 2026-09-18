import Foundation
import WebKit

/// Host-based authentication classification, independent of workspace mount discovery.
enum ServerAuthentication: Equatable {
  case databricksWorkspace
  case databricksApp
  case oidc

  init(origin: String?) {
    self.init(host: origin.flatMap { URL(string: $0)?.host })
  }

  init(host: String?) {
    guard let host = host?.lowercased() else {
      self = .oidc
      return
    }
    if Self.workspaceDomains.contains(where: { Self.matches(host, domain: $0) }) {
      self = .databricksWorkspace
    } else if Self.matches(host, domain: Self.appsDomain) {
      self = .databricksApp
    } else {
      self = .oidc
    }
  }

  /// Platform SSO runs inline so its cookies land in WebKit's cookie store.
  var usesInWebViewAuth: Bool {
    switch self {
    case .databricksWorkspace, .databricksApp: true
    case .oidc: false
    }
  }

  private static let workspaceDomains = ["databricks.com", "azuredatabricks.net"]
  private static let appsDomain = "databricksapps.com"

  private static func matches(_ host: String, domain: String) -> Bool {
    host == domain || host.hasSuffix(".\(domain)")
  }
}

extension URL {
  var omnigentOrigin: String? {
    guard let scheme, let host else { return nil }
    var components = URLComponents()
    components.scheme = scheme.lowercased()
    components.host = host.lowercased()
    components.port = port
    return components.url?.absoluteString.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
  }

  var omnigentHostLabel: String {
    guard let host else { return absoluteString }
    if let port {
      return "\(host):\(port)"
    }
    return host
  }
}

extension WKSecurityOrigin {
  var omnigentOrigin: String? {
    guard !self.protocol.isEmpty, !host.isEmpty else { return nil }
    var components = URLComponents()
    components.scheme = self.protocol.lowercased()
    components.host = host.lowercased()
    if port > 0 && !Self.isDefaultPort(port, for: self.protocol) {
      components.port = port
    }
    return components.url?.absoluteString.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
  }

  private static func isDefaultPort(_ port: Int, for scheme: String) -> Bool {
    (scheme == "https" && port == 443) || (scheme == "http" && port == 80)
  }
}
