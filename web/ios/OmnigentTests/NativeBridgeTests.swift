import WebKit
import XCTest

@testable import Omnigent

@MainActor
final class NativeBridgeTests: XCTestCase {
  func testNavigationDelegateResetsPinnedDocumentsButPreservesAuthReturn() {
    let (coordinator, webView) = makeCoordinator()
    let clock = ManualWatchdogClock()
    var failures = 0
    coordinator.livenessWatchdog = MobileLivenessWatchdog(schedule: clock.schedule) {
      failures += 1
    }
    let pinned = URL(string: "https://server.example.com/app")!
    coordinator.load(pinned, in: webView)
    coordinator.mainFrameNavigationStarted(url: pinned)
    XCTAssertTrue(coordinator.livenessWatchdog.protocolReady(version: 1, expectedVersion: 1))

    coordinator.mainFrameNavigationStarted(
      url: URL(string: "https://server.example.com/replacement"))
    clock.advance(by: 10)
    coordinator.livenessWatchdog.receivedHeartbeat()  // outgoing document
    clock.advance(by: 10)
    XCTAssertEqual(failures, 1)

    XCTAssertTrue(coordinator.livenessWatchdog.protocolReady(version: 1, expectedVersion: 1))
    coordinator.mainFrameNavigationStarted(url: URL(string: "https://idp.example.com/login"))
    coordinator.mainFrameNavigationStarted(url: pinned)
    clock.advance(by: 14)
    coordinator.livenessWatchdog.receivedHeartbeat()
    clock.advance(by: 14)
    XCTAssertEqual(failures, 1)
  }

  func testCoordinatorResumePreservesCompatibilityDuringGrace() {
    let (coordinator, webView) = makeCoordinator()
    let clock = ManualWatchdogClock()
    var failures = 0
    coordinator.livenessWatchdog = MobileLivenessWatchdog(schedule: clock.schedule) {
      failures += 1
    }
    let pinned = URL(string: "https://server.example.com/app")!
    coordinator.load(pinned, in: webView)
    XCTAssertTrue(coordinator.livenessWatchdog.protocolReady(version: 1, expectedVersion: 1))
    coordinator.livenessWatchdog.setActive(false)
    coordinator.appBecameActive(url: pinned)
    clock.advance(by: 14)
    coordinator.livenessWatchdog.receivedHeartbeat()
    clock.advance(by: 14)
    XCTAssertEqual(failures, 0)
  }

  func testRendererTerminationRoutesToFullScreenRecovery() {
    let suite = "NativeBridgeTests.\(UUID().uuidString)"
    let defaults = UserDefaults(suiteName: suite)!
    defer { defaults.removePersistentDomain(forName: suite) }
    let url = URL(string: "https://server.example.com/app")!
    var failure: (URL, String)?
    let parent = OmnigentWebView(
      serverURL: url,
      initialURL: url,
      managedServers: [],
      recentServers: [],
      model: WebViewModel(),
      settings: SettingsStore(defaults: defaults),
      switchToServer: { _ in },
      connectToNewServer: {},
      loadFailed: { failure = ($0, $1) },
      loadSucceeded: {})
    let coordinator = OmnigentWebView.Coordinator(parent)

    coordinator.webViewWebContentProcessDidTerminate(WKWebView())

    XCTAssertEqual(failure?.0, url)
    XCTAssertEqual(failure?.1, "The server UI process stopped unexpectedly.")
  }

  func testGeneratedBridgeExecutesLivePickerRoundTripInWKWebView() async throws {
    let handler = MessageHandler()
    let configuration = WKWebViewConfiguration()
    configuration.userContentController.add(handler, name: "omnigentNative")
    let webView = WKWebView(frame: .zero, configuration: configuration)
    webView.loadHTMLString("<html><head></head><body></body></html>", baseURL: nil)
    try await Task.sleep(for: .milliseconds(100))

    _ = try await webView.evaluateJavaScript(OmnigentWebView.nativeBridgeScript())
    _ = try await webView.evaluateJavaScript("window.omnigentNative.nativeWebReady(1)")
    let promise = Task {
      try await webView.callAsyncJavaScript(
        "return await window.omnigentNative.getServerPicker();",
        arguments: [:],
        contentWorld: .page
      )
    }

    let request = try await handler.nextMessage(method: "getServerPicker")
    let requestID = try XCTUnwrap(request["requestId"] as? Int)
    _ = try await webView.evaluateJavaScript(
      "window.__omnigentNativeEmitServerPicker(\(requestID), {currentOrigin:'https://current.example.com',currentServerUrl:'https://current.example.com/app',managedServers:['https://managed.example.com'],recentServers:['https://recent.example.com']})"
    )
    let result = try await promise.value as? [String: Any]

    XCTAssertEqual(result?["currentServerUrl"] as? String, "https://current.example.com/app")
    XCTAssertTrue(handler.messages.contains { $0["method"] as? String == "nativeWebReady" })
  }

  private func makeCoordinator() -> (OmnigentWebView.Coordinator, WKWebView) {
    let url = URL(string: "https://server.example.com/app")!
    let parent = OmnigentWebView(
      serverURL: url, initialURL: url, managedServers: [], recentServers: [],
      model: WebViewModel(), settings: SettingsStore(), switchToServer: { _ in },
      connectToNewServer: {}, loadFailed: { _, _ in }, loadSucceeded: {})
    return (OmnigentWebView.Coordinator(parent), WKWebView())
  }
}

@MainActor
private final class MessageHandler: NSObject, WKScriptMessageHandler {
  var messages: [[String: Any]] = []

  func userContentController(
    _ userContentController: WKUserContentController, didReceive message: WKScriptMessage
  ) {
    if let body = message.body as? [String: Any] { messages.append(body) }
  }

  func nextMessage(method: String) async throws -> [String: Any] {
    for _ in 0..<100 {
      if let message = messages.first(where: { $0["method"] as? String == method }) {
        return message
      }
      try await Task.sleep(for: .milliseconds(10))
    }
    throw CocoaError(.coderReadCorrupt)
  }
}
