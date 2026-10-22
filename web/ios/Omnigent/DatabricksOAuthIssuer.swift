import Foundation

/// Provider authority, distinct from the entered page origin.
struct DatabricksOAuthIssuer: Codable, Equatable, Sendable {
  let url: URL

  init(_ url: URL) throws {
    guard let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
      components.scheme == "https",
      ServerAuthentication(host: components.host) == .databricksWorkspace,
      components.user == nil, components.password == nil,
      components.port == nil || components.port == 443,
      components.query == nil, components.fragment == nil
    else { throw DatabricksOAuthError.invalidIssuer }
    let path = components.percentEncodedPath
    if path != "/oidc" {
      let prefix = "/oidc/accounts/"
      guard path.hasPrefix(prefix) else { throw DatabricksOAuthError.invalidIssuer }
      let accountID = path.dropFirst(prefix.count)
      let allowed = CharacterSet(
        charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
      guard !accountID.isEmpty, accountID.unicodeScalars.allSatisfy(allowed.contains) else {
        throw DatabricksOAuthError.invalidIssuer
      }
    }
    self.url = url
  }

  var discoveryURL: URL { url.appendingPathComponent(".well-known/openid-configuration") }
  var tokenEndpoint: URL { url.appendingPathComponent("v1/token") }

  func validateDiscovery(_ data: Data) throws {
    guard let document = try? JSONDecoder().decode(Discovery.self, from: data),
      document.issuer == url.absoluteString,
      document.tokenEndpoint == tokenEndpoint.absoluteString
    else { throw DatabricksOAuthError.invalidDiscovery }
  }

  init(from decoder: Decoder) throws {
    let container = try decoder.singleValueContainer()
    try self.init(container.decode(URL.self))
  }

  func encode(to encoder: Encoder) throws {
    var container = encoder.singleValueContainer()
    try container.encode(url)
  }

  private struct Discovery: Decodable {
    let issuer: String
    let tokenEndpoint: String

    enum CodingKeys: String, CodingKey {
      case issuer
      case tokenEndpoint = "token_endpoint"
    }
  }
}
