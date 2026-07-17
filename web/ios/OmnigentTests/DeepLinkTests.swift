import XCTest

@testable import Omnigent

final class DeepLinkTests: XCTestCase {
  func testParsesLoopbackHostWithPortAsHTTP() {
    let dl = DeepLink.parse(URL(string: "omnigent://localhost:8000/c/conv_abc")!)
    XCTAssertEqual(dl?.origin, "http://localhost:8000")
    XCTAssertEqual(dl?.path, "/c/conv_abc")

    let dl2 = DeepLink.parse(URL(string: "omnigent://127.0.0.1:8000/c/x")!)
    XCTAssertEqual(dl2?.origin, "http://127.0.0.1:8000")
    XCTAssertEqual(dl2?.path, "/c/x")
  }

  func testParsesRemoteHostAsHTTPS() {
    let dl = DeepLink.parse(URL(string: "omnigent://my-workspace.cloud.databricks.com/c/x")!)
    XCTAssertEqual(dl?.origin, "https://my-workspace.cloud.databricks.com")
    XCTAssertEqual(dl?.path, "/c/x")
  }

  func testPreservesNonDefaultPortOnRemoteHost() {
    let dl = DeepLink.parse(URL(string: "omnigent://example.com:8443/c/x")!)
    XCTAssertEqual(dl?.origin, "https://example.com:8443")
    XCTAssertEqual(dl?.path, "/c/x")
  }

  func testParsesIPv6LoopbackAsHTTP() {
    let dl = DeepLink.parse(URL(string: "omnigent://[::1]:8000/c/x")!)
    XCTAssertEqual(dl?.origin, "http://[::1]:8000")
    XCTAssertEqual(dl?.path, "/c/x")
  }

  func testStripsTrailingSlashFromPath() {
    // Foundation already strips it; the parser normalizes regardless so the
    // forwarded path is always `/c/<id>` (react-router matches that exactly).
    let dl = DeepLink.parse(URL(string: "omnigent://localhost:8000/c/conv_abc/")!)
    XCTAssertEqual(dl?.path, "/c/conv_abc")
  }

  func testRejectsNonOmnigentScheme() {
    XCTAssertNil(DeepLink.parse(URL(string: "https://localhost:8000/c/x")!))
    XCTAssertNil(DeepLink.parse(URL(string: "vscode://localhost/c/x")!))
  }

  func testRejectsLinkWithNoHost() {
    XCTAssertNil(DeepLink.parse(URL(string: "omnigent://")!))
    XCTAssertNil(DeepLink.parse(URL(string: "omnigent:///c/x")!))
  }

  func testRejectsNonConversationPaths() {
    XCTAssertNil(DeepLink.parse(URL(string: "omnigent://localhost:8000/inbox")!))
    XCTAssertNil(DeepLink.parse(URL(string: "omnigent://localhost:8000/settings/appearance")!))
    XCTAssertNil(DeepLink.parse(URL(string: "omnigent://localhost:8000/c/")!))
    XCTAssertNil(DeepLink.parse(URL(string: "omnigent://localhost:8000/c/a/b")!))
    XCTAssertNil(DeepLink.parse(URL(string: "omnigent://localhost:8000/")!))
  }
}
