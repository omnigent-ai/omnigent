import XCTest

@testable import Omnigent

@MainActor
final class DownloadFilenameTests: XCTestCase {
  func testKeepsServerSuggestedFilename() {
    XCTAssertEqual(
      OmnigentWebView.Coordinator.safeDownloadFilename("terminal-recording.webm"),
      "terminal-recording.webm"
    )
  }

  func testDropsPathComponents() {
    XCTAssertEqual(
      OmnigentWebView.Coordinator.safeDownloadFilename("../../private/report.pdf"),
      "report.pdf"
    )
  }

  func testFallsBackForInvalidFilename() {
    XCTAssertEqual(OmnigentWebView.Coordinator.safeDownloadFilename(".."), "download")
  }
}
