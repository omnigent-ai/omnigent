import UIKit
import WebKit
import XCTest

@testable import Omnigent

@MainActor
final class KeyboardViewportTests: XCTestCase {
  private let landscape = CGRect(x: 0, y: 0, width: 1210, height: 834)

  func testCompactHardwareToolbarKeepsFullHeight() {
    // Actual iPad hardware-toolbar frame with followsUndockedKeyboard enabled.
    let toolbar = CGRect(x: 496.5, y: 764.5, width: 217, height: 49.5)
    XCTAssertEqual(keyboardViewportHeight(in: landscape, keyboardFrame: toolbar), 834)
  }

  func testHardwareToolbarTransitionNeverShrinksTheViewport() {
    let frames = [
      CGRect(x: 0, y: 834, width: 1210, height: 0),
      CGRect(x: 0, y: 765.5, width: 1210, height: 68.5),
      CGRect(x: 496.5, y: 764.5, width: 217, height: 49.5),
    ]
    for frame in frames {
      XCTAssertEqual(
        keyboardViewportHeight(
          in: landscape, keyboardFrame: frame, hasIPadHardwareKeyboard: true), 834)
    }
  }

  func testHardwareKeyboardDoesNotSuppressTheFullSoftwareKeyboard() {
    let keyboard = CGRect(x: 0, y: 480, width: 1210, height: 354)
    XCTAssertEqual(
      keyboardViewportHeight(
        in: landscape, keyboardFrame: keyboard, hasIPadHardwareKeyboard: true), 480)
  }

  func testShortDockedFrameIsPreservedWithoutAnIPadHardwareKeyboard() {
    let frame = CGRect(x: 0, y: 765.5, width: 1210, height: 68.5)
    XCTAssertEqual(keyboardViewportHeight(in: landscape, keyboardFrame: frame), 765.5)
  }

  func testFloatingKeyboardKeepsFullHeightEvenAtBottomEdge() {
    let keyboard = CGRect(x: 880, y: 574, width: 320, height: 260)
    XCTAssertEqual(keyboardViewportHeight(in: landscape, keyboardFrame: keyboard), 834)
  }

  func testUndockedFullWidthKeyboardKeepsFullHeight() {
    let keyboard = CGRect(x: 0, y: 400, width: 1210, height: 300)
    XCTAssertEqual(keyboardViewportHeight(in: landscape, keyboardFrame: keyboard), 834)
  }

  func testDockedSoftwareKeyboardReservesSpaceInBothOrientations() {
    let keyboard = CGRect(x: 0, y: 480, width: 1210, height: 354)
    XCTAssertEqual(keyboardViewportHeight(in: landscape, keyboardFrame: keyboard), 480)
    let portrait = CGRect(x: 0, y: 0, width: 834, height: 1210)
    let portraitKeyboard = CGRect(x: 0, y: 850, width: 834, height: 360)
    XCTAssertEqual(keyboardViewportHeight(in: portrait, keyboardFrame: portraitKeyboard), 850)
  }

  func testDismissedKeyboardKeepsFullHeight() {
    let hidden = CGRect(x: 0, y: 834, width: 1210, height: 0)
    XCTAssertEqual(keyboardViewportHeight(in: landscape, keyboardFrame: hidden), 834)
    XCTAssertEqual(keyboardViewportHeight(in: landscape, keyboardFrame: .zero), 834)
  }

  func testNewDocumentReceivesGeometryBeforeSlowResourcesFinish() async throws {
    let defaultsName = "KeyboardViewportTests.\(UUID().uuidString)"
    let defaults = UserDefaults(suiteName: defaultsName)!
    defer { defaults.removePersistentDomain(forName: defaultsName) }
    let url = URL(string: "viewport-test://shell/page")!
    let model = WebViewModel()
    let view = OmnigentWebView(
      initialURL: url, model: model, settings: SettingsStore(defaults: defaults),
      databricksInternalFeaturesEnabled: false, loadFailed: { _, _ in }, loadSucceeded: {},
      pushServerPicker: {}, requestSwitchServer: { _ in }, openServerSetup: {})
    let coordinator = view.makeCoordinator()
    let configuration = WKWebViewConfiguration()
    let content = configuration.userContentController
    content.add(coordinator, name: "omnigentNative")
    content.addUserScript(
      WKUserScript(
        source: OmnigentWebView.nativeBridgeScript(managesWorkspace: false),
        injectionTime: .atDocumentStart, forMainFrameOnly: true))
    let resources = PendingViewportResources()
    configuration.setURLSchemeHandler(resources, forURLScheme: "viewport-test")
    let webView = AccessoryFreeWebView(frame: landscape, configuration: configuration)
    let window = try XCTUnwrap(
      UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }
        .flatMap(\.windows).first { $0.isKeyWindow })
    window.addSubview(webView)
    model.webView = webView
    coordinator.attach(webView)
    defer {
      coordinator.detach()
      content.removeScriptMessageHandler(forName: "omnigentNative")
      content.removeScriptMessageHandler(forName: "viewportTest")
      webView.removeFromSuperview()
    }
    let published = expectation(description: "geometry in each new document")
    published.expectedFulfillmentCount = 2
    let receiver = ViewportMessageReceiver { message in
      guard let size = message.body as? [String: Double] else {
        XCTFail("Missing native geometry")
        return
      }
      XCTAssertEqual(size["width"], 1210)
      XCTAssertEqual(size["height"], 834)
      XCTAssertTrue(webView.isLoading, "Geometry must arrive before pending resources finish")
      published.fulfill()
      if resources.pages == 1 {
        coordinator.load(url.appendingPathComponent("next"), in: webView)
      }
    }
    content.add(receiver, name: "viewportTest")
    // Prime the native cache before either document has a bridge.
    webView.emitKeyboardViewport()
    coordinator.load(url, in: webView)
    await fulfillment(of: [published], timeout: 30)
  }

  func testNativePanLockLeavesInnerScrollersAndOtherDocumentsScrollable() {
    let defaultsName = "KeyboardViewportTests.\(UUID().uuidString)"
    let defaults = UserDefaults(suiteName: defaultsName)!
    defer { defaults.removePersistentDomain(forName: defaultsName) }
    let model = WebViewModel()
    let view = OmnigentWebView(
      initialURL: URL(string: "https://server.invalid")!, model: model,
      settings: SettingsStore(defaults: defaults), databricksInternalFeaturesEnabled: false,
      loadFailed: { _, _ in }, loadSucceeded: {}, pushServerPicker: {},
      requestSwitchServer: { _ in }, openServerSetup: {})
    let coordinator = view.makeCoordinator()
    let webView = WKWebView(frame: landscape)
    model.webView = webView
    coordinator.attach(webView)
    defer { coordinator.detach() }

    webView.scrollView.isScrollEnabled = false
    webView.scrollView.contentOffset = CGPoint(x: 0, y: 68.5)
    coordinator.scrollViewDidScroll(webView.scrollView)
    XCTAssertEqual(webView.scrollView.contentOffset, .zero)

    let innerScroller = UIScrollView()
    innerScroller.contentOffset = CGPoint(x: 0, y: 68.5)
    coordinator.scrollViewDidScroll(innerScroller)
    XCTAssertEqual(innerScroller.contentOffset.y, 68.5)

    coordinator.webView(webView, didStartProvisionalNavigation: nil)
    XCTAssertFalse(webView.scrollView.isScrollEnabled)
    coordinator.webView(webView, didFailProvisionalNavigation: nil, withError: URLError(.cancelled))
    XCTAssertFalse(webView.scrollView.isScrollEnabled)
    coordinator.webView(webView, didCommit: nil)
    XCTAssertTrue(webView.scrollView.isScrollEnabled)
    webView.scrollView.contentOffset = CGPoint(x: 0, y: 68.5)
    coordinator.scrollViewDidScroll(webView.scrollView)
    XCTAssertEqual(webView.scrollView.contentOffset.y, 68.5)
  }
}

@MainActor
private final class ViewportMessageReceiver: NSObject, WKScriptMessageHandler {
  let receive: (WKScriptMessage) -> Void
  init(receive: @escaping (WKScriptMessage) -> Void) { self.receive = receive }
  func userContentController(
    _ userContentController: WKUserContentController, didReceive message: WKScriptMessage
  ) { receive(message) }
}

@MainActor
private final class PendingViewportResources: NSObject, WKURLSchemeHandler {
  var pages = 0
  func webView(_ webView: WKWebView, start urlSchemeTask: WKURLSchemeTask) {
    let url = urlSchemeTask.request.url!
    // Keep the image pending so navigation cannot reach didFinish.
    guard !url.path.hasSuffix("slow.png") else { return }
    pages += 1
    let html = """
      <!doctype html><html><head></head><body>
      <img src="/slow.png">
      <script>
        const publish = () => {
          const size = window.omnigentNative.getKeyboardViewport();
          if (size) window.webkit.messageHandlers.viewportTest.postMessage(size);
        };
        window.omnigentNative.onKeyboardViewportChanged(publish);
        publish();
      </script></body></html>
      """
    urlSchemeTask.didReceive(
      URLResponse(
        url: url, mimeType: "text/html", expectedContentLength: -1, textEncodingName: "utf-8"))
    urlSchemeTask.didReceive(Data(html.utf8))
    urlSchemeTask.didFinish()
  }
  func webView(_ webView: WKWebView, stop urlSchemeTask: WKURLSchemeTask) {}
}
