import Foundation

struct DatabricksOAuthTokens: Sendable, Equatable {
  let accessToken: String
  let refreshToken: String
  let expiresAt: Date
}

struct DatabricksOAuthClient: Sendable {
  private let session: URLSession
  // Reuse one isolated transport instead of allocating a delegate-backed session per login manager.
  private static let sharedSession = makeSession()

  init(session: URLSession? = nil) {
    self.session = session ?? Self.sharedSession
  }

  func exchange(code: String, for attempt: DatabricksOAuthAttempt) async throws
    -> DatabricksOAuthTokens
  {
    try Task.checkCancellation()
    let request = attempt.tokenRequest(code: code)
    let requestedAt = Date()
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
    guard let http = response as? HTTPURLResponse, http.url == request.url,
      http.statusCode == 200
    else { throw DatabricksOAuthError.tokenExchangeFailed }
    return try Self.tokens(from: data, requestedAt: requestedAt)
  }

  static func tokens(from data: Data, requestedAt: Date) throws -> DatabricksOAuthTokens {
    guard let response = try? JSONDecoder().decode(TokenResponse.self, from: data),
      response.tokenType.lowercased() == "bearer",
      validToken(response.accessToken), validToken(response.refreshToken),
      response.expiresIn.isFinite, response.expiresIn > 0
    else { throw DatabricksOAuthError.invalidTokenResponse }
    return DatabricksOAuthTokens(
      accessToken: response.accessToken, refreshToken: response.refreshToken,
      expiresAt: requestedAt.addingTimeInterval(response.expiresIn))
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

  private static func validToken(_ token: String) -> Bool {
    !token.isEmpty
      && token.rangeOfCharacter(from: .whitespacesAndNewlines.union(.controlCharacters)) == nil
  }

  private struct TokenResponse: Decodable {
    let accessToken: String
    let refreshToken: String
    let tokenType: String
    let expiresIn: Double

    enum CodingKeys: String, CodingKey {
      case accessToken = "access_token"
      case refreshToken = "refresh_token"
      case tokenType = "token_type"
      case expiresIn = "expires_in"
    }
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
