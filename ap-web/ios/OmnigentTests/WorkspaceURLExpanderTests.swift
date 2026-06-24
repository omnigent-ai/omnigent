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

    XCTAssertEqual(expanded.absoluteString, "https://workspace.example.com/ml/omnigents")
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
    let original = URL(string: "https://workspace.example.com/ml/omnigents")!
    let expanded = await WorkspaceURLExpander.expandIfNeeded(original, session: stubbedSession())

    XCTAssertEqual(expanded, original)
    XCTAssertNil(URLProtocolStub.handler)
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
