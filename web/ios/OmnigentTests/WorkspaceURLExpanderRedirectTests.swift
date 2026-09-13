import XCTest

@testable import Omnigent

/// Integration test for the redirect-policy half of [F-CR-7], run on the
/// simulator over a REAL local HTTP network.
///
/// `WorkspaceURLExpander.expandIfNeeded`'s probe is https-only, and a deep link
/// to `localhost` infers `http` — so the probe never fires for loopback, which
/// means a localhost UI test can't exercise the redirect. This test targets the
/// fix mechanism directly: it starts real local HTTP servers and issues
/// requests through a `URLSession` backed by `SameOriginRedirectHandler`,
/// asserting that:
///   1. a CROSS-ORIGIN redirect (distinct port ⇒ distinct origin) is NOT
///      followed — the 3xx response is returned with `response.url` still on
///      the approved origin; and
///   2. a SAME-ORIGIN redirect (same host+port, different path) IS followed.
///
/// The `response.url` defense-in-depth check inside `expandIfNeeded` is covered
/// separately by `WorkspaceURLExpanderTests.testRejectsResponseFromDifferentOrigin`.
final class WorkspaceURLExpanderRedirectTests: XCTestCase {
  func testBlocksCrossOriginRedirect() async throws {
    // B answers the redirected probe with `server: databricks` — the payload
    // that, if the redirect were followed, would let B impersonate a workspace.
    let serverB = try MockHTTPServer { method, _ in
      if method == "HEAD" {
        return (200, ["server": "databricks", "content-length": "0"], Data())
      }
      return (200, ["content-type": "text/plain"], Data("LEAK".utf8))
    }

    // A 3xx-redirects the probe to B — a different origin (different port).
    let serverA = try MockHTTPServer { method, _ in
      if method == "HEAD" {
        let location = "http://localhost:\(serverB.port)/"
        return (302, ["location": location, "content-length": "0"], Data())
      }
      return (200, ["content-type": "text/plain"], Data("A".utf8))
    }

    let session = URLSession(
      configuration: .ephemeral,
      delegate: SameOriginRedirectHandler(),
      delegateQueue: nil
    )
    defer { session.invalidateAndCancel() }

    var request = URLRequest(url: URL(string: "http://localhost:\(serverA.port)/")!)
    request.httpMethod = "HEAD"

    let (_, response) = try await session.data(for: request)
    let http = try XCTUnwrap(response as? HTTPURLResponse)

    // The redirect must NOT have been followed: we get the 3xx from A, and the
    // response URL is still A's origin (not B's).
    XCTAssertEqual(http.statusCode, 302)
    let responsePort = http.url?.port
    XCTAssertEqual(
      responsePort, serverA.port,
      "Cross-origin redirect was followed to port \(String(describing: responsePort)) — consent boundary broken."
    )
    XCTAssertNotEqual(responsePort, serverB.port)
  }

  func testFollowsSameOriginRedirect() async throws {
    // A same-origin redirect: /start → /elsewhere on the SAME host+port.
    let server = try MockHTTPServer { method, path in
      if method == "HEAD", path == "/start" {
        return (302, ["location": "/elsewhere", "content-length": "0"], Data())
      }
      if method == "HEAD", path == "/elsewhere" {
        return (200, ["server": "databricks", "content-length": "0"], Data())
      }
      return (404, ["content-length": "0"], Data())
    }

    let session = URLSession(
      configuration: .ephemeral,
      delegate: SameOriginRedirectHandler(),
      delegateQueue: nil
    )
    defer { session.invalidateAndCancel() }

    var request = URLRequest(url: URL(string: "http://localhost:\(server.port)/start")!)
    request.httpMethod = "HEAD"

    let (_, response) = try await session.data(for: request)
    let http = try XCTUnwrap(response as? HTTPURLResponse)

    // Same-origin redirect IS followed — we land on /elsewhere with 200.
    XCTAssertEqual(http.statusCode, 200)
    XCTAssertEqual(http.url?.path, "/elsewhere")
  }
}
