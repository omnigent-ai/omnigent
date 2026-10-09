import AuthenticationServices
import UIKit
import XCTest

@testable import Omnigent

@MainActor
final class OidcCredentialsTests: XCTestCase {
  private static let now = Date(timeIntervalSince1970: 1_700_000_000)

  // MARK: Sign-in

  func testSignInUsesMountedLoginAndExchangesWithPKCE() async throws {
    let harness = try OidcHarness()
    harness.server.respond(
      "/omnigent/auth/native-token",
      json: ["token": "session-1", "user_id": "u1", "expires_in": 3600, "refresh_token": "r1"])
    let (task, browser) = try await harness.startSignIn()

    let login = try XCTUnwrap(URLComponents(url: browser.url, resolvingAgainstBaseURL: false))
    XCTAssertEqual(login.scheme, "http")
    XCTAssertEqual(login.host, "localhost")
    XCTAssertEqual(login.path, "/omnigent/auth/login")
    let query = Dictionary(
      uniqueKeysWithValues: (login.queryItems ?? []).map { ($0.name, $0.value) })
    XCTAssertEqual(
      Set(query.keys),
      ["native_redirect_uri", "native_state", "code_challenge", "code_challenge_method"])
    XCTAssertEqual(query["native_redirect_uri"], "ai.omnigent.ios:/oauth/callback")
    XCTAssertEqual(query["code_challenge_method"], "S256")
    let state = try XCTUnwrap(query["native_state"] ?? nil)
    XCTAssertNotNil(state.range(of: "^[A-Za-z0-9._~-]{1,256}$", options: .regularExpression))
    let challenge = try XCTUnwrap(query["code_challenge"] ?? nil)
    XCTAssertEqual(challenge.count, 43)
    XCTAssertTrue(browser.callback.matchesURL(URL(string: "ai.omnigent.ios:/oauth/callback")!))
    XCTAssertFalse(browser.callback.matchesURL(URL(string: "omnigent:/oauth/callback")!))

    browser.complete([("code", "the-code"), ("state", state)])
    let minted = try await task.value
    XCTAssertEqual(
      minted, OidcSessionToken(token: "session-1", expiresAt: Self.now.addingTimeInterval(3600)))

    let exchange = try XCTUnwrap(harness.server.requests.last)
    XCTAssertEqual(exchange.method, "POST")
    XCTAssertEqual(exchange.path, "/omnigent/auth/native-token")
    XCTAssertEqual(exchange.headers["content-type"], "application/x-www-form-urlencoded")
    let form = OidcHarness.form(exchange)
    XCTAssertEqual(form["code"], "the-code")
    XCTAssertEqual(form["redirect_uri"], "ai.omnigent.ios:/oauth/callback")
    XCTAssertEqual(OAuthSupport.challenge(for: try XCTUnwrap(form["code_verifier"])), challenge)
    XCTAssertEqual(
      harness.store.grant(for: harness.origin), OidcRefreshGrant(refreshToken: "r1", userID: "u1"))
    XCTAssertTrue(harness.credentials.hasStoredGrant(serverURL: harness.serverURL))
  }

  func testInvalidCallbacksAndCancellationNeverExchange() async throws {
    let harness = try OidcHarness()
    harness.server.respond("/omnigent/auth/native-token", json: ["token": "unexpected"])
    let host = harness.host
    let cases: [(String, (FakeOidcAuthenticationSession) -> Void, OidcSignInError?)] = [
      (
        "state mismatch", { $0.complete([("code", "c"), ("state", "other")]) },
        .invalidCallback(host: host)
      ),
      ("missing state", { $0.complete([("code", "c")]) }, .invalidCallback(host: host)),
      ("missing code", { $0.complete([("state", $0.state)]) }, .invalidCallback(host: host)),
      (
        "empty code", { $0.complete([("code", ""), ("state", $0.state)]) },
        .invalidCallback(host: host)
      ),
      (
        "wrong path", { $0.complete([("code", "c"), ("state", $0.state)], path: "/other") },
        .invalidCallback(host: host)
      ),
      (
        "server error",
        {
          $0.complete([
            ("error", "access_denied"), ("error_description", "example.org is not allowed"),
            ("state", $0.state),
          ])
        }, .signInRefused(host: host, description: "example.org is not allowed")
      ),
      (
        "error without state", { $0.complete([("error", "access_denied")]) },
        .invalidCallback(host: host)
      ),
      (
        "user cancelled",
        {
          $0.completion(
            nil,
            NSError(
              domain: ASWebAuthenticationSessionErrorDomain,
              code: ASWebAuthenticationSessionError.Code.canceledLogin.rawValue))
        }, nil
      ),
      (
        "sheet failed", { $0.completion(nil, NSError(domain: "test", code: 1)) },
        .browserUnavailable(host: host)
      ),
      ("no callback", { $0.completion(nil, nil) }, .invalidCallback(host: host)),
    ]
    for (name, finish, expected) in cases {
      let (task, browser) = try await harness.startSignIn()
      finish(browser)
      do {
        _ = try await task.value
        XCTFail("\(name): expected failure")
      } catch {
        if let expected {
          XCTAssertEqual(error as? OidcSignInError, expected, name)
        } else {
          XCTAssertTrue(error is CancellationError, "\(name): \(error)")
        }
      }
    }
    XCTAssertTrue(harness.server.requests.isEmpty)
    XCTAssertEqual(
      OidcSignInError.signInRefused(host: host, description: nil).errorDescription,
      "\(host) didn't accept the sign-in. Try again.")
  }

  func testCallbackQueryDecodesAsAFormLikeTheServerEncodesIt() throws {
    let host = "omnigent.example.com"
    let state = "s-1_.~AbC"
    func callback(_ query: String) -> URL {
      URL(string: "ai.omnigent.ios:/oauth/callback?" + query)!
    }
    do {
      _ = try OidcCredentials.authorizationCode(
        from: callback(
          "error=access_denied&error_description=Email+domain+%27other.test%27+is%20not%2Ballowed"
            + "&state=\(state)"), state: state, host: host)
      XCTFail("Expected the refusal")
    } catch {
      XCTAssertEqual(
        error as? OidcSignInError,
        .signInRefused(host: host, description: "Email domain 'other.test' is not+allowed"))
    }
    XCTAssertEqual(
      try OidcCredentials.authorizationCode(
        from: callback("code=abc-_.~%2Bdef&state=\(state)"), state: state, host: host),
      "abc-_.~+def")
    // A literal '+' in state means a space, so it no longer matches.
    XCTAssertThrowsError(
      try OidcCredentials.authorizationCode(
        from: callback("code=c&state=a+b"), state: "a+b", host: host))
    XCTAssertEqual(
      try OidcCredentials.authorizationCode(
        from: callback("code=c&state=a%2Bb"), state: "a+b", host: host), "c")
    XCTAssertThrowsError(
      try OidcCredentials.authorizationCode(
        from: callback("code=c&state=\(state)&state=\(state)"), state: state, host: host))
    XCTAssertNil(OidcCredentials.formItems("code=%zz"))
    XCTAssertEqual(
      OidcCredentials.formItems("a=1+2&&b&c=%2B"),
      [
        URLQueryItem(name: "a", value: "1 2"), URLQueryItem(name: "b", value: nil),
        URLQueryItem(name: "c", value: "+"),
      ])
  }

  func testCancellingSignInCancelsSheet() async throws {
    let harness = try OidcHarness()
    let (task, browser) = try await harness.startSignIn()
    task.cancel()
    do {
      _ = try await task.value
      XCTFail("Expected cancellation")
    } catch {
      XCTAssertTrue(error is CancellationError, "\(error)")
    }
    XCTAssertEqual(browser.cancelCount, 1)
    XCTAssertTrue(harness.server.requests.isEmpty)
  }

  func testSheetStartFailureIsBrowserUnavailable() async throws {
    let harness = try OidcHarness()
    harness.startResult = false
    do {
      _ = try await harness.credentials.signIn(serverURL: harness.serverURL, anchor: harness.anchor)
      XCTFail("Expected start failure")
    } catch {
      XCTAssertEqual(error as? OidcSignInError, .browserUnavailable(host: harness.host))
    }
  }

  func testExchangeWithoutRefreshTokenForgetsStaleGrant() async throws {
    let harness = try OidcHarness()
    harness.store.set(OidcRefreshGrant(refreshToken: "stale", userID: nil), for: harness.origin)
    harness.server.respond(
      "/omnigent/auth/native-token", json: ["token": "session-1", "user_id": "u1"])
    let (task, browser) = try await harness.startSignIn()
    browser.complete([("code", "c"), ("state", browser.state)])
    let minted = try await task.value
    XCTAssertEqual(minted, OidcSessionToken(token: "session-1", expiresAt: nil))
    XCTAssertNil(harness.store.grant(for: harness.origin))
    XCTAssertFalse(harness.credentials.hasStoredGrant(serverURL: harness.serverURL))
  }

  func testOversizedExpiresInIsCapped() async throws {
    let harness = try OidcHarness()
    harness.server.respond(
      "/omnigent/auth/native-token", json: ["token": "session-1", "expires_in": 20_000_000_000])
    let (task, browser) = try await harness.startSignIn()
    browser.complete([("code", "c"), ("state", browser.state)])
    let minted = try await task.value
    XCTAssertEqual(
      minted,
      OidcSessionToken(
        token: "session-1",
        expiresAt: Self.now.addingTimeInterval(OidcCredentials.maxSessionLifetime)))
  }

  func testExchangeWithoutRefreshTokenSucceedsWhenTheStaleGrantCannotBeDeleted() async throws {
    let harness = try OidcHarness()
    harness.store.set(OidcRefreshGrant(refreshToken: "stale", userID: nil), for: harness.origin)
    harness.store.deleteError = OidcCredentialStoreError.keychain(errSecInteractionNotAllowed)
    harness.server.respond("/omnigent/auth/native-token", json: ["token": "session-1"])
    let (task, browser) = try await harness.startSignIn()
    browser.complete([("code", "c"), ("state", browser.state)])
    let minted = try await task.value
    XCTAssertEqual(minted, OidcSessionToken(token: "session-1", expiresAt: nil))
  }

  func testUnsafeSessionTokensAreRefused() async throws {
    let harness = try OidcHarness()
    let grant = OidcRefreshGrant(refreshToken: "r1", userID: nil)
    for token in OidcHarness.unsafeTokens {
      harness.store.set(nil, for: harness.origin)
      harness.server.respond(
        "/omnigent/auth/native-token", json: ["token": token, "refresh_token": "r1"])
      let (task, browser) = try await harness.startSignIn()
      browser.complete([("code", "c"), ("state", browser.state)])
      do {
        _ = try await task.value
        XCTFail("Expected \(token.debugDescription) to be refused")
      } catch {
        XCTAssertEqual(
          error as? OidcSignInError, .exchangeFailed(host: harness.host),
          token.debugDescription)
      }
      XCTAssertNil(harness.store.grant(for: harness.origin), token.debugDescription)

      harness.store.set(grant, for: harness.origin)
      harness.server.respond("/omnigent/oauth/token", json: ["access_token": token])
      do {
        _ = try await harness.credentials.refresh(serverURL: harness.serverURL)
        XCTFail("Expected \(token.debugDescription) to be refused")
      } catch {
        XCTAssertEqual(error as? OidcSignInError, .network(host: harness.host))
      }
      XCTAssertEqual(harness.store.grant(for: harness.origin), grant, token.debugDescription)
    }
    XCTAssertTrue(
      OidcSessionToken.isCookieSafe("eyJhbGciOiJIUzI1NiJ9.e30.sig-_~!#$%&'()*+-./:<=>?@[]^`{|}"))
  }

  func testServerErrorDuringExchangeIsAnExchangeFailure() async throws {
    let harness = try OidcHarness()
    harness.server.respond("/omnigent/auth/native-token", status: 500, json: [:])
    let (task, browser) = try await harness.startSignIn()
    browser.complete([("code", "c"), ("state", browser.state)])
    do {
      _ = try await task.value
      XCTFail("Expected exchange failure")
    } catch {
      XCTAssertEqual(error as? OidcSignInError, .exchangeFailed(host: harness.host))
      XCTAssertEqual(
        error.localizedDescription, "Couldn't finish signing in to \(harness.host). Try again.")
    }
  }

  func testRejectedExchangeFailsAndKeepsStoredGrant() async throws {
    let harness = try OidcHarness()
    let grant = OidcRefreshGrant(refreshToken: "kept", userID: nil)
    harness.store.set(grant, for: harness.origin)
    harness.server.respond(
      "/omnigent/auth/native-token", status: 400, json: ["error": "invalid_grant"])
    let (task, browser) = try await harness.startSignIn()
    browser.complete([("code", "c"), ("state", browser.state)])
    do {
      _ = try await task.value
      XCTFail("Expected exchange failure")
    } catch {
      XCTAssertEqual(
        error as? OidcSignInError, .exchangeFailed(host: harness.host))
    }
    XCTAssertEqual(harness.server.requests.count, 1)
    XCTAssertEqual(harness.store.grant(for: harness.origin), grant)
  }

  func testCancelDuringExchangeStillStoresGrant() async throws {
    let harness = try OidcHarness()
    harness.server.respond(
      "/omnigent/auth/native-token", json: ["token": "session-1", "refresh_token": "r1"])
    harness.server.delay = 300_000
    let (task, browser) = try await harness.startSignIn()
    browser.complete([("code", "c"), ("state", browser.state)])
    try await harness.waitForRequest("/omnigent/auth/native-token")
    task.cancel()
    do {
      _ = try await task.value
      XCTFail("Expected cancellation")
    } catch {
      XCTAssertTrue(error is CancellationError, "\(error)")
    }
    XCTAssertEqual(
      harness.store.grant(for: harness.origin), OidcRefreshGrant(refreshToken: "r1", userID: nil))
  }

  func testUnsavableGrantIsRevoked() async throws {
    let harness = try OidcHarness()
    harness.store.saveError = OidcCredentialStoreError.keychain(errSecInteractionNotAllowed)
    harness.server.respond(
      "/omnigent/auth/native-token", json: ["token": "session-1", "refresh_token": "r1"])
    harness.server.respond("/omnigent/oauth/revoke", json: [:])
    let (task, browser) = try await harness.startSignIn()
    browser.complete([("code", "c"), ("state", browser.state)])
    do {
      _ = try await task.value
      XCTFail("Expected the Keychain failure")
    } catch {
      XCTAssertEqual(
        error as? OidcCredentialStoreError, .keychain(errSecInteractionNotAllowed))
    }
    let revoke = try await harness.waitForRequest("/omnigent/oauth/revoke")
    XCTAssertEqual(OidcHarness.form(revoke), ["refresh_token": "r1"])
    XCTAssertNil(harness.store.grant(for: harness.origin))
  }

  func testExchangeDoesNotFollowRedirects() async throws {
    let harness = try OidcHarness()
    harness.server.respond(
      "/omnigent/auth/native-token", status: 302, headers: ["location": "/omnigent/leak"],
      json: [:])
    harness.server.respond("/omnigent/leak", json: ["token": "leaked", "refresh_token": "r9"])
    let (task, browser) = try await harness.startSignIn()
    browser.complete([("code", "c"), ("state", browser.state)])
    do {
      _ = try await task.value
      XCTFail("Expected the redirect to be refused")
    } catch {
      XCTAssertEqual(
        error as? OidcSignInError, .exchangeFailed(host: harness.host))
    }
    XCTAssertEqual(harness.server.requests.map(\.path), ["/omnigent/auth/native-token"])
    XCTAssertNil(harness.store.grant(for: harness.origin))
  }

  // MARK: Refresh

  func testRefreshDoesNotFollowRedirects() async throws {
    let harness = try OidcHarness()
    let grant = OidcRefreshGrant(refreshToken: "r1", userID: nil)
    harness.store.set(grant, for: harness.origin)
    harness.server.respond(
      "/omnigent/oauth/token", status: 302, headers: ["location": "/omnigent/leak"], json: [:])
    harness.server.respond("/omnigent/leak", json: ["access_token": "leaked"])
    do {
      _ = try await harness.credentials.refresh(serverURL: harness.serverURL)
      XCTFail("Expected the redirect to be refused")
    } catch {
      XCTAssertEqual(error as? OidcSignInError, .network(host: harness.host))
    }
    XCTAssertEqual(harness.server.requests.map(\.path), ["/omnigent/oauth/token"])
    XCTAssertEqual(harness.store.grant(for: harness.origin), grant)
  }

  func testRefreshMintsFromStoredGrant() async throws {
    let harness = try OidcHarness()
    let grant = OidcRefreshGrant(refreshToken: "r1", userID: "u1")
    harness.store.set(grant, for: harness.origin)
    harness.server.respond(
      "/omnigent/oauth/token",
      json: ["access_token": "session-2", "refresh_token": "r1", "expires_in": 600])
    let minted = try await harness.credentials.refresh(serverURL: harness.serverURL)
    XCTAssertEqual(
      minted, OidcSessionToken(token: "session-2", expiresAt: Self.now.addingTimeInterval(600)))
    let request = try XCTUnwrap(harness.server.requests.first)
    XCTAssertEqual(request.method, "POST")
    XCTAssertEqual(request.path, "/omnigent/oauth/token")
    XCTAssertEqual(
      OidcHarness.form(request), ["grant_type": "refresh_token", "refresh_token": "r1"])
    XCTAssertEqual(harness.store.grant(for: harness.origin), grant)
  }

  func testDeadGrantsAreForgotten() async throws {
    let harness = try OidcHarness()
    let host = harness.host
    let cases: [(Int, [String: Any], OidcSignInError)] = [
      (400, ["error": "invalid_grant"], .grantRejected(host: host)),
      (400, ["error": "expired_token"], .grantExpired(host: host)),
      (404, ["detail": "Not Found"], .noStoredGrant(host: host)),
    ]
    for (status, body, expected) in cases {
      harness.store.set(OidcRefreshGrant(refreshToken: "r1", userID: nil), for: harness.origin)
      harness.server.respond("/omnigent/oauth/token", status: status, json: body)
      do {
        _ = try await harness.credentials.refresh(serverURL: harness.serverURL)
        XCTFail("Expected \(expected)")
      } catch {
        XCTAssertEqual(error as? OidcSignInError, expected)
      }
      XCTAssertNil(harness.store.grant(for: harness.origin), "\(expected)")
    }
    XCTAssertEqual(
      OidcSignInError.grantExpired(host: host).errorDescription,
      "Your sign-in to \(host) has expired. Sign in again to continue.")
    XCTAssertEqual(
      OidcSignInError.grantRejected(host: host).errorDescription,
      "\(host) ended your session. Sign in again to continue.")
    XCTAssertEqual(
      OidcSignInError.noStoredGrant(host: host).errorDescription,
      "Sign in to \(host) to continue.")
  }

  func testServerAndTransportFailuresKeepGrant() async throws {
    let harness = try OidcHarness()
    let grant = OidcRefreshGrant(refreshToken: "r1", userID: nil)
    harness.store.set(grant, for: harness.origin)
    for (status, body) in [(500, ["error": "server_error"]), (400, ["error": "invalid_request"])] {
      harness.server.respond("/omnigent/oauth/token", status: status, json: body)
      do {
        _ = try await harness.credentials.refresh(serverURL: harness.serverURL)
        XCTFail("Expected network failure")
      } catch {
        XCTAssertEqual(error as? OidcSignInError, .network(host: harness.host))
      }
      XCTAssertEqual(harness.store.grant(for: harness.origin), grant)
    }

    let unreachable = URL(string: "http://localhost:1/omnigent/")!
    let unreachableOrigin = try XCTUnwrap(unreachable.omnigentOrigin)
    harness.store.set(grant, for: unreachableOrigin)
    do {
      _ = try await harness.credentials.refresh(serverURL: unreachable)
      XCTFail("Expected network failure")
    } catch {
      let error = try XCTUnwrap(error as? OidcSignInError)
      XCTAssertEqual(error, .network(host: "localhost:1"))
      XCTAssertFalse(error.localizedDescription.lowercased().contains("expired"))
    }
    XCTAssertEqual(harness.store.grant(for: unreachableOrigin), grant)
  }

  func testRefreshWithoutGrantMakesNoRequest() async throws {
    let harness = try OidcHarness()
    do {
      _ = try await harness.credentials.refresh(serverURL: harness.serverURL)
      XCTFail("Expected no stored grant")
    } catch {
      XCTAssertEqual(error as? OidcSignInError, .noStoredGrant(host: harness.host))
    }
    XCTAssertTrue(harness.server.requests.isEmpty)
  }

  func testConcurrentRefreshesShareOneRequest() async throws {
    let harness = try OidcHarness()
    harness.store.set(OidcRefreshGrant(refreshToken: "r1", userID: nil), for: harness.origin)
    harness.server.respond("/omnigent/oauth/token", json: ["access_token": "session-2"])
    harness.server.delay = 300_000
    let credentials = harness.credentials
    let serverURL = harness.serverURL
    let first = Task { try await credentials.refresh(serverURL: serverURL) }
    // A different spelling of the same origin shares the flight.
    let second = Task {
      try await credentials.refresh(serverURL: URL(string: "http://LOCALHOST:\(harness.port)/")!)
    }
    let tokens = try await [first.value, second.value]
    XCTAssertEqual(tokens.map(\.token), ["session-2", "session-2"])
    XCTAssertEqual(harness.server.requests.count, 1)

    harness.server.delay = 0
    _ = try await credentials.refresh(serverURL: serverURL)
    XCTAssertEqual(harness.server.requests.count, 2)
  }

  // MARK: Sign-out

  func testSignOutForgetsGrantBeforeRevoking() async throws {
    let harness = try OidcHarness()
    harness.store.set(OidcRefreshGrant(refreshToken: "r1", userID: nil), for: harness.origin)
    harness.server.respond("/omnigent/oauth/revoke", json: [:])
    let revoke = try harness.credentials.signOut(serverURL: harness.serverURL)
    XCTAssertNil(harness.store.grant(for: harness.origin))
    XCTAssertTrue(harness.server.requests.isEmpty)
    await revoke.value
    let request = try XCTUnwrap(harness.server.requests.first)
    XCTAssertEqual(request.method, "POST")
    XCTAssertEqual(request.path, "/omnigent/oauth/revoke")
    XCTAssertEqual(OidcHarness.form(request), ["refresh_token": "r1"])
  }

  func testSignOutDropsRefreshInFlight() async throws {
    let harness = try OidcHarness()
    harness.store.set(OidcRefreshGrant(refreshToken: "r1", userID: nil), for: harness.origin)
    harness.server.respond("/omnigent/oauth/token", json: ["access_token": "session-2"])
    harness.server.respond("/omnigent/oauth/revoke", json: [:])
    harness.server.delay = 300_000
    let credentials = harness.credentials
    let serverURL = harness.serverURL
    let inFlight = Task { try await credentials.refresh(serverURL: serverURL) }
    try await harness.waitForRequest("/omnigent/oauth/token")

    let revoke = try credentials.signOut(serverURL: serverURL)
    // A refresh after sign-out starts fresh and finds no grant, rather than joining.
    do {
      _ = try await credentials.refresh(serverURL: serverURL)
      XCTFail("Expected no stored grant")
    } catch {
      XCTAssertEqual(error as? OidcSignInError, .noStoredGrant(host: harness.host))
    }
    do {
      _ = try await inFlight.value
      XCTFail("Expected the in-flight refresh to be dropped")
    } catch {
      XCTAssertTrue(error is CancellationError, "\(error)")
    }
    await revoke.value
    XCTAssertEqual(harness.server.requests.filter { $0.path == "/omnigent/oauth/token" }.count, 1)
  }

  func testSignOutSurvivesRevokeFailure() async throws {
    let harness = try OidcHarness()
    harness.store.set(OidcRefreshGrant(refreshToken: "r1", userID: nil), for: harness.origin)
    harness.server.respond("/omnigent/oauth/revoke", status: 500, json: [:])
    await (try harness.credentials.signOut(serverURL: harness.serverURL)).value
    XCTAssertEqual(harness.server.requests.count, 1)
    XCTAssertNil(harness.store.grant(for: harness.origin))

    let unreachable = URL(string: "http://localhost:1/")!
    harness.store.set(
      OidcRefreshGrant(refreshToken: "r2", userID: nil), for: unreachable.omnigentOrigin!)
    await (try harness.credentials.signOut(serverURL: unreachable)).value
    XCTAssertNil(harness.store.grant(for: unreachable.omnigentOrigin!))

    // Nothing stored: nothing to revoke.
    await (try harness.credentials.signOut(serverURL: harness.serverURL)).value
    XCTAssertEqual(harness.server.requests.count, 1)
  }

  // MARK: Session verification

  func testIsAcceptedMapsMeStatus() async throws {
    let harness = try OidcHarness()
    for (status, accepted) in [(200, true), (401, false), (403, false), (302, false)] {
      harness.server.respond(
        "/omnigent/v1/me", status: status, headers: ["location": "https://idp.example.com/login"],
        json: [:])
      let result = try await harness.credentials.isAccepted(
        token: "session-1", cookieName: "ap_session", serverURL: harness.serverURL)
      XCTAssertEqual(result, accepted, "\(status)")
    }
    let request = try XCTUnwrap(harness.server.requests.first)
    XCTAssertEqual(request.method, "GET")
    XCTAssertEqual(request.path, "/omnigent/v1/me")
    XCTAssertEqual(request.headers["cookie"], "ap_session=session-1")
    // The redirect was not followed.
    XCTAssertEqual(harness.server.requests.count, 4)

    harness.server.respond("/omnigent/v1/me", status: 500, json: [:])
    do {
      _ = try await harness.credentials.isAccepted(
        token: "session-1", cookieName: "ap_session", serverURL: harness.serverURL)
      XCTFail("Expected network failure")
    } catch {
      XCTAssertEqual(error as? OidcSignInError, .network(host: harness.host))
    }
    do {
      _ = try await harness.credentials.isAccepted(
        token: "session-1", cookieName: "ap_session", serverURL: URL(string: "http://localhost:1")!)
      XCTFail("Expected network failure")
    } catch {
      XCTAssertEqual(error as? OidcSignInError, .network(host: "localhost:1"))
    }
  }

  // MARK: Session cookie

  func testSessionCookieAttributes() throws {
    let expiry = Self.now.addingTimeInterval(3600)
    let secure = try XCTUnwrap(
      OidcSessionToken(token: "session-1", expiresAt: expiry).sessionCookie(
        named: "__Host-ap_session",
        serverURL: URL(string: "https://Omnigent.Example.com/omnigent/")!
      ))
    XCTAssertEqual(secure.name, "__Host-ap_session")
    XCTAssertEqual(secure.value, "session-1")
    XCTAssertEqual(secure.domain, "omnigent.example.com")
    XCTAssertEqual(secure.path, "/")
    XCTAssertTrue(secure.isSecure)
    XCTAssertTrue(secure.isHTTPOnly)
    XCTAssertEqual(secure.sameSitePolicy, .sameSiteLax)
    XCTAssertEqual(secure.expiresDate, expiry)
    XCTAssertFalse(secure.isSessionOnly)

    let plain = try XCTUnwrap(
      OidcSessionToken(token: "session-2", expiresAt: nil).sessionCookie(
        named: "ap_session", serverURL: URL(string: "http://localhost:6767")!))
    XCTAssertEqual(plain.domain, "localhost")
    XCTAssertFalse(plain.isSecure)
    XCTAssertTrue(plain.isHTTPOnly)
    XCTAssertEqual(plain.sameSitePolicy, .sameSiteLax)
    XCTAssertNil(plain.expiresDate)
    XCTAssertTrue(plain.isSessionOnly)
  }
}

@MainActor
private final class OidcHarness {
  let server = MockOidcServer()
  let store = MemoryOidcCredentialStore()
  let http: MockHTTPServer
  let anchor: ASPresentationAnchor
  var browsers: [FakeOidcAuthenticationSession] = []
  var startResult = true
  private var onStart: (() -> Void)?
  lazy var credentials = OidcCredentials(
    store: store,
    sessionFactory: { [unowned self] url, callback, _, completion in
      let browser = FakeOidcAuthenticationSession(
        url: url, callback: callback, completion: completion)
      browser.startResult = startResult
      browser.onStart = onStart
      browsers.append(browser)
      return browser
    }, now: { Date(timeIntervalSince1970: 1_700_000_000) })

  var port: Int { http.port }
  var serverURL: URL { URL(string: "http://localhost:\(port)/omnigent/")! }
  var origin: String { serverURL.omnigentOrigin! }
  var host: String { serverURL.omnigentHostLabel }

  init() throws {
    let server = server
    http = try MockHTTPServer(requestHandler: { server.handle($0) })
    let scene = try XCTUnwrap(
      UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
    anchor = ASPresentationAnchor(windowScene: scene)
  }

  /// Starts a sign-in and waits until its sheet is presented.
  func startSignIn() async throws -> (Task<OidcSessionToken, Error>, FakeOidcAuthenticationSession)
  {
    let started = XCTestExpectation(description: "sheet started")
    onStart = { started.fulfill() }
    let credentials = credentials
    let serverURL = serverURL
    let anchor = anchor
    let task = Task { try await credentials.signIn(serverURL: serverURL, anchor: anchor) }
    let result = await XCTWaiter().fulfillment(of: [started], timeout: 2)
    onStart = nil
    guard result == .completed, let browser = browsers.last else {
      throw URLError(
        .timedOut, userInfo: [NSLocalizedDescriptionKey: "sign-in sheet did not start"])
    }
    return (task, browser)
  }

  /// Server tokens that would break or extend the `Cookie` header.
  static let unsafeTokens = [
    "", " ", "a b", "a;b", "a,b", "a\"b", "a\\b", "a\u{7}b", "a\nb", "a\u{7F}b", "caf\u{E9}",
  ]

  /// Waits until the server has received a request for `path`, and returns it.
  @discardableResult
  func waitForRequest(_ path: String) async throws -> MockHTTPServer.Request {
    for _ in 0..<200 {
      if let request = server.requests.first(where: { $0.path == path }) { return request }
      try await Task.sleep(nanoseconds: 10_000_000)
    }
    throw URLError(.timedOut, userInfo: [NSLocalizedDescriptionKey: "no request for \(path)"])
  }

  static func form(_ request: MockHTTPServer.Request) -> [String: String] {
    let body = String(data: request.body, encoding: .utf8) ?? ""
    return Dictionary(
      uniqueKeysWithValues: body.split(separator: "&").map {
        let pair = $0.split(separator: "=", maxSplits: 1).map(String.init)
        return (pair[0], (pair.count > 1 ? pair[1] : "").removingPercentEncoding!)
      })
  }
}

/// Canned JSON answers by path, recording every request it receives.
private final class MockOidcServer: @unchecked Sendable {
  private let lock = NSLock()
  private var responses: [String: (Int, [String: String], Data)] = [:]
  private var received: [MockHTTPServer.Request] = []
  private var responseDelay: useconds_t = 0

  var requests: [MockHTTPServer.Request] {
    lock.withLock { received }
  }

  var delay: useconds_t {
    get { lock.withLock { responseDelay } }
    set { lock.withLock { responseDelay = newValue } }
  }

  func respond(
    _ path: String, status: Int = 200, headers: [String: String] = [:], json: [String: Any]
  ) {
    let data = try! JSONSerialization.data(withJSONObject: json)
    lock.withLock {
      responses[path] = (
        status, headers.merging(["content-type": "application/json"]) { a, _ in a }, data
      )
    }
  }

  func handle(_ request: MockHTTPServer.Request) -> (Int, [String: String], Data) {
    let (response, delay) = lock.withLock {
      received.append(request)
      let path = String(request.path.split(separator: "?", maxSplits: 1).first ?? "")
      return (responses[path] ?? (404, [:], Data()), responseDelay)
    }
    if delay > 0 { usleep(delay) }
    return response
  }
}

private final class MemoryOidcCredentialStore: OidcCredentialStoring, @unchecked Sendable {
  private let lock = NSLock()
  private var grants: [String: OidcRefreshGrant] = [:]
  private var failingSave: Error?
  private var failingDelete: Error?

  var deleteError: Error? {
    get { lock.withLock { failingDelete } }
    set { lock.withLock { failingDelete = newValue } }
  }

  var saveError: Error? {
    get { lock.withLock { failingSave } }
    set { lock.withLock { failingSave = newValue } }
  }

  func grant(for origin: String) -> OidcRefreshGrant? { lock.withLock { grants[origin] } }
  func set(_ grant: OidcRefreshGrant?, for origin: String) {
    lock.withLock { grants[origin] = grant }
  }

  func load(origin: String) throws -> OidcRefreshGrant? { grant(for: origin) }
  func save(_ grant: OidcRefreshGrant, origin: String) throws {
    if let saveError { throw saveError }
    set(grant, for: origin)
  }
  func delete(origin: String) throws {
    if let deleteError { throw deleteError }
    set(nil, for: origin)
  }
}

@MainActor
private final class FakeOidcAuthenticationSession: WebAuthenticationSession {
  let url: URL
  let callback: ASWebAuthenticationSession.Callback
  let completion: ASWebAuthenticationSession.CompletionHandler
  var startResult = true
  var cancelCount = 0
  var onStart: (() -> Void)?

  init(
    url: URL, callback: ASWebAuthenticationSession.Callback,
    completion: @escaping ASWebAuthenticationSession.CompletionHandler
  ) {
    self.url = url
    self.callback = callback
    self.completion = completion
  }

  /// The `native_state` this sheet's sign-in expects back.
  var state: String {
    URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?.first {
      $0.name == "native_state"
    }?.value ?? ""
  }

  func start() -> Bool {
    onStart?()
    return startResult
  }

  func cancel() { cancelCount += 1 }

  func complete(_ items: [(String, String)], path: String = "/oauth/callback") {
    var components = URLComponents()
    components.scheme = "ai.omnigent.ios"
    components.path = path
    components.queryItems = items.map { URLQueryItem(name: $0.0, value: $0.1) }
    completion(components.url, nil)
  }
}
