import CryptoKit
import Foundation
import Security

/// Transient material for one authorization-code flow; never persisted or sent to JavaScript.
struct DatabricksOAuthAttempt: Sendable {
  let configuration: DatabricksOAuthConfiguration
  let credentialScope: DatabricksCredentialScope
  var workspaceOrigin: URL { credentialScope.workspaceOrigin }
  let state: String
  let verifier: String

  init(workspaceURL: URL, configuration: DatabricksOAuthConfiguration) throws {
    credentialScope = try DatabricksCredentialScope(
      workspaceURL: workspaceURL, configuration: configuration)
    self.configuration = configuration
    state = try Self.randomValue()
    verifier = try Self.randomValue()
  }

  var authorizationURL: URL {
    var components = URLComponents(
      url: workspaceOrigin.appendingPathComponent("oidc/v1/authorize"),
      resolvingAgainstBaseURL: false)!
    components.queryItems = [
      URLQueryItem(name: "client_id", value: configuration.clientID),
      URLQueryItem(name: "redirect_uri", value: configuration.redirectURL.absoluteString),
      URLQueryItem(name: "response_type", value: "code"),
      URLQueryItem(name: "scope", value: Self.scope),
      URLQueryItem(name: "state", value: state),
      URLQueryItem(name: "code_challenge", value: Self.challenge(for: verifier)),
      URLQueryItem(name: "code_challenge_method", value: "S256"),
    ]
    // OAuth servers decode query parameters as form data, where a literal + is a space.
    components.percentEncodedQuery = components.percentEncodedQuery?.replacingOccurrences(
      of: "+", with: "%2B")
    return components.url!
  }

  func authorizationCode(from callback: URL) throws -> String {
    guard configuration.matchesCallback(callback),
      let items = URLComponents(url: callback, resolvingAgainstBaseURL: false)?.queryItems
    else { throw DatabricksOAuthError.invalidCallback }
    let states = items.filter { $0.name == "state" }
    let codes = items.filter { $0.name == "code" }
    let errors = items.filter { $0.name == "error" }
    guard states.count == 1, states.first?.value == state else {
      throw DatabricksOAuthError.invalidCallback
    }
    if !errors.isEmpty {
      guard errors.count == 1, codes.isEmpty, let error = errors.first?.value, !error.isEmpty
      else { throw DatabricksOAuthError.invalidCallback }
      throw error == "access_denied"
        ? DatabricksOAuthError.authorizationDenied : DatabricksOAuthError.authorizationFailed
    }
    guard codes.count == 1, let code = codes.first?.value, !code.isEmpty else {
      throw DatabricksOAuthError.invalidCallback
    }
    return code
  }

  func tokenRequest(code: String) -> URLRequest {
    DatabricksOAuthClient.tokenRequest(
      for: credentialScope,
      fields: [
        ("grant_type", "authorization_code"),
        ("redirect_uri", configuration.redirectURL.absoluteString), ("scope", Self.scope),
        ("code_verifier", verifier), ("code", code),
      ])
  }

  static func challenge(for verifier: String) -> String {
    base64URL(Data(SHA256.hash(data: Data(verifier.utf8))))
  }

  private static let scope = "all-apis offline_access"

  private static func randomValue() throws -> String {
    var bytes = [UInt8](repeating: 0, count: 32)
    guard SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes) == errSecSuccess else {
      throw DatabricksOAuthError.randomGenerationFailed
    }
    return base64URL(Data(bytes))
  }

  private static func base64URL(_ data: Data) -> String {
    data.base64EncodedString().replacingOccurrences(of: "+", with: "-")
      .replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: "=", with: "")
  }

}
