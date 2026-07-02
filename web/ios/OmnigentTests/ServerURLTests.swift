import XCTest

@testable import Omnigent

final class ServerURLTests: XCTestCase {
  func testReleasePolicyDefaultsBareHostToHTTPS() throws {
    let url = try ServerURL.normalize("example.com", allowsInsecureHTTP: false)
    XCTAssertEqual(url.absoluteString, "https://example.com")
  }

  func testDebugPolicyDefaultsBareHostToHTTP() throws {
    let url = try ServerURL.normalize("localhost:6767", allowsInsecureHTTP: true)
    XCTAssertEqual(url.absoluteString, "http://localhost:6767")
  }

  func testReleasePolicyRejectsHTTP() {
    XCTAssertThrowsError(try ServerURL.normalize("http://example.com", allowsInsecureHTTP: false)) {
      error in
      XCTAssertEqual(error as? ServerURLError, .insecureHTTPNotAllowed)
    }
  }

  func testRejectsNonWebSchemes() {
    XCTAssertThrowsError(try ServerURL.normalize("ftp://example.com", allowsInsecureHTTP: true)) {
      error in
      XCTAssertEqual(error as? ServerURLError, .unsupportedScheme("ftp"))
    }
  }
}
