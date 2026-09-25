import Foundation

struct DatabricksOAuthTokens: Codable, Sendable, Equatable {
  let accessToken: String
  let refreshToken: String
  let expiresAt: Date
  let issuer: DatabricksOAuthIssuer?

  init(
    accessToken: String, refreshToken: String, expiresAt: Date, issuer: DatabricksOAuthIssuer? = nil
  ) {
    self.accessToken = accessToken
    self.refreshToken = refreshToken
    self.expiresAt = expiresAt
    self.issuer = issuer
  }

  var isValid: Bool {
    [accessToken, refreshToken].allSatisfy {
      !$0.isEmpty
        && $0.rangeOfCharacter(from: .whitespacesAndNewlines.union(.controlCharacters)) == nil
    } && expiresAt.timeIntervalSince1970.isFinite
  }
}

protocol DatabricksTokenRefreshing: Sendable {
  func refresh(
    _ refreshToken: String, for scope: DatabricksCredentialScope, issuer: DatabricksOAuthIssuer?
  ) async throws
    -> DatabricksOAuthTokens
}

struct DatabricksOAuthClient: DatabricksTokenRefreshing {
  private let session: URLSession
  // Reuse one isolated transport instead of allocating a delegate-backed session per login manager.
  private static let sharedSession = makeSession()

  init(session: URLSession? = nil) {
    self.session = session ?? Self.sharedSession
  }

  func exchange(
    code: String, for attempt: DatabricksOAuthAttempt, issuer: DatabricksOAuthIssuer? = nil
  ) async throws
    -> DatabricksOAuthTokens
  {
    let issuer =
      try issuer ?? DatabricksOAuthIssuer(attempt.workspaceOrigin.appendingPathComponent("oidc"))
    var request = URLRequest(url: issuer.discoveryURL)
    request.setValue("application/json", forHTTPHeaderField: "Accept")
    request.timeoutInterval = 30
    let (data, response) = try await send(request)
    guard response.statusCode == 200 else { throw DatabricksOAuthError.invalidDiscovery }
    try issuer.validateDiscovery(data)
    return try await requestTokens(attempt.tokenRequest(code: code, issuer: issuer), issuer: issuer)
  }

  func refresh(
    _ refreshToken: String, for scope: DatabricksCredentialScope,
    issuer: DatabricksOAuthIssuer? = nil
  ) async throws
    -> DatabricksOAuthTokens
  {
    let request = Self.tokenRequest(
      for: scope,
      fields: [
        ("grant_type", "refresh_token"), ("refresh_token", refreshToken),
      ], issuer: issuer)
    return try await requestTokens(request, previousRefreshToken: refreshToken, issuer: issuer)
  }

  private func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
    try Task.checkCancellation()
    let data: Data
    let response: URLResponse
    do {
      (data, response) = try await session.data(for: request)
    } catch {
      if Task.isCancelled || (error as? URLError)?.code == .cancelled {
        throw CancellationError()
      }
      throw DatabricksOAuthError.networkUnavailable
    }
    try Task.checkCancellation()
    guard let http = response as? HTTPURLResponse, http.url == request.url else {
      throw DatabricksOAuthError.tokenExchangeFailed
    }
    return (data, http)
  }

  private func requestTokens(
    _ request: URLRequest, previousRefreshToken: String? = nil, issuer: DatabricksOAuthIssuer? = nil
  )
    async throws -> DatabricksOAuthTokens
  {
    let requestedAt = Date()
    let (data, http) = try await send(request)
    if previousRefreshToken != nil, http.statusCode == 400,
      (try? JSONDecoder().decode(ErrorResponse.self, from: data))?.error == "invalid_grant"
    {
      throw DatabricksOAuthError.invalidRefreshGrant
    }
    guard http.statusCode == 200 else { throw DatabricksOAuthError.tokenExchangeFailed }
    return try Self.tokens(
      from: data, requestedAt: requestedAt, previousRefreshToken: previousRefreshToken,
      issuer: issuer)
  }

  static func tokens(
    from data: Data, requestedAt: Date, previousRefreshToken: String? = nil,
    issuer: DatabricksOAuthIssuer? = nil
  ) throws
    -> DatabricksOAuthTokens
  {
    guard let response = try? JSONDecoder().decode(TokenResponse.self, from: data),
      response.tokenType.lowercased() == "bearer",
      let refreshToken = response.refreshToken ?? previousRefreshToken,
      response.expiresIn.isFinite, response.expiresIn > 0
    else { throw DatabricksOAuthError.invalidTokenResponse }
    let tokens = DatabricksOAuthTokens(
      accessToken: response.accessToken, refreshToken: refreshToken,
      expiresAt: requestedAt.addingTimeInterval(response.expiresIn), issuer: issuer)
    guard tokens.isValid else { throw DatabricksOAuthError.invalidTokenResponse }
    return tokens
  }

  static func tokenRequest(
    for scope: DatabricksCredentialScope, fields: [(String, String)],
    issuer: DatabricksOAuthIssuer? = nil
  )
    -> URLRequest
  {
    var request = URLRequest(
      url: issuer?.tokenEndpoint ?? scope.workspaceOrigin.appendingPathComponent("oidc/v1/token"))
    request.httpMethod = "POST"
    request.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
    request.setValue("application/json", forHTTPHeaderField: "Accept")
    request.timeoutInterval = 30
    let unreserved = CharacterSet(
      charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
    let fields = [("client_id", scope.clientID)] + fields
    request.httpBody = Data(
      fields.map {
        "\($0.0.addingPercentEncoding(withAllowedCharacters: unreserved)!)=\($0.1.addingPercentEncoding(withAllowedCharacters: unreserved)!)"
      }.joined(separator: "&").utf8)
    return request
  }

  static func makeSession(configuration: URLSessionConfiguration = .ephemeral) -> URLSession {
    configuration.httpCookieStorage = nil
    configuration.httpShouldSetCookies = false
    configuration.urlCredentialStorage = nil
    configuration.urlCache = nil
    configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
    configuration.timeoutIntervalForRequest = 30
    return URLSession(
      configuration: configuration, delegate: DatabricksOAuthRedirectBlocker(), delegateQueue: nil)
  }

  private struct TokenResponse: Decodable {
    let accessToken: String
    let refreshToken: String?
    let tokenType: String
    let expiresIn: Double

    init(from decoder: Decoder) throws {
      let container = try decoder.container(keyedBy: CodingKeys.self)
      accessToken = try container.decode(String.self, forKey: .accessToken)
      tokenType = try container.decode(String.self, forKey: .tokenType)
      expiresIn = try container.decode(Double.self, forKey: .expiresIn)
      refreshToken =
        container.contains(.refreshToken)
        ? try container.decode(String.self, forKey: .refreshToken) : nil
    }

    enum CodingKeys: String, CodingKey {
      case accessToken = "access_token"
      case refreshToken = "refresh_token"
      case tokenType = "token_type"
      case expiresIn = "expires_in"
    }
  }

  private struct ErrorResponse: Decodable {
    let error: String
  }
}

/// Authorization codes and verifiers must never be forwarded through HTTP redirects.
final class DatabricksOAuthRedirectBlocker: NSObject, URLSessionTaskDelegate {
  func urlSession(
    _ session: URLSession, task: URLSessionTask,
    willPerformHTTPRedirection response: HTTPURLResponse,
    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void
  ) {
    completionHandler(nil)
  }
}
