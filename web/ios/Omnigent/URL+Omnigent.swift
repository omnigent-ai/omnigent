import Foundation
import WebKit

/// Server domains whose platform SSO must run inside the WebView so its cookies
/// land in the WebView's own cookie store.
private let inWebViewAuthDomains = ["databricks.com", "azuredatabricks.net", "databricksapps.com"]

/// Whether the pinned server uses an authentication redirect chain that must
/// remain in the WebView rather than Omnigent's system-browser OIDC handoff.
func usesInWebViewAuth(_ origin: String?) -> Bool {
  guard let origin, let host = URL(string: origin)?.host?.lowercased() else { return false }
  return inWebViewAuthDomains.contains { host == $0 || host.hasSuffix(".\($0)") }
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
