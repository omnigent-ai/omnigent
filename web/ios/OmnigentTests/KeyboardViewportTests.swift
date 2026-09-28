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
    XCTAssertTrue(webView.scrollView.isScrollEnabled)
    webView.scrollView.contentOffset = CGPoint(x: 0, y: 68.5)
    coordinator.scrollViewDidScroll(webView.scrollView)
    XCTAssertEqual(webView.scrollView.contentOffset.y, 68.5)
  }
}
