import Foundation

/// Shared per process so callers cannot race a rotating refresh token for the same workspace/client.
actor DatabricksTokenManager {
  static let shared = DatabricksTokenManager()

  private let store: any DatabricksCredentialStoring
  private let client: any DatabricksTokenRefreshing
  private let now: @Sendable () -> Date
  private var refreshes: [DatabricksCredentialScope: Refresh] = [:]
  private var generations: [DatabricksCredentialScope: UUID] = [:]
  private var signIns: [DatabricksCredentialScope: UUID] = [:]
  // Keep issued rotations until durable; never retry a consumed grant after a failed Keychain write.
  private var pendingWrites: [DatabricksCredentialScope: Write] = [:]

  init(
    store: any DatabricksCredentialStoring = DatabricksCredentialStore(),
    client: any DatabricksTokenRefreshing = DatabricksOAuthClient(),
    now: @escaping @Sendable () -> Date = { Date() }
  ) {
    self.store = store
    self.client = client
    self.now = now
  }

  struct SignIn: Sendable {
    fileprivate let scope: DatabricksCredentialScope
    fileprivate let id: UUID
  }

  func beginSignIn(for scope: DatabricksCredentialScope) throws -> SignIn {
    try Task.checkCancellation()
    let id = UUID()
    signIns[scope] = id
    return SignIn(scope: scope, id: id)
  }

  func endSignIn(_ signIn: SignIn) {
    if signIns[signIn.scope] == signIn.id { signIns.removeValue(forKey: signIn.scope) }
  }

  func save(_ tokens: DatabricksOAuthTokens, for signIn: SignIn) throws {
    guard signIns[signIn.scope] == signIn.id else { throw CancellationError() }
    try save(tokens, for: signIn.scope)
  }

  func save(_ tokens: DatabricksOAuthTokens, for scope: DatabricksCredentialScope) throws {
    try Task.checkCancellation()
    guard tokens.isValid else { throw DatabricksCredentialError.invalidData }
    signIns.removeValue(forKey: scope)
    invalidateRefresh(for: scope)
    pendingWrites[scope] = .save(tokens)
    try flushPendingWrite(for: scope)
  }

  func clear(for scope: DatabricksCredentialScope) throws {
    try Task.checkCancellation()
    signIns.removeValue(forKey: scope)
    invalidateRefresh(for: scope)
    pendingWrites[scope] = .delete
    try flushPendingWrite(for: scope)
  }

  func isCurrent(_ tokens: DatabricksOAuthTokens, for scope: DatabricksCredentialScope) throws
    -> Bool
  {
    try Task.checkCancellation()
    guard pendingWrites[scope] == nil else { return false }
    return try store.load(for: scope) == tokens
  }

  /// Nil means no reusable grant. Network, configuration, and Keychain failures remain errors.
  func tokens(for scope: DatabricksCredentialScope) async throws -> DatabricksOAuthTokens? {
    try Task.checkCancellation()
    try flushPendingWrite(for: scope)
    if refreshes[scope] == nil {
      guard let saved = try store.load(for: scope) else { return nil }
      guard saved.isValid else { throw DatabricksCredentialError.invalidData }
      if saved.expiresAt > now().addingTimeInterval(60) { return saved }
      startRefresh(saved, for: scope)
    }
    let refreshID = refreshes[scope]!.id
    let generation = generations[scope]
    let waiterID = UUID()
    let tokens = try await withTaskCancellationHandler {
      try Task.checkCancellation()
      return try await withCheckedThrowingContinuation { continuation in
        refreshes[scope]?.waiters[waiterID] = continuation
      }
    } onCancel: {
      Task { await self.cancelWaiter(waiterID, refreshID: refreshID, for: scope) }
    }
    try Task.checkCancellation()
    guard generations[scope] == generation else { throw CancellationError() }
    return tokens
  }

  private func startRefresh(_ saved: DatabricksOAuthTokens, for scope: DatabricksCredentialScope) {
    let id = UUID()
    let task = Task {
      do {
        let tokens = try await client.refresh(saved.refreshToken, for: scope, issuer: saved.issuer)
        guard tokens.issuer == saved.issuer else { throw DatabricksOAuthError.invalidTokenResponse }
        finishRefresh(id, for: scope, result: .success(tokens))
      } catch {
        finishRefresh(id, for: scope, result: .failure(error))
      }
    }
    refreshes[scope] = Refresh(id: id, task: task)
  }

  private func finishRefresh(
    _ id: UUID, for scope: DatabricksCredentialScope, result: Result<DatabricksOAuthTokens, Error>
  ) {
    guard let refresh = refreshes[scope], refresh.id == id else { return }
    let completion: Result<DatabricksOAuthTokens?, Error>
    do {
      switch result {
      case .success(let tokens):
        pendingWrites[scope] = .save(tokens)
        try flushPendingWrite(for: scope)
        completion = .success(tokens)
      case .failure(let error) where error as? DatabricksOAuthError == .invalidRefreshGrant:
        pendingWrites[scope] = .delete
        try flushPendingWrite(for: scope)
        completion = .success(nil)
      case .failure(let error):
        completion = .failure(error)
      }
    } catch {
      completion = .failure(error)
    }
    refreshes.removeValue(forKey: scope)
    for continuation in refresh.waiters.values {
      continuation.resume(with: completion)
    }
  }

  private func flushPendingWrite(for scope: DatabricksCredentialScope) throws {
    guard let write = pendingWrites[scope] else { return }
    switch write {
    case .save(let tokens): try store.save(tokens, for: scope)
    case .delete: try store.delete(for: scope)
    }
    pendingWrites.removeValue(forKey: scope)
  }

  private func invalidateRefresh(for scope: DatabricksCredentialScope) {
    generations[scope] = UUID()
    guard let refresh = refreshes.removeValue(forKey: scope) else { return }
    refresh.task.cancel()
    for continuation in refresh.waiters.values {
      continuation.resume(throwing: CancellationError())
    }
  }

  private func cancelWaiter(_ waiterID: UUID, refreshID: UUID, for scope: DatabricksCredentialScope)
  {
    guard refreshes[scope]?.id == refreshID else { return }
    let continuation = refreshes[scope]?.waiters.removeValue(forKey: waiterID)
    continuation?.resume(throwing: CancellationError())
    // Finish an issued refresh even without waiters so a rotated grant can still be saved.
  }

  private enum Write {
    case save(DatabricksOAuthTokens)
    case delete
  }

  private struct Refresh {
    let id: UUID
    let task: Task<Void, Never>
    var waiters: [UUID: CheckedContinuation<DatabricksOAuthTokens?, Error>] = [:]
  }
}
