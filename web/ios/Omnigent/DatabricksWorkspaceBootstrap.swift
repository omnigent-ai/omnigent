import AuthenticationServices
import Foundation

@MainActor
protocol DatabricksSigningIn {
  func signIn(
    workspaceURL: URL, configuration: DatabricksOAuthConfiguration, anchor: ASPresentationAnchor
  ) async throws -> DatabricksOAuthTokens
  func cancel()
}

extension DatabricksLoginManager: DatabricksSigningIn {}

@MainActor
final class DatabricksWorkspaceBootstrap {
  private let tokens: DatabricksTokenManager
  private let login: any DatabricksSigningIn
  private let sessions: any DatabricksSessionCreating
  private let installer: DatabricksCookieInstaller

  init(
    tokens: DatabricksTokenManager = .shared,
    login: (any DatabricksSigningIn)? = nil,
    sessions: (any DatabricksSessionCreating)? = nil,
    installer: DatabricksCookieInstaller? = nil
  ) {
    self.tokens = tokens
    self.login = login ?? DatabricksLoginManager(tokenManager: tokens)
    self.sessions = sessions ?? DatabricksSessionClient()
    self.installer = installer ?? .shared
  }

  func prepare(
    context: DatabricksWebContext, store: any DatabricksWebStoring, anchor: ASPresentationAnchor
  ) async throws -> DatabricksWebSession {
    try Task.checkCancellation()
    guard store.identifier == context.storeIdentifier else {
      throw DatabricksSessionError.workspaceChanged
    }
    let saved = try await tokens.tokens(for: context.scope)
    try Task.checkCancellation()
    let credential: DatabricksOAuthTokens
    if let saved {
      credential = saved
    } else {
      // Clear before a new grant can be saved, even if cookie bootstrap later fails or is canceled.
      try await installer.install([], in: store, reset: true)
      try Task.checkCancellation()
      credential = try await login.signIn(
        workspaceURL: context.pageURL, configuration: context.configuration, anchor: anchor)
    }
    try Task.checkCancellation()
    let session = try await sessions.create(context: context, tokens: credential)
    try Task.checkCancellation()
    guard try await tokens.isCurrent(credential, for: context.scope) else {
      throw DatabricksSessionError.credentialsChanged
    }
    try await installer.install(session.cookies, in: store, reset: false) { [tokens] in
      guard try await tokens.isCurrent(credential, for: context.scope) else {
        throw DatabricksSessionError.credentialsChanged
      }
    }
    let installed = await store.cookies()
    try Task.checkCancellation()
    guard
      installed.contains(where: { cookie in
        cookie.name == "DBAUTH" && cookie.isSecure && cookie.isHTTPOnly
          && DatabricksSessionClient.cookie(cookie, appliesTo: session.pageURL)
          && session.cookies.contains {
            $0.name == cookie.name && $0.domain == cookie.domain && $0.path == cookie.path
              && $0.value == cookie.value
          }
      })
    else { throw DatabricksSessionError.missingCookie }
    guard try await tokens.isCurrent(credential, for: context.scope) else {
      throw DatabricksSessionError.credentialsChanged
    }
    return session
  }

  func cancel() { login.cancel() }
}
