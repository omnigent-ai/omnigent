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
  private let signOuts: DatabricksSignOutManager

  init(
    tokens: DatabricksTokenManager = .shared,
    login: (any DatabricksSigningIn)? = nil,
    sessions: (any DatabricksSessionCreating)? = nil,
    installer: DatabricksCookieInstaller? = nil,
    signOuts: DatabricksSignOutManager? = nil
  ) {
    self.tokens = tokens
    self.login = login ?? DatabricksLoginManager(tokenManager: tokens)
    self.sessions = sessions ?? DatabricksSessionClient()
    self.installer = installer ?? .shared
    self.signOuts = signOuts ?? .shared
  }

  func prepare(
    context: DatabricksWebContext, store: any DatabricksWebStoring, anchor: ASPresentationAnchor,
    intent: DatabricksConnectionIntent = .connect, pageURL: URL? = nil
  ) async throws -> DatabricksWebSession {
    try Task.checkCancellation()
    guard store.identifier == context.storeIdentifier else {
      throw DatabricksSessionError.workspaceChanged
    }
    try await signOuts.finishPending(context: context, store: store)
    try Task.checkCancellation()
    let lifecycleGeneration = signOuts.generation(context.storeIdentifier)
    func checkActive() throws {
      try Task.checkCancellation()
      guard !signOuts.isPending(context.storeIdentifier),
        signOuts.generation(context.storeIdentifier) == lifecycleGeneration
      else {
        throw DatabricksSessionError.credentialsChanged
      }
    }
    let destination = try pageURL.map { try context.navigating(to: $0) } ?? context
    let saved = try await tokens.tokens(for: context.scope)
    try checkActive()
    var credential: DatabricksOAuthTokens
    if let saved {
      credential = saved
    } else {
      guard intent == .connect else { throw DatabricksSessionError.reauthenticationRequired }
      // Clear before a new grant can be saved, even if cookie bootstrap later fails or is canceled.
      try await installer.install([], in: store, reset: true) { try checkActive() }
      try checkActive()
      credential = try await login.signIn(
        workspaceURL: context.pageURL, configuration: context.configuration, anchor: anchor)
    }
    try checkActive()
    let session: DatabricksWebSession
    do {
      session = try await sessions.create(context: destination, tokens: credential)
    } catch DatabricksSessionError.rejected(401) where saved != nil {
      try checkActive()
      guard let refreshed = try await tokens.refresh(rejected: credential, for: context.scope)
      else {
        throw DatabricksSessionError.reauthenticationRequired
      }
      credential = refreshed
      try checkActive()
      session = try await sessions.create(context: destination, tokens: credential)
    }
    try checkActive()
    guard !signOuts.isPending(context.storeIdentifier),
      try await tokens.isCurrent(credential, for: context.scope)
    else {
      throw DatabricksSessionError.credentialsChanged
    }
    let acceptedCredential = credential
    try await installer.install(session.cookies, in: store, reset: false) { [tokens, signOuts] in
      guard !signOuts.isPending(context.storeIdentifier),
        signOuts.generation(context.storeIdentifier) == lifecycleGeneration,
        try await tokens.isCurrent(acceptedCredential, for: context.scope)
      else {
        throw DatabricksSessionError.credentialsChanged
      }
    }
    let installed = await store.cookies()
    try checkActive()
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
    guard !signOuts.isPending(context.storeIdentifier),
      try await tokens.isCurrent(credential, for: context.scope)
    else {
      throw DatabricksSessionError.credentialsChanged
    }
    try checkActive()
    return session
  }

  #if DEBUG
    /// Break one piece of the live session so a tester can watch recovery happen. False when the
    /// workspace has no saved credentials to fault. Debug builds only.
    func inject(
      _ fault: DatabricksDebugFault, context: DatabricksWebContext, store: any DatabricksWebStoring
    ) async throws -> Bool {
      guard store.identifier == context.storeIdentifier else {
        throw DatabricksSessionError.workspaceChanged
      }
      guard let credential = fault.credential else {
        // Same serialized queue as bootstrap, so a concurrent install cannot interleave.
        try await installer.install([], in: store, reset: false)
        return true
      }
      return try await tokens.inject(credential, for: context.scope)
    }
  #endif

  func beginSignOut(context: DatabricksWebContext, store: any DatabricksWebStoring) throws -> Task<
    Void, Error
  > {
    login.cancel()
    return try signOuts.begin(context: context, store: store)
  }

  func cancel() { login.cancel() }
}

enum DatabricksConnectionIntent {
  case connect, recover
}

#if DEBUG
  /// One broken piece of a live workspace session, offered by the debug menu. Each case names the
  /// behavior it should produce so a tester can tell a real regression from the injected fault.
  enum DatabricksDebugFault: String, CaseIterable, Identifiable, Sendable {
    case sessionCookie, accessToken, rejectedAccessToken, refreshToken

    var id: String { rawValue }

    var title: String {
      switch self {
      case .sessionCookie: "Clear Session Cookie"
      case .accessToken: "Clear Access Token"
      case .rejectedAccessToken: "Reject Access Token"
      case .refreshToken: "Clear Refresh Token"
      }
    }

    var systemImage: String {
      switch self {
      case .sessionCookie: "trash"
      case .accessToken: "key"
      case .rejectedAccessToken: "exclamationmark.shield"
      case .refreshToken: "arrow.triangle.2.circlepath"
      }
    }

    /// What the app should do next, shown after injecting so the expectation is explicit.
    var expectation: String {
      switch self {
      case .sessionCookie:
        "Cleared the session cookie. Expect a silent repair on the next page change, or after "
          + "leaving and reopening the app."
      case .accessToken:
        "Cleared the access token. Expect a silent refresh and no browser prompt."
      case .rejectedAccessToken:
        "Kept an unusable access token. Expect the workspace to reject it once, then one refresh "
          + "and retry."
      case .refreshToken:
        "Cleared both tokens. Expect a Sign In prompt instead of a silent retry."
      }
    }

    var credential: DatabricksCredentialFault? {
      switch self {
      case .sessionCookie: nil
      case .accessToken: .clearedAccessToken
      case .rejectedAccessToken: .rejectedAccessToken
      case .refreshToken: .clearedRefreshToken
      }
    }
  }

  enum DatabricksCredentialFault: Sendable {
    case clearedAccessToken, rejectedAccessToken, clearedRefreshToken
  }
#endif

struct DatabricksRecoveryPolicy {
  private var lastAttempt: Date?
  private var pageWasReady = false

  mutating func markReady() { pageWasReady = true }

  mutating func begin(now: Date = Date()) -> Bool {
    if let lastAttempt, !pageWasReady || now.timeIntervalSince(lastAttempt) < 60 { return false }
    lastAttempt = now
    pageWasReady = false
    return true
  }
}

/// Durable cleanup intent prevents interrupted sign-out from reusing old credentials on restart.
@MainActor
final class DatabricksSignOutManager {
  static let shared = DatabricksSignOutManager()
  private let tokens: DatabricksTokenManager
  private let installer: DatabricksCookieInstaller
  private let defaults: UserDefaults
  private var jobs: [UUID: (id: UUID, task: Task<Void, Error>)] = [:]
  private var generations: [UUID: UUID] = [:]
  private static let pendingKey = "omnigent.pendingWorkspaceSignOuts"

  init(
    tokens: DatabricksTokenManager = .shared, installer: DatabricksCookieInstaller? = nil,
    defaults: UserDefaults = .standard
  ) {
    self.tokens = tokens
    self.installer = installer ?? .shared
    self.defaults = defaults
  }

  func generation(_ identifier: UUID) -> UUID? { generations[identifier] }

  func isPending(_ identifier: UUID) -> Bool {
    (defaults.stringArray(forKey: Self.pendingKey) ?? []).contains(identifier.uuidString)
  }

  func begin(context: DatabricksWebContext, store: any DatabricksWebStoring) throws -> Task<
    Void, Error
  > {
    guard context.storeIdentifier == store.identifier else {
      throw DatabricksSessionError.workspaceChanged
    }
    let identifier = context.storeIdentifier
    if let job = jobs[identifier] { return job.task }
    setPending(identifier, true)
    generations[identifier] = UUID()
    let id = UUID()
    let task = Task {
      defer { if jobs[identifier]?.id == id { jobs.removeValue(forKey: identifier) } }
      do {
        try await tokens.clear(for: context.scope)
        try await installer.install([], in: store, reset: true)
        setPending(identifier, false)
      } catch {
        throw DatabricksSessionError.signOutIncomplete
      }
    }
    jobs[identifier] = (id, task)
    return task
  }

  func finishPending(context: DatabricksWebContext, store: any DatabricksWebStoring) async throws {
    guard isPending(context.storeIdentifier) else { return }
    try await begin(context: context, store: store).value
  }

  private func setPending(_ identifier: UUID, _ pending: Bool) {
    var identifiers = Set(defaults.stringArray(forKey: Self.pendingKey) ?? [])
    if pending {
      identifiers.insert(identifier.uuidString)
    } else {
      identifiers.remove(identifier.uuidString)
    }
    defaults.set(identifiers.sorted(), forKey: Self.pendingKey)
  }
}
