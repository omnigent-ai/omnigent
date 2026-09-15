import Foundation

struct DatabricksOAuthConfiguration: Sendable {
  let clientID: String
  let redirectURL: URL

  init(clientID: String, redirectURL: String) throws {
    guard !clientID.isEmpty, !clientID.contains("$("),
      clientID.rangeOfCharacter(from: .whitespacesAndNewlines.union(.controlCharacters)) == nil
    else { throw DatabricksOAuthError.invalidClientID }
    guard let url = URL(string: redirectURL),
      let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
      components.scheme?.lowercased() == "https",
      let host = components.host, !host.isEmpty,
      components.user == nil, components.password == nil,
      components.port == nil || components.port == 443,
      !components.path.isEmpty,
      components.query == nil, components.fragment == nil,
      !redirectURL.contains("$("),
      redirectURL.rangeOfCharacter(from: .whitespacesAndNewlines.union(.controlCharacters)) == nil
    else { throw DatabricksOAuthError.invalidRedirectURL }
    self.clientID = clientID
    self.redirectURL = url
  }

  static func load(bundle: Bundle = .main) throws -> Self {
    try Self(
      clientID: bundle.object(forInfoDictionaryKey: "DatabricksOAuthClientID") as? String ?? "",
      redirectURL: bundle.object(forInfoDictionaryKey: "DatabricksOAuthRedirectURL") as? String
        ?? ""
    )
  }

  func matchesCallback(_ url: URL) -> Bool {
    guard let actual = URLComponents(url: url, resolvingAgainstBaseURL: false),
      let expected = URLComponents(url: redirectURL, resolvingAgainstBaseURL: false)
    else { return false }
    return actual.scheme?.lowercased() == "https"
      && actual.host?.lowercased() == expected.host?.lowercased()
      && (actual.port ?? 443) == (expected.port ?? 443)
      && actual.percentEncodedPath == expected.percentEncodedPath
      && actual.user == nil && actual.password == nil && actual.fragment == nil
  }
}

enum DatabricksOAuthError: Error, Equatable, LocalizedError {
  case invalidClientID
  case invalidRedirectURL
  case invalidWorkspace
  case randomGenerationFailed
  case invalidCallback
  case authorizationDenied
  case authorizationFailed
  case loginInProgress
  case browserUnavailable
  case authenticationFailed
  case networkUnavailable
  case tokenExchangeFailed
  case invalidRefreshGrant
  case invalidTokenResponse

  var errorDescription: String? {
    switch self {
    case .invalidClientID: "Configure a Databricks OAuth client ID for this build."
    case .invalidRedirectURL: "Configure a valid HTTPS OAuth redirect URL for this build."
    case .invalidWorkspace: "Databricks OAuth requires an HTTPS workspace URL."
    case .randomGenerationFailed: "Could not prepare secure sign-in. Please try again."
    case .invalidCallback: "Databricks returned an invalid sign-in callback."
    case .authorizationDenied: "Databricks sign-in was not authorized."
    case .authorizationFailed: "Databricks could not authorize this sign-in."
    case .loginInProgress: "A Databricks sign-in is already in progress."
    case .browserUnavailable: "Could not present the sign-in browser."
    case .authenticationFailed: "Databricks browser sign-in failed."
    case .networkUnavailable: "Could not reach Databricks. Please try again."
    case .tokenExchangeFailed: "Databricks could not complete sign-in. Please sign in again."
    case .invalidRefreshGrant: "The saved Databricks session has expired. Please sign in again."
    case .invalidTokenResponse: "Databricks returned an invalid token response."
    }
  }
}
