import Foundation
import XCTest

@testable import Omnigent

final class WorkspaceURLExpanderTests: XCTestCase {
  override func setUp() {
    super.setUp()
    URLProtocolStub.handler = nil
  }

  func testExpandsBareDatabricksWorkspaceRoot() async {
    URLProtocolStub.handler = { request in
      let response = HTTPURLResponse(
        url: request.url!,
        statusCode: 200,
        httpVersion: nil,
        headerFields: ["server": "databricks"]
      )!
      return (response, Data())
    }

    let expanded = await WorkspaceURLExpander.expandIfNeeded(
      URL(string: "https://workspace.example.com")!,
      session: stubbedSession()
    )

    XCTAssertEqual(expanded.absoluteString, "https://workspace.example.com/omnigent")
  }

  func testLeavesNonWorkspaceRootUnchanged() async {
    URLProtocolStub.handler = { request in
      let response = HTTPURLResponse(
        url: request.url!,
        statusCode: 200,
        httpVersion: nil,
        headerFields: ["server": "nginx"]
      )!
      return (response, Data())
    }

    let original = URL(string: "https://app.example.com")!
    let expanded = await WorkspaceURLExpander.expandIfNeeded(original, session: stubbedSession())

    XCTAssertEqual(expanded, original)
  }

  func testLeavesURLsWithPathsUnchangedWithoutProbe() async {
    let original = URL(string: "https://workspace.example.com/omnigent")!
    let expanded = await WorkspaceURLExpander.expandIfNeeded(original, session: stubbedSession())

    XCTAssertEqual(expanded, original)
    XCTAssertNil(URLProtocolStub.handler)
  }

  func testLeavesDatabricksAppsHostUnchangedWithoutProbe() async {
    URLProtocolStub.handler = { _ in
      XCTFail("Databricks Apps must not be probed for a workspace mount")
      throw URLError(.badServerResponse)
    }
    for raw in [
      "https://my-app-123.aws.databricksapps.com",
      "https://databricksapps.com",
      "https://MY-APP.AZURE.DATABRICKSAPPS.COM",
    ] {
      let original = URL(string: raw)!
      let expanded = await WorkspaceURLExpander.expandIfNeeded(original, session: stubbedSession())
      XCTAssertEqual(expanded, original)
    }
  }

  func testAppsLookalikesAreStillProbed() async {
    URLProtocolStub.handler = { request in
      XCTAssertEqual(request.httpMethod, "HEAD")
      let response = HTTPURLResponse(
        url: request.url!, statusCode: 200, httpVersion: nil,
        headerFields: ["server": "databricks"]
      )!
      return (response, Data())
    }
    for raw in [
      "https://databricksapps.com.example.org",
      "https://notdatabricksapps.com",
    ] {
      let expanded = await WorkspaceURLExpander.expandIfNeeded(
        URL(string: raw)!, session: stubbedSession())
      XCTAssertEqual(expanded.absoluteString, raw + "/omnigent")
    }
  }

  func testRejectsResponseFromDifferentOrigin() async {
    // Simulates a consented host 3xx-redirecting the HEAD probe to a
    // different origin (e.g. a local-network service). Even if the redirect
    // were followed, the response.url origin check must reject it so the app
    // never trusts a host the user did not approve.
    URLProtocolStub.handler = { request in
      let response = HTTPURLResponse(
        url: URL(string: "http://192.168.1.5:8080")!,
        statusCode: 200,
        httpVersion: nil,
        headerFields: ["server": "databricks"]
      )!
      return (response, Data())
    }

    let original = URL(string: "https://workspace.example.com")!
    let expanded = await WorkspaceURLExpander.expandIfNeeded(original, session: stubbedSession())

    XCTAssertEqual(expanded, original)
  }

  private func stubbedSession() -> URLSession {
    let configuration = URLSessionConfiguration.ephemeral
    configuration.protocolClasses = [URLProtocolStub.self]
    return URLSession(configuration: configuration)
  }
}

private final class URLProtocolStub: URLProtocol {
  static var handler: ((URLRequest) throws -> (HTTPURLResponse, Data))?

  override class func canInit(with request: URLRequest) -> Bool {
    true
  }

  override class func canonicalRequest(for request: URLRequest) -> URLRequest {
    request
  }

  override func startLoading() {
    guard let handler = Self.handler else {
      client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse))
      return
    }

    do {
      let (response, data) = try handler(request)
      client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
      client?.urlProtocol(self, didLoad: data)
      client?.urlProtocolDidFinishLoading(self)
    } catch {
      client?.urlProtocol(self, didFailWithError: error)
    }
  }

  override func stopLoading() {}
}

/// Domain-matched bare-root rewriting (no probe), mirroring Android's
/// `OriginsWorkspaceUiUrlTest`.
final class WorkspaceMountURLTests: XCTestCase {
  private func mount(_ raw: String) -> String? {
    guard let url = URL(string: raw) else { return nil }
    return WorkspaceURLExpander.workspaceUIURL(forBareRoot: url)?.absoluteString
  }

  func testRewritesBareWorkspaceRoots() {
    XCTAssertEqual(
      mount("https://dbc-1234.cloud.databricks.com"),
      "https://dbc-1234.cloud.databricks.com/omnigent")
    XCTAssertEqual(
      mount("https://dbc-1234.cloud.databricks.com/"),
      "https://dbc-1234.cloud.databricks.com/omnigent")
    XCTAssertEqual(
      mount("https://adb-99.azuredatabricks.net/"), "https://adb-99.azuredatabricks.net/omnigent")
    XCTAssertEqual(mount("https://databricks.com/"), "https://databricks.com/omnigent")
    XCTAssertEqual(mount("https://azuredatabricks.net/"), "https://azuredatabricks.net/omnigent")
  }

  /// `?o=<org>` selects which workspace the request lands in, so it must survive.
  func testPreservesQueryAndFragment() {
    XCTAssertEqual(
      mount("https://dbc-1234.cloud.databricks.com/?o=987654321"),
      "https://dbc-1234.cloud.databricks.com/omnigent?o=987654321")
    XCTAssertEqual(
      mount("https://dbc-1234.cloud.databricks.com/?o=1#frag"),
      "https://dbc-1234.cloud.databricks.com/omnigent?o=1#frag")
  }

  func testPreservesPortAndNormalizesHostCase() {
    XCTAssertEqual(
      mount("https://dbc-1234.cloud.databricks.com:8443/"),
      "https://dbc-1234.cloud.databricks.com:8443/omnigent")
    XCTAssertEqual(
      mount("https://DBC-1234.Cloud.DataBricks.Com/"),
      "https://DBC-1234.Cloud.DataBricks.Com/omnigent")
  }

  /// A URL that already carries a path is a deliberate deep link.
  func testLeavesNonRootPathsAlone() {
    XCTAssertNil(mount("https://dbc-1234.cloud.databricks.com/omnigent"))
    XCTAssertNil(mount("https://dbc-1234.cloud.databricks.com/c/abc"))
    XCTAssertNil(mount("https://dbc-1234.cloud.databricks.com/ml/omnigents"))
  }

  /// Apps serve their own app at the root and have no workspace mount.
  func testLeavesDatabricksAppsAndOtherHostsAlone() {
    XCTAssertNil(mount("https://my-app.databricksapps.com/"))
    XCTAssertNil(mount("https://databricksapps.com/"))
    XCTAssertNil(mount("https://MY-APP.AWS.DATABRICKSAPPS.COM/"))
    XCTAssertNil(mount("https://example.com/"))
    XCTAssertNil(mount("https://localhost:8000/"))
    // Lookalike host: must match on a dot boundary.
    XCTAssertNil(mount("https://databricks.com.evil.example/"))
    XCTAssertNil(mount("https://azuredatabricks.net.example.org/"))
    XCTAssertNil(mount("https://notdatabricks.com/"))
    XCTAssertNil(mount("https://notazuredatabricks.net/"))
  }

  func testRejectsNonHTTPSchemes() {
    XCTAssertNil(mount("omnigent://dbc-1234.cloud.databricks.com/"))
    XCTAssertNil(mount("file:///tmp"))
  }
}
