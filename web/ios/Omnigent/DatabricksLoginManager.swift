import AuthenticationServices
import Foundation

@MainActor
final class DatabricksLoginManager {
  private let client: DatabricksOAuthClient
  private let tokenManager: DatabricksTokenManager
  private let browser: WebAuthenticationBrowser
  private var activeID: UUID?
  private var operation: Task<DatabricksOAuthTokens, Error>?

  var isInFlight: Bool { activeID != nil }

  init(
    client: DatabricksOAuthClient = DatabricksOAuthClient(),
    tokenManager: DatabricksTokenManager = .shared,
    sessionFactory: WebAuthenticationBrowser.SessionFactory? = nil
  ) {
    self.client = client
    self.tokenManager = tokenManager
    browser = WebAuthenticationBrowser(sessionFactory: sessionFactory)
  }

  func signIn(
    workspaceURL: URL, configuration: DatabricksOAuthConfiguration, anchor: ASPresentationAnchor
  ) async throws -> DatabricksOAuthTokens {
    try Task.checkCancellation()
    guard !isInFlight else { throw DatabricksOAuthError.loginInProgress }
    let attempt = try DatabricksOAuthAttempt(
      workspaceURL: workspaceURL, configuration: configuration)
    let id = UUID()
    activeID = id
    let operation = Task {
      let signIn = try await self.tokenManager.beginSignIn(for: attempt.credentialScope)
      do {
        let callback = try await self.callbackURL(for: attempt, anchor: anchor, id: id)
        try Task.checkCancellation()
        let authorization = try attempt.authorizationResponse(from: callback)
        let tokens = try await self.client.exchange(
          code: authorization.code, for: attempt, issuer: authorization.issuer)
        try Task.checkCancellation()
        try await self.tokenManager.save(tokens, for: signIn)
        try Task.checkCancellation()
        return tokens
      } catch {
        await self.tokenManager.endSignIn(signIn)
        throw error
      }
    }
    self.operation = operation
    defer { cancel(id: id) }
    return try await withTaskCancellationHandler {
      let tokens = try await operation.value
      try Task.checkCancellation()
      guard activeID == id else { throw CancellationError() }
      return tokens
    } onCancel: {
      Task { @MainActor [weak self] in self?.cancel(id: id) }
    }
  }

  func cancel() {
    guard let activeID else { return }
    cancel(id: activeID)
  }

  private func callbackURL(
    for attempt: DatabricksOAuthAttempt, anchor: ASPresentationAnchor, id: UUID
  ) async throws -> URL {
    try Task.checkCancellation()
    guard activeID == id else { throw CancellationError() }
    let redirect = attempt.configuration.redirectURL
    do {
      return try await browser.callbackURL(
        for: attempt.authorizationURL, callback: .https(host: redirect.host!, path: redirect.path),
        anchor: anchor)
    } catch let error as WebAuthenticationBrowserError {
      switch error {
      case .inProgress: throw DatabricksOAuthError.loginInProgress
      case .unavailable: throw DatabricksOAuthError.browserUnavailable
      case .failed: throw DatabricksOAuthError.authenticationFailed
      case .missingCallback: throw DatabricksOAuthError.invalidCallback
      }
    }
  }

  private func cancel(id: UUID) {
    guard activeID == id else { return }
    activeID = nil
    operation?.cancel()
    operation = nil
    browser.cancel()
  }
}
