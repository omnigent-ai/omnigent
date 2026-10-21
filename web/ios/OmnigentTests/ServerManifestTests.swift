import XCTest

@testable import Omnigent

final class ServerManifestTests: XCTestCase {
  private static let json = ["content-type": "application/json"]

  func testReadsOidcManifestAtTheOriginWithCookieAndNativeRedirects() async throws {
    let server = try MockHTTPServer { method, path in
      XCTAssertEqual(method, "GET")
      XCTAssertEqual(path, "/.well-known/omnigent.json")
      return (
        200, ["content-type": "application/json; charset=utf-8"],
        Data(
          #"""
          {"manifest_version": 1, "server_version": "0.18.0", "auth": {"mode": "oidc",
           "session_cookie": "ap_session",
           "native_redirect_uris": ["ai.omnigent.ios:/oauth/callback", 7, null]}}
          """#.utf8)
      )
    }

    let manifest = try await ServerManifest.fetch(
      for: URL(string: "http://localhost:\(server.port)/omnigent/?o=1#chat")!)

    XCTAssertEqual(
      manifest,
      ServerManifest(
        manifestVersion: 1,
        auth: .init(
          mode: .oidc, sessionCookie: "ap_session",
          nativeRedirectURIs: ["ai.omnigent.ios:/oauth/callback"])))
  }

  func testAuthWithoutNativeRedirectsHasNone() async throws {
    let server = try Self.server(#"{"manifest_version": 1, "auth": {"mode": "accounts"}}"#)
    let manifest = try await ServerManifest.fetch(for: server.url)
    XCTAssertEqual(
      manifest.auth, .init(mode: .accounts, sessionCookie: nil, nativeRedirectURIs: []))

    let nullRedirects = ServerManifest.parse(
      Data(
        #"{"manifest_version": 1, "auth": {"mode": "none", "native_redirect_uris": null}}"#.utf8),
      serverURL: server.url)
    XCTAssertEqual(
      nullRedirects.auth, .init(mode: .unauthenticated, sessionCookie: nil, nativeRedirectURIs: []))
  }

  func testUnknownModeDropsAuthButKeepsVersion() async throws {
    let server = try Self.server(
      #"{"manifest_version": 2, "auth": {"mode": "saml", "session_cookie": "ap_session"}}"#)
    let manifest = try await ServerManifest.fetch(for: server.url)
    XCTAssertEqual(manifest, ServerManifest(manifestVersion: 2, auth: nil))
  }

  func testUnknownSessionCookieIsDropped() async throws {
    let server = try Self.server(
      #"{"manifest_version": 1, "auth": {"mode": "oidc", "session_cookie": "session"}}"#)
    let manifest = try await ServerManifest.fetch(for: server.url)
    XCTAssertEqual(manifest.auth, .init(mode: .oidc, sessionCookie: nil, nativeRedirectURIs: []))
  }

  func testHostPrefixedCookieOnlyForHTTPS() async throws {
    let body =
      #"{"manifest_version": 1, "auth": {"mode": "oidc", "session_cookie": "__Host-ap_session"}}"#
    let server = try Self.server(body)
    let http = try await ServerManifest.fetch(for: server.url)
    XCTAssertEqual(http.auth?.mode, .oidc)
    XCTAssertNil(http.auth?.sessionCookie)

    let https = ServerManifest.parse(
      Data(body.utf8), serverURL: URL(string: "https://omnigent.example.com/omnigent")!)
    XCTAssertEqual(https.auth?.sessionCookie, "__Host-ap_session")
  }

  func testNotFoundIsBaseline() async throws {
    let server = try MockHTTPServer { _, _ in
      (404, Self.json, Data(#"{"manifest_version": 1}"#.utf8))
    }
    let manifest = try await ServerManifest.fetch(for: server.url)
    XCTAssertEqual(manifest, .baseline)
  }

  func testHTMLFromSPACatchAllIsBaseline() async throws {
    let server = try MockHTTPServer { _, _ in
      (200, ["content-type": "text/html"], Data(#"{"manifest_version": 1}"#.utf8))
    }
    let manifest = try await ServerManifest.fetch(for: server.url)
    XCTAssertEqual(manifest, .baseline)
  }

  func testMalformedJSONIsBaseline() async throws {
    let server = try Self.server(#"{"manifest_version": 1, "auth": "#)
    let manifest = try await ServerManifest.fetch(for: server.url)
    XCTAssertEqual(manifest, .baseline)
  }

  func testNonNumericVersionIsBaseline() async throws {
    for version in [#""1""#, "true", "null"] {
      let server = try Self.server(#"{"manifest_version": \#(version), "auth": {"mode": "oidc"}}"#)
      let manifest = try await ServerManifest.fetch(for: server.url)
      XCTAssertEqual(manifest, .baseline, version)
    }
    let missing = ServerManifest.parse(
      Data(#"{"auth": {"mode": "oidc"}}"#.utf8), serverURL: URL(string: "https://h")!)
    XCTAssertEqual(missing, .baseline)
  }

  func testRedirectIsNotFollowed() async throws {
    let server = try MockHTTPServer { _, path in
      if path == "/.well-known/omnigent.json" {
        return (302, ["location": "/login?next=manifest"], Data())
      }
      XCTFail("Manifest fetch must not follow a redirect to \(path)")
      return (200, Self.json, Data(#"{"manifest_version": 1, "auth": {"mode": "oidc"}}"#.utf8))
    }
    let manifest = try await ServerManifest.fetch(for: server.url)
    XCTAssertEqual(manifest, .baseline)
  }

  func testUnreachableServerIsBaseline() async throws {
    let port: Int
    do {
      let server = try Self.server("{}")
      port = server.port
    }
    let manifest = try await ServerManifest.fetch(for: URL(string: "http://localhost:\(port)")!)
    XCTAssertEqual(manifest, .baseline)
  }

  func testCancellationPropagates() async throws {
    let requested = expectation(description: "manifest requested")
    let server = try MockHTTPServer { _, _ in
      requested.fulfill()
      usleep(1_000_000)
      return (200, Self.json, Data(#"{"manifest_version": 1}"#.utf8))
    }
    let task = Task { try await ServerManifest.fetch(for: server.url) }
    await fulfillment(of: [requested], timeout: 2)
    task.cancel()
    do {
      _ = try await task.value
      XCTFail("Expected cancellation")
    } catch {
      XCTAssertTrue(error is CancellationError, "\(error)")
    }
  }

  func testTransportCancellationWithoutCallerCancellationIsBaseline() async throws {
    let requested = expectation(description: "manifest requested")
    let server = try MockHTTPServer { _, _ in
      requested.fulfill()
      usleep(1_000_000)
      return (200, Self.json, Data(#"{"manifest_version": 1}"#.utf8))
    }
    let session = URLSession(configuration: .ephemeral)
    let task = Task { try await ServerManifest.fetch(for: server.url, session: session) }
    await fulfillment(of: [requested], timeout: 2)
    // Invalidating the session fails the request with URLError.cancelled; the caller is not cancelled.
    session.invalidateAndCancel()
    let manifest = try await task.value
    XCTAssertEqual(manifest, .baseline)
  }

  private static func server(_ body: String) throws -> MockHTTPServer {
    try MockHTTPServer { _, _ in (200, json, Data(body.utf8)) }
  }
}

extension MockHTTPServer {
  fileprivate var url: URL { URL(string: "http://localhost:\(port)")! }
}
