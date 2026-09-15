import AuthenticationServices
import Foundation

@MainActor
protocol DatabricksAuthenticationSession: AnyObject {
  func start() -> Bool
  func cancel()
}

extension ASWebAuthenticationSession: DatabricksAuthenticationSession {}

@MainActor
final class DatabricksLoginManager {
  typealias SessionFactory = (
    URL, ASWebAuthenticationSession.Callback, ASWebAuthenticationPresentationContextProviding,
    @escaping ASWebAuthenticationSession.CompletionHandler
  ) -> any DatabricksAuthenticationSession

  private let client: DatabricksOAuthClient
  private let makeSession: SessionFactory
  private var activeID: UUID?
  private var operation: Task<DatabricksOAuthTokens, Error>?
  private var browser: (any DatabricksAuthenticationSession)?
  private var presentationContext: DatabricksPresentationContext?
  private var callbackContinuation: CheckedContinuation<URL, Error>?

  var isInFlight: Bool { activeID != nil }

  init(
    client: DatabricksOAuthClient = DatabricksOAuthClient(), sessionFactory: SessionFactory? = nil
  ) {
    self.client = client
    makeSession =
      sessionFactory ?? { url, callback, provider, completion in
        let session = ASWebAuthenticationSession(
          url: url, callback: callback, completionHandler: completion)
        session.presentationContextProvider = provider
        session.prefersEphemeralWebBrowserSession = false
        return session
      }
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
      let callback = try await self.callbackURL(for: attempt, anchor: anchor, id: id)
      try Task.checkCancellation()
      let code = try attempt.authorizationCode(from: callback)
      let tokens = try await self.client.exchange(code: code, for: attempt)
      try Task.checkCancellation()
      return tokens
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
    return try await withCheckedThrowingContinuation { continuation in
      let context = DatabricksPresentationContext(anchor: anchor)
      presentationContext = context
      callbackContinuation = continuation
      let redirect = attempt.configuration.redirectURL
      let browser = makeSession(
        attempt.authorizationURL, .https(host: redirect.host!, path: redirect.path), context
      ) { [weak self] url, error in
        Task { @MainActor in self?.completeBrowser(id: id, url: url, error: error) }
      }
      self.browser = browser
      if !browser.start() {
        callbackContinuation = nil
        continuation.resume(throwing: DatabricksOAuthError.browserUnavailable)
      }
    }
  }

  private func completeBrowser(id: UUID, url: URL?, error: Error?) {
    guard activeID == id, let continuation = callbackContinuation else { return }
    callbackContinuation = nil
    if let error {
      let error = error as NSError
      if error.domain == ASWebAuthenticationSessionErrorDomain,
        error.code == ASWebAuthenticationSessionError.Code.canceledLogin.rawValue
      {
        continuation.resume(throwing: CancellationError())
      } else {
        continuation.resume(throwing: DatabricksOAuthError.authenticationFailed)
      }
    } else if let url {
      continuation.resume(returning: url)
    } else {
      continuation.resume(throwing: DatabricksOAuthError.invalidCallback)
    }
  }

  private func cancel(id: UUID) {
    guard activeID == id else { return }
    activeID = nil
    operation?.cancel()
    operation = nil
    let continuation = callbackContinuation
    callbackContinuation = nil
    let browser = browser
    self.browser = nil
    browser?.cancel()
    presentationContext = nil
    // Programmatic browser cancellation need not invoke its completion handler.
    continuation?.resume(throwing: CancellationError())
  }
}

@MainActor
private final class DatabricksPresentationContext: NSObject,
  ASWebAuthenticationPresentationContextProviding
{
  let anchor: ASPresentationAnchor

  init(anchor: ASPresentationAnchor) {
    self.anchor = anchor
  }

  func presentationAnchor(for session: ASWebAuthenticationSession) -> ASPresentationAnchor {
    anchor
  }
}
