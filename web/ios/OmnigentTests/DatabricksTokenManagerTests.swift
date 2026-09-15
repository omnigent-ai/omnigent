import Security
import XCTest

@testable import Omnigent

final class DatabricksTokenManagerTests: XCTestCase {
  func testMissingAndUsableCredentialsNeverRefresh() async throws {
    let store = MemoryDatabricksCredentialStore()
    let client = RefreshStub()
    let manager = DatabricksTokenManager(
      store: store, client: client, now: { Date(timeIntervalSince1970: 1000) })
    let scope = try credentialScope()
    let missing = try await manager.tokens(for: scope)
    XCTAssertNil(missing)
    let valid = credentialTokens()
    try await manager.save(valid, for: scope)
    let restored = DatabricksTokenManager(
      store: store, client: client, now: { Date(timeIntervalSince1970: 1000) })
    let result = try await restored.tokens(for: scope)
    XCTAssertEqual(result, valid)
    let calls = await client.calls
    XCTAssertTrue(calls.isEmpty)
  }

  func testRefreshesAtSafetyBoundaryAndPersistsRotationBeforeReturning() async throws {
    let store = MemoryDatabricksCredentialStore()
    let scope = try credentialScope()
    try store.save(credentialTokens(expiry: 1060), for: scope)
    let rotated = credentialTokens(access: "next-access", refresh: "next-refresh")
    let client = RefreshStub(response: .success(rotated))
    let manager = DatabricksTokenManager(
      store: store, client: client, now: { Date(timeIntervalSince1970: 1000) })
    let result = try await manager.tokens(for: scope)
    XCTAssertEqual(result, rotated)
    XCTAssertEqual(store.snapshot(for: scope), rotated)
    let calls = await client.calls
    XCTAssertEqual(calls.map(\.token), ["test-refresh"])
    XCTAssertEqual(calls.map(\.scope), [scope])
  }

  func testConcurrentWaitersShareRefreshAndOneCancellationDoesNotCancelOthers() async throws {
    let requested = expectation(description: "single refresh")
    requested.assertForOverFulfill = true
    let client = RefreshStub(onRequest: { requested.fulfill() })
    let store = MemoryDatabricksCredentialStore()
    let scope = try credentialScope()
    try store.save(credentialTokens(expiry: 900), for: scope)
    let manager = DatabricksTokenManager(store: store, client: client)
    let first = Task { try await manager.tokens(for: scope) }
    await fulfillment(of: [requested], timeout: 2)
    let second = Task { try await manager.tokens(for: scope) }
    first.cancel()
    await assertCancelled(first)
    let rotated = credentialTokens(expiry: Date().timeIntervalSince1970 + 3600)
    await client.complete(0, with: .success(rotated))
    let result = try await second.value
    XCTAssertEqual(result, rotated)
    XCTAssertEqual(store.snapshot(for: scope), rotated)
    let calls = await client.calls
    XCTAssertEqual(calls.count, 1)
    let cancellationStates = await client.cancellationStates
    XCTAssertEqual(cancellationStates, [false])
  }

  func testRefreshStillPersistsAfterAllOriginalWaitersCancel() async throws {
    let requested = expectation(description: "refresh started")
    let client = RefreshStub(onRequest: { requested.fulfill() })
    let store = MemoryDatabricksCredentialStore()
    let scope = try credentialScope()
    try store.save(credentialTokens(expiry: 900), for: scope)
    let manager = DatabricksTokenManager(
      store: store, client: client, now: { Date(timeIntervalSince1970: 1000) })
    let waiter = Task { try await manager.tokens(for: scope) }
    await fulfillment(of: [requested], timeout: 2)
    waiter.cancel()
    await assertCancelled(waiter)
    let rotated = credentialTokens(refresh: "rotated-refresh")
    await client.complete(0, with: .success(rotated))
    let result = try await manager.tokens(for: scope)
    XCTAssertEqual(result, rotated)
    XCTAssertEqual(store.snapshot(for: scope), rotated)
    let calls = await client.calls
    XCTAssertEqual(calls.count, 1)
    let cancellationStates = await client.cancellationStates
    XCTAssertEqual(cancellationStates, [false])
  }

  func testDifferentWorkspacesDoNotShareRefreshOperations() async throws {
    let requested = expectation(description: "two independent refreshes")
    requested.expectedFulfillmentCount = 2
    let client = RefreshStub(onRequest: { requested.fulfill() })
    let store = MemoryDatabricksCredentialStore()
    let firstScope = try credentialScope()
    let secondScope = try credentialScope(workspace: "https://adb-123.azuredatabricks.net")
    try store.save(credentialTokens(refresh: "first", expiry: 900), for: firstScope)
    try store.save(credentialTokens(refresh: "second", expiry: 900), for: secondScope)
    let manager = DatabricksTokenManager(
      store: store, client: client, now: { Date(timeIntervalSince1970: 1000) })
    let first = Task { try await manager.tokens(for: firstScope) }
    let second = Task { try await manager.tokens(for: secondScope) }
    await fulfillment(of: [requested], timeout: 2)
    let calls = await client.calls
    for (index, call) in calls.enumerated() {
      XCTAssertEqual(call.token, call.scope == firstScope ? "first" : "second")
      await client.complete(
        index,
        with: .success(credentialTokens(access: call.token, refresh: "rotated-" + call.token)))
    }
    let firstTokens = try await first.value
    let secondTokens = try await second.value
    XCTAssertEqual(firstTokens?.accessToken, "first")
    XCTAssertEqual(secondTokens?.accessToken, "second")
  }

  func testInvalidGrantClearsOnlyMatchingScopeAndRequiresReauthentication() async throws {
    let store = MemoryDatabricksCredentialStore()
    let scope = try credentialScope()
    let other = try credentialScope(clientID: "other-client")
    let saved = credentialTokens(expiry: 900)
    try store.save(saved, for: scope)
    try store.save(saved, for: other)
    let manager = DatabricksTokenManager(
      store: store,
      client: RefreshStub(response: .failure(DatabricksOAuthError.invalidRefreshGrant)))
    let result = try await manager.tokens(for: scope)
    XCTAssertNil(result)
    XCTAssertNil(store.snapshot(for: scope))
    XCTAssertEqual(store.snapshot(for: other), saved)
  }

  func testTransientErrorsAndMalformedResponsesRetainCredentials() async throws {
    for error in [
      DatabricksOAuthError.networkUnavailable, .tokenExchangeFailed, .invalidTokenResponse,
    ] {
      let store = MemoryDatabricksCredentialStore()
      let scope = try credentialScope()
      let saved = credentialTokens(expiry: 900)
      try store.save(saved, for: scope)
      let manager = DatabricksTokenManager(
        store: store, client: RefreshStub(response: .failure(error)))
      do {
        _ = try await manager.tokens(for: scope)
        XCTFail("Expected refresh failure")
      } catch let actual {
        XCTAssertEqual(actual as? DatabricksOAuthError, error)
      }
      XCTAssertEqual(store.snapshot(for: scope), saved)
    }
  }

  func testLockedKeychainDoesNotBecomeMissingCredentials() async throws {
    let store = MemoryDatabricksCredentialStore()
    store.fail(.load)
    let client = RefreshStub()
    let manager = DatabricksTokenManager(store: store, client: client)
    do {
      _ = try await manager.tokens(for: credentialScope())
      XCTFail("Expected Keychain failure")
    } catch {
      XCTAssertEqual(error as? DatabricksCredentialError, .keychain(errSecInteractionNotAllowed))
    }
    let calls = await client.calls
    XCTAssertTrue(calls.isEmpty)
  }

  func testFailedRotationWriteRetriesPersistenceWithoutReusingOldGrant() async throws {
    let store = MemoryDatabricksCredentialStore()
    let scope = try credentialScope()
    let issuer = try DatabricksOAuthIssuer(
      URL(string: "https://accounts.cloud.databricks.com/oidc/accounts/test-account")!)
    let saved = credentialTokens(expiry: 900, issuer: issuer)
    try store.save(saved, for: scope)
    store.fail(.save)
    let rotated = credentialTokens(
      access: "rotated-access", refresh: "rotated-refresh", issuer: issuer)
    let client = RefreshStub(response: .success(rotated))
    let manager = DatabricksTokenManager(
      store: store, client: client, now: { Date(timeIntervalSince1970: 1000) })
    do {
      _ = try await manager.tokens(for: scope)
      XCTFail("Expected persistence failure")
    } catch {
      XCTAssertEqual(error as? DatabricksCredentialError, .keychain(errSecInteractionNotAllowed))
    }
    XCTAssertEqual(store.snapshot(for: scope), saved)
    store.fail(nil)
    let result = try await manager.tokens(for: scope)
    XCTAssertEqual(result, rotated)
    XCTAssertEqual(store.snapshot(for: scope), rotated)
    let calls = await client.calls
    XCTAssertEqual(calls.count, 1)
    XCTAssertEqual(calls.first?.issuer, issuer)
  }

  func testRejectsRefreshThatDropsIssuerContext() async throws {
    let store = MemoryDatabricksCredentialStore()
    let scope = try credentialScope()
    let issuer = try DatabricksOAuthIssuer(
      URL(string: "https://workspace.cloud.databricks.com/oidc")!)
    let saved = credentialTokens(expiry: 900, issuer: issuer)
    try store.save(saved, for: scope)
    let manager = DatabricksTokenManager(
      store: store, client: RefreshStub(response: .success(credentialTokens())))
    do {
      _ = try await manager.tokens(for: scope)
      XCTFail("Expected issuer mismatch")
    } catch {
      XCTAssertEqual(error as? DatabricksOAuthError, .invalidTokenResponse)
    }
    XCTAssertEqual(store.snapshot(for: scope), saved)
  }

  func testSameOriginWorkspaceContextsKeepTheirOwnTokens() async throws {
    let first = try credentialScope(workspace: "https://workspace.databricks.com/omnigent?o=123")
    let second = try credentialScope(workspace: "https://workspace.databricks.com/omnigent?o=456")
    let store = MemoryDatabricksCredentialStore()
    let firstTokens = credentialTokens(access: "first")
    let secondTokens = credentialTokens(access: "second")
    let manager = DatabricksTokenManager(
      store: store, client: RefreshStub(), now: { Date(timeIntervalSince1970: 1000) })
    try await manager.save(firstTokens, for: first)
    try await manager.save(secondTokens, for: second)
    let loadedFirst = try await manager.tokens(for: first)
    let loadedSecond = try await manager.tokens(for: second)
    XCTAssertEqual(loadedFirst, firstTokens)
    XCTAssertEqual(loadedSecond, secondTokens)
    try await manager.clear(for: first)
    XCTAssertNil(store.snapshot(for: first))
    XCTAssertEqual(store.snapshot(for: second), secondTokens)
  }

  func testFailedClearDoesNotAllowOldCredentialsToReappear() async throws {
    let store = MemoryDatabricksCredentialStore()
    let scope = try credentialScope()
    try store.save(credentialTokens(), for: scope)
    store.fail(.delete)
    let client = RefreshStub()
    let manager = DatabricksTokenManager(store: store, client: client)
    do {
      try await manager.clear(for: scope)
      XCTFail("Expected delete failure")
    } catch {
      XCTAssertEqual(error as? DatabricksCredentialError, .keychain(errSecInteractionNotAllowed))
    }
    store.fail(nil)
    let result = try await manager.tokens(for: scope)
    XCTAssertNil(result)
    XCTAssertNil(store.snapshot(for: scope))
    let calls = await client.calls
    XCTAssertTrue(calls.isEmpty)
  }

  func testClearAndNewLoginRejectLateRefreshResults() async throws {
    let oldIssuer = try DatabricksOAuthIssuer(
      URL(string: "https://accounts.cloud.databricks.com/oidc/accounts/old-account")!)
    let newIssuer = try DatabricksOAuthIssuer(
      URL(string: "https://accounts.cloud.databricks.com/oidc/accounts/new-account")!)
    for replaceWithLogin in [false, true] {
      for remoteResult: Result<DatabricksOAuthTokens, Error> in [
        .success(credentialTokens(access: "old-result", issuer: oldIssuer)),
        .failure(DatabricksOAuthError.invalidRefreshGrant),
      ] {
        let requested = expectation(description: "refresh started")
        let client = RefreshStub(onRequest: { requested.fulfill() })
        let store = MemoryDatabricksCredentialStore()
        let scope = try credentialScope()
        try store.save(credentialTokens(expiry: 900, issuer: oldIssuer), for: scope)
        let manager = DatabricksTokenManager(
          store: store, client: client, now: { Date(timeIntervalSince1970: 1000) })
        let waiter = Task { try await manager.tokens(for: scope) }
        await fulfillment(of: [requested], timeout: 2)
        let newer = credentialTokens(
          access: "new-login", refresh: "new-login-refresh", issuer: newIssuer)
        if replaceWithLogin {
          try await manager.save(newer, for: scope)
        } else {
          try await manager.clear(for: scope)
        }
        await assertCancelled(waiter)
        await client.complete(0, with: remoteResult)
        let result = try await manager.tokens(for: scope)
        XCTAssertEqual(result, replaceWithLogin ? newer : nil)
        XCTAssertEqual(store.snapshot(for: scope), replaceWithLogin ? newer : nil)
      }
    }
  }

  private func assertCancelled(_ task: Task<DatabricksOAuthTokens?, Error>) async {
    do {
      _ = try await task.value
      XCTFail("Expected cancellation")
    } catch { XCTAssertTrue(error is CancellationError) }
  }
}

private actor RefreshStub: DatabricksTokenRefreshing {
  struct Call: Sendable {
    let token: String
    let scope: DatabricksCredentialScope
    let issuer: DatabricksOAuthIssuer?
  }
  private(set) var calls: [Call] = []
  private(set) var cancellationStates: [Bool] = []
  private let response: Result<DatabricksOAuthTokens, Error>?
  private let onRequest: (@Sendable () -> Void)?
  private var pending: [Int: CheckedContinuation<DatabricksOAuthTokens, Error>] = [:]

  init(
    response: Result<DatabricksOAuthTokens, Error> = .failure(
      DatabricksOAuthError.networkUnavailable)
  ) {
    self.response = response
    onRequest = nil
  }

  init(onRequest: @escaping @Sendable () -> Void) {
    response = nil
    self.onRequest = onRequest
  }

  func refresh(
    _ refreshToken: String, for scope: DatabricksCredentialScope, issuer: DatabricksOAuthIssuer?
  ) async throws
    -> DatabricksOAuthTokens
  {
    let index = calls.count
    calls.append(Call(token: refreshToken, scope: scope, issuer: issuer))
    if let response { return try response.get() }
    let tokens: DatabricksOAuthTokens = try await withCheckedThrowingContinuation { continuation in
      pending[index] = continuation
      onRequest?()
    }
    cancellationStates.append(Task.isCancelled)
    return tokens
  }

  // Deliberately allow completion after cancellation to model a late provider response.
  func complete(_ index: Int, with result: Result<DatabricksOAuthTokens, Error>) {
    pending.removeValue(forKey: index)?.resume(with: result)
  }
}
