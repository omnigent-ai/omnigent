import XCTest

@testable import Omnigent

final class OidcAuthRouteTests: XCTestCase {
  func testMatchesLoginAndLogoutAtTheOriginRoot() {
    let server = URL(string: "https://omnigent.example.com")!
    XCTAssertEqual(route("https://omnigent.example.com/auth/login", server), .login)
    XCTAssertEqual(route("https://omnigent.example.com/auth/login?next=%2Fc%2Fx", server), .login)
    XCTAssertEqual(route("https://omnigent.example.com/auth/logout", server), .logout)
    XCTAssertEqual(route("https://OMNIGENT.example.com/auth/logout", server), .logout)
  }

  func testMatchesRoutesUnderTheMountOnly() {
    let server = URL(string: "https://example.com/omnigent/")!
    XCTAssertEqual(route("https://example.com/omnigent/auth/login", server), .login)
    XCTAssertEqual(route("https://example.com/omnigent/auth/logout", server), .logout)
    XCTAssertNil(route("https://example.com/auth/login", server))
    XCTAssertNil(route("https://example.com/other/auth/login", server))
  }

  func testIgnoresOtherPagesAndOtherOrigins() {
    let server = URL(string: "https://omnigent.example.com")!
    for other in [
      "https://omnigent.example.com/",
      "https://omnigent.example.com/c/conv_1",
      "https://omnigent.example.com/auth/login/",
      "https://omnigent.example.com/auth/cli-login",
      "https://omnigent.example.com/v1/auth/login",
      "http://omnigent.example.com/auth/login",
      "https://omnigent.example.com:8443/auth/login",
      "https://idp.example.com/auth/login",
    ] {
      XCTAssertNil(route(other, server), other)
    }
  }

  func testAppPagesStayUnderTheMountAndExcludeAuthRoutes() {
    let server = URL(string: "https://example.com/omnigent")!
    XCTAssertTrue(OidcWebSession.isPage(URL(string: "https://example.com/omnigent")!, of: server))
    XCTAssertTrue(
      OidcWebSession.isPage(URL(string: "https://example.com/omnigent/c/conv_1?x=1")!, of: server))
    XCTAssertFalse(OidcWebSession.isPage(URL(string: "https://example.com/other")!, of: server))
    XCTAssertFalse(
      OidcWebSession.isPage(URL(string: "https://example.com/omnigenty/c/x")!, of: server))
    XCTAssertFalse(
      OidcWebSession.isPage(URL(string: "https://example.com/omnigent/auth/login")!, of: server))
    XCTAssertFalse(
      OidcWebSession.isPage(URL(string: "https://idp.example.com/omnigent")!, of: server))
  }

  private func route(_ address: String, _ server: URL) -> OidcAuthRoute? {
    OidcAuthRoute(url: URL(string: address)!, serverURL: server)
  }
}

final class OidcNativeSignInGatingTests: XCTestCase {
  private let https = URL(string: "https://omnigent.example.com")!

  func testOidcWithThisAppsRedirectAndACookieUsesNativeSignIn() {
    let manifest = parse(
      #"{"manifest_version": 1, "auth": {"mode": "oidc", "session_cookie": "__Host-ap_session","#
        + #" "native_redirect_uris": ["ai.omnigent.ios:/oauth/callback"]}}"#)
    XCTAssertEqual(manifest.nativeSignInCookieName, "__Host-ap_session")
  }

  func testMissingRedirectOrCookieKeepsTheLegacyFlow() {
    for body in [
      #"{"manifest_version": 1, "auth": {"mode": "oidc", "session_cookie": "__Host-ap_session"}}"#,
      #"{"manifest_version": 1, "auth": {"mode": "oidc", "session_cookie": "__Host-ap_session","#
        + #" "native_redirect_uris": ["http://127.0.0.1/callback", "omnigent:/oauth/callback"]}}"#,
      #"{"manifest_version": 1, "auth": {"mode": "oidc","#
        + #" "native_redirect_uris": ["ai.omnigent.ios:/oauth/callback"]}}"#,
    ] {
      XCTAssertNil(parse(body).nativeSignInCookieName, body)
    }
  }

  func testOtherModesAndTheBaselineKeepTheLegacyFlow() {
    for mode in ["accounts", "header", "custom", "none"] {
      let manifest = parse(
        #"{"manifest_version": 1, "auth": {"mode": ""# + mode
          + #"", "session_cookie": "__Host-ap_session","#
          + #" "native_redirect_uris": ["ai.omnigent.ios:/oauth/callback"]}}"#)
      XCTAssertNil(manifest.nativeSignInCookieName, mode)
    }
    XCTAssertNil(ServerManifest.baseline.nativeSignInCookieName)
    XCTAssertNil(parse(#"{"manifest_version": 1}"#).nativeSignInCookieName)
  }

  private func parse(_ body: String) -> ServerManifest {
    ServerManifest.parse(Data(body.utf8), serverURL: https)
  }
}

final class OidcRenewalGuardTests: XCTestCase {
  func testASecondSignInRequestWithinFifteenSecondsIsRejected() {
    var guardrail = OidcRenewalGuard()
    let start = Date(timeIntervalSince1970: 1_000)
    XCTAssertTrue(guardrail.shouldRenew(now: start, renewalPending: false))
    XCTAssertFalse(
      guardrail.shouldRenew(now: start.addingTimeInterval(14.9), renewalPending: false))
  }

  func testARequestAfterTheWindowRenewsAgain() {
    var guardrail = OidcRenewalGuard()
    let start = Date(timeIntervalSince1970: 1_000)
    XCTAssertTrue(guardrail.shouldRenew(now: start, renewalPending: false))
    XCTAssertTrue(guardrail.shouldRenew(now: start.addingTimeInterval(15), renewalPending: false))
  }

  func testARequestDuringAPendingRenewalJoinsIt() {
    var guardrail = OidcRenewalGuard()
    let start = Date(timeIntervalSince1970: 1_000)
    XCTAssertTrue(guardrail.shouldRenew(now: start, renewalPending: false))
    XCTAssertTrue(guardrail.shouldRenew(now: start.addingTimeInterval(1), renewalPending: true))
    // The joined request restarts the window.
    XCTAssertFalse(guardrail.shouldRenew(now: start.addingTimeInterval(15), renewalPending: false))
  }
}

@MainActor
final class OidcSignOutGenerationsTests: XCTestCase {
  func testSignOutInvalidatesWorkStartedBeforeIt() {
    let generations = OidcSignOutGenerations()
    let origin = "https://omnigent.example.com"
    let started = generations.current(origin: origin)
    XCTAssertTrue(generations.isCurrent(started, origin: origin))
    generations.signOut(origin: origin)
    XCTAssertFalse(generations.isCurrent(started, origin: origin))
    XCTAssertTrue(generations.isCurrent(generations.current(origin: origin), origin: origin))
  }

  func testSignOutLeavesOtherOriginsAlone() {
    let generations = OidcSignOutGenerations()
    let other = "https://other.example.com"
    let started = generations.current(origin: other)
    generations.signOut(origin: "https://omnigent.example.com")
    XCTAssertTrue(generations.isCurrent(started, origin: other))
  }
}

final class OidcWebSessionTests: XCTestCase {
  private let server = URL(string: "https://omnigent.example.com/")!
  private let now = Date(timeIntervalSince1970: 1_000_000)

  func testLiveSessionCookieMatchesNameHostAndExpiry() {
    let live = cookie("__Host-ap_session", "live", host: "omnigent.example.com", expires: 60)
    let expired = cookie("__Host-ap_session", "old", host: "omnigent.example.com", expires: -1)
    let otherHost = cookie("__Host-ap_session", "x", host: "other.example.com", expires: 60)
    let otherName = cookie("ap_session", "y", host: "omnigent.example.com", expires: 60)
    let found = OidcWebSession.liveSessionCookie(
      in: [expired, otherHost, otherName, live], named: "__Host-ap_session", serverURL: server,
      now: now)
    XCTAssertEqual(found?.value, "live")
    XCTAssertEqual(
      OidcWebSession.sessionCookies(
        in: [expired, otherHost, otherName, live], named: "__Host-ap_session", serverURL: server,
        now: now
      ).map(\.value), ["live"])
  }

  func testSecureCookieIsNeverOfferedToAnHttpServerOnTheSameHost() {
    let secure = cookie("ap_session", "from-https", host: "localhost", expires: 60, secure: true)
    let plain = URL(string: "http://localhost:6767/")!
    XCTAssertNil(
      OidcWebSession.liveSessionCookie(
        in: [secure], named: "ap_session", serverURL: plain, now: now))
    XCTAssertTrue(
      OidcWebSession.sessionCookies(
        in: [secure], named: "ap_session", serverURL: plain, now: now
      ).isEmpty)
    XCTAssertEqual(
      OidcWebSession.liveSessionCookie(
        in: [secure], named: "ap_session", serverURL: URL(string: "https://localhost:8443/")!,
        now: now)?.value, "from-https")
  }

  func testCookiePathMustCoverTheServersMount() {
    let mounted = URL(string: "https://omnigent.example.com/omnigent/")!
    let other = cookie(
      "ap_session", "other-app", host: "omnigent.example.com", expires: 60, path: "/other")
    let prefix = cookie(
      "ap_session", "prefix", host: "omnigent.example.com", expires: 60, path: "/omni")
    let mount = cookie(
      "ap_session", "mount", host: "omnigent.example.com", expires: 60, path: "/omnigent")
    XCTAssertEqual(
      OidcWebSession.sessionCookies(
        in: [other, prefix, mount], named: "ap_session", serverURL: mounted, now: now
      ).map(\.value), ["mount"])
    XCTAssertNil(
      OidcWebSession.liveSessionCookie(
        in: [mount], named: "ap_session", serverURL: server, now: now))
    XCTAssertTrue(OidcWebSession.pathMatches(cookiePath: "/", requestPath: "/v1/me"))
    XCTAssertTrue(
      OidcWebSession.pathMatches(cookiePath: "/omnigent/", requestPath: "/omnigent/v1/me"))
    XCTAssertFalse(
      OidcWebSession.pathMatches(cookiePath: "/omnigent", requestPath: "/omnigentx/v1/me"))
  }

  func testOnlyARefusedSignInStopsAutoOpening() {
    let host = "omnigent.example.com"
    XCTAssertTrue(
      OidcWebSession.stopsAutoOpening(
        after: OidcSignInError.signInRefused(host: host, description: "not permitted")))
    XCTAssertTrue(
      OidcWebSession.stopsAutoOpening(
        after: OidcSignInError.signInRefused(host: host, description: nil)))
    for other: Error in [
      CancellationError(), OidcSignInError.network(host: host),
      OidcSignInError.grantExpired(host: host), OidcSignInError.grantRejected(host: host),
      OidcSignInError.noStoredGrant(host: host), OidcSignInError.sessionRejected(host: host),
      OidcSignInError.browserUnavailable(host: host), OidcSignInError.invalidCallback(host: host),
      OidcSignInError.exchangeFailed(host: host),
    ] {
      XCTAssertFalse(OidcWebSession.stopsAutoOpening(after: other), "\(other)")
    }
  }

  func testABackgroundRenewalsLostGrantExplainsTheNextPrompt() {
    let host = "omnigent.example.com"
    let expired = OidcSignInError.grantExpired(host: host)
    XCTAssertEqual(OidcWebSession.rememberedRenewalCause(expired), expired)
    XCTAssertEqual(
      OidcWebSession.rememberedRenewalCause(OidcSignInError.grantRejected(host: host)),
      .grantRejected(host: host))
    XCTAssertNil(OidcWebSession.rememberedRenewalCause(OidcSignInError.network(host: host)))
    XCTAssertNil(OidcWebSession.rememberedRenewalCause(OidcSignInError.noStoredGrant(host: host)))

    let later = OidcWebSession.reauthenticationCause(
      for: OidcSignInError.noStoredGrant(host: host), remembered: expired)
    XCTAssertEqual(
      OidcWebSession.reauthenticationMessage(for: later, host: host),
      "Your sign-in to omnigent.example.com has expired. Sign in again to continue.")
    XCTAssertEqual(
      OidcWebSession.reauthenticationCause(
        for: OidcSignInError.sessionRejected(host: host), remembered: expired) as? OidcSignInError,
      .sessionRejected(host: host))
    XCTAssertEqual(
      OidcWebSession.reauthenticationCause(
        for: OidcSignInError.noStoredGrant(host: host), remembered: nil) as? OidcSignInError,
      .noStoredGrant(host: host))
  }

  func testRenewalDelayLeavesAMarginBeforeExpiry() {
    XCTAssertEqual(
      OidcWebSession.renewalDelay(expiresAt: now.addingTimeInterval(8 * 3600), now: now),
      8 * 3600 - 60)
    XCTAssertEqual(
      OidcWebSession.renewalDelay(expiresAt: now.addingTimeInterval(100), now: now), 80)
    XCTAssertEqual(OidcWebSession.renewalDelay(expiresAt: now, now: now), 0)
    XCTAssertEqual(OidcWebSession.renewalDelay(expiresAt: now.addingTimeInterval(-5), now: now), 0)
  }

  func testOversizedRenewalDelayIsBounded() {
    for expiresAt in [now.addingTimeInterval(20_000_000_000), Date.distantFuture] {
      let delay = OidcWebSession.renewalDelay(expiresAt: expiresAt, now: now)
      XCTAssertEqual(delay, OidcWebSession.maxRenewalDelay)
      XCTAssertLessThan(delay * 1_000_000_000, Double(UInt64.max))
    }
  }

  func testReauthenticationMessagesNameTheCause() {
    let host = "omnigent.example.com"
    XCTAssertEqual(
      OidcWebSession.reauthenticationMessage(
        for: OidcSignInError.grantExpired(host: host), host: host),
      "Your sign-in to omnigent.example.com has expired. Sign in again to continue.")
    XCTAssertEqual(
      OidcWebSession.reauthenticationMessage(
        for: OidcSignInError.grantRejected(host: host), host: host),
      "omnigent.example.com ended your session. Sign in again to continue.")
    XCTAssertEqual(
      OidcWebSession.reauthenticationMessage(for: CancellationError(), host: host),
      "Sign in to omnigent.example.com to continue.")
    XCTAssertTrue(OidcWebSession.isNetworkFailure(OidcSignInError.network(host: host)))
    XCTAssertFalse(OidcWebSession.isNetworkFailure(OidcSignInError.grantExpired(host: host)))
    XCTAssertEqual(
      OidcWebSession.signedOutMessage(host: host, complete: true),
      "You're signed out of omnigent.example.com.")
  }

  func testClosingTheSheetKeepsOnlyAnEndedSessionsReason() {
    let host = "omnigent.example.com"
    for cause in [
      OidcSignInError.grantExpired(host: host), .grantRejected(host: host),
      .sessionRejected(host: host),
    ] {
      XCTAssertEqual(OidcWebSession.cancelledSignInCause(cause), cause)
    }
    for quiet: Error in [
      OidcSignInError.noStoredGrant(host: host), OidcSignInError.network(host: host),
      OidcCredentialStoreError.invalidData, CancellationError(),
    ] {
      XCTAssertNil(OidcWebSession.cancelledSignInCause(quiet), "\(quiet)")
    }
  }

  private func cookie(
    _ name: String, _ value: String, host: String, expires: TimeInterval, secure: Bool = false,
    path: String = "/"
  ) -> HTTPCookie {
    var properties: [HTTPCookiePropertyKey: Any] = [
      .name: name, .value: value, .domain: host, .path: path,
      .expires: now.addingTimeInterval(expires),
    ]
    if secure { properties[.secure] = "TRUE" }
    return HTTPCookie(properties: properties)!
  }
}

final class ServerPickerPayloadTests: XCTestCase {
  func testPayloadCarriesCanSignOut() throws {
    for canSignOut in [true, false] {
      let json = try XCTUnwrap(
        WebViewModel.serverPickerJSON(
          currentOrigin: "https://omnigent.example.com", managedServers: ["https://m.example.com"],
          recentServers: ["https://r.example.com"], canSignOut: canSignOut))
      let payload = try XCTUnwrap(
        JSONSerialization.jsonObject(with: Data(json.utf8)) as? [String: Any])
      XCTAssertEqual(payload["canSignOut"] as? Bool, canSignOut)
      XCTAssertEqual(payload["currentOrigin"] as? String, "https://omnigent.example.com")
      XCTAssertEqual(payload["managedServers"] as? [String], ["https://m.example.com"])
      XCTAssertEqual(payload["recentServers"] as? [String], ["https://r.example.com"])
    }
    XCTAssertNil(
      WebViewModel.serverPickerJSON(
        currentOrigin: nil, managedServers: [], recentServers: [], canSignOut: true))
  }

  func testBridgeExposesSignOutOfServerForEveryConnectionAndPassesCanSignOut() {
    for managesWorkspace in [true, false] {
      let script = OmnigentWebView.nativeBridgeScript(managesWorkspace: managesWorkspace)
      XCTAssertTrue(script.contains("signOutOfServer()"))
      XCTAssertTrue(script.contains("canSignOut: payload.canSignOut === true"))
      XCTAssertEqual(script.contains("method: 'signOut'"), managesWorkspace)
    }
  }
}

@MainActor
final class OidcPersistedCookiesTests: XCTestCase {
  func testPersistedCookiesLoadBeforeTheyAreRead() async throws {
    let cookie = try XCTUnwrap(
      HTTPCookie(properties: [
        .name: "ap_session", .value: "kept", .domain: "localhost", .path: "/",
        .expires: Date().addingTimeInterval(3600),
      ]))
    let jar = ColdCookieJar(persisted: [cookie])
    let cookies = await OidcWebSession.persistedCookies(in: jar)
    XCTAssertEqual(cookies.map(\.value), ["kept"])
    XCTAssertEqual(jar.calls, ["load", "all"])
  }
}

/// A jar that, like WebKit's after a relaunch, reads empty until its persisted cookies load.
@MainActor
private final class ColdCookieJar: OidcCookieJar {
  private let persisted: [HTTPCookie]
  private var loaded = false
  private(set) var calls: [String] = []

  init(persisted: [HTTPCookie]) { self.persisted = persisted }

  func loadPersistedCookies() async {
    calls.append("load")
    loaded = true
  }

  func allCookies() async -> [HTTPCookie] {
    calls.append("all")
    return loaded ? persisted : []
  }
}
