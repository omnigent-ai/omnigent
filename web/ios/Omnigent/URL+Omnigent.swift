import Foundation
import WebKit

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
