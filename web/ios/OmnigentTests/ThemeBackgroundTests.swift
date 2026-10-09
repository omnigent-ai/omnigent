import SwiftUI
import UIKit
import XCTest

@testable import Omnigent

@MainActor
final class ThemeBackgroundTests: XCTestCase {
  func testExplicitThemesColorTheWindowAndHostingView() throws {
    for source in [ThemeSource.light, .dark] {
      let window = makeWindow()
      ThemeController.apply(source, to: window)

      XCTAssertEqual(window.overrideUserInterfaceStyle, source.userInterfaceStyle)
      try assertBackgrounds(window, scheme: XCTUnwrap(source.colorScheme))
    }
  }

  func testSystemThemeBackgroundsResolveForBothAppearances() throws {
    let window = makeWindow()
    ThemeController.apply(.system, to: window)

    XCTAssertEqual(window.overrideUserInterfaceStyle, .unspecified)
    try assertBackgrounds(window, scheme: .light)
    try assertBackgrounds(window, scheme: .dark)
  }

  func testSwitchingThemesUpdatesAnExistingWindow() throws {
    let window = makeWindow()
    ThemeController.apply(.dark, to: window)
    try assertBackgrounds(window, scheme: .dark)

    ThemeController.apply(.light, to: window)
    XCTAssertEqual(window.overrideUserInterfaceStyle, .light)
    try assertBackgrounds(window, scheme: .light)

    ThemeController.apply(.system, to: window)
    XCTAssertEqual(window.overrideUserInterfaceStyle, .unspecified)
    try assertBackgrounds(window, scheme: .dark)
  }

  func testOverlayWindowsKeepTheirTransparentBackgrounds() {
    let window = makeWindow()
    window.windowLevel = UIWindow.Level(rawValue: UIWindow.Level.normal.rawValue + 1)
    window.backgroundColor = .clear
    window.rootViewController?.view.backgroundColor = .clear

    for source in ThemeSource.allCases {
      ThemeController.apply(source, to: window)

      XCTAssertEqual(window.overrideUserInterfaceStyle, source.userInterfaceStyle)
      XCTAssertEqual(window.backgroundColor, .clear)
      XCTAssertEqual(window.rootViewController?.view.backgroundColor, .clear)
    }
  }

  private func makeWindow() -> UIWindow {
    let window = UIWindow(frame: CGRect(x: 0, y: 0, width: 390, height: 844))
    window.rootViewController = UIHostingController(rootView: Color.clear)
    window.backgroundColor = .white
    window.rootViewController?.view.backgroundColor = .white
    return window
  }

  private func assertBackgrounds(_ window: UIWindow, scheme: ColorScheme) throws {
    let traits = UITraitCollection(userInterfaceStyle: scheme == .dark ? .dark : .light)
    let expected = UIColor(DesignTokens.background(scheme))
    let windowBackground = try XCTUnwrap(window.backgroundColor)
    let rootBackground = try XCTUnwrap(window.rootViewController?.view.backgroundColor)

    XCTAssertEqual(windowBackground.resolvedColor(with: traits), expected)
    XCTAssertEqual(rootBackground.resolvedColor(with: traits), expected)
  }
}
