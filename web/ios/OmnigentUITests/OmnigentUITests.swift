import XCTest

@MainActor
final class OmnigentUITests: XCTestCase {
  override func setUpWithError() throws {
    continueAfterFailure = false
  }

  func testLocalServerSnapshot() throws {
    let app = XCUIApplication(bundleIdentifier: "ai.omnigent.ios")
    setupSnapshot(app)
    let serverURL = try XCTUnwrap(
      ScreenshotConfiguration.serverURL(from: app),
      "Pass --omnigent-server-url or OMNIGENT_SCREENSHOT_APP_URL for screenshot tests."
    )
    app.launchArguments += [
      "--omnigent-server-url",
      serverURL,
    ]
    NSLog("Omnigent screenshot server URL: \(serverURL)")
    app.launchEnvironment["OMNIGENT_SCREENSHOT_APP_URL"] = serverURL
    app.launch()

    XCTAssertTrue(
      app.staticTexts["Server URL"].waitForExistence(timeout: 15),
      "Expected Omnigent to show the server selection screen before connecting."
    )
    snapshot("01-home", timeWaitingForIdle: 2)

    connectFromSetupIfNeeded(app, serverURL: serverURL)
    XCTAssertTrue(
      app.webViews.firstMatch.waitForExistence(timeout: 90),
      "Expected Omnigent to connect to \(serverURL) before taking screenshots."
    )

    snapshot("02-connected", timeWaitingForIdle: 5)
  }

  private func connectFromSetupIfNeeded(_ app: XCUIApplication, serverURL: String) {
    let setupLabel = app.staticTexts["Server URL"]
    guard setupLabel.waitForExistence(timeout: 5) else { return }

    let textField = app.textFields["server-url-field"]
    if textField.waitForExistence(timeout: 2),
      (textField.value as? String)?.contains(serverURL) != true
    {
      textField.tap()
      textField.press(forDuration: 0.8)
      if app.menuItems["Select All"].waitForExistence(timeout: 1) {
        app.menuItems["Select All"].tap()
      }
      textField.typeText(serverURL)
    }

    let connectButton = app.buttons["connect-button"]
    guard connectButton.waitForExistence(timeout: 2), connectButton.isEnabled else { return }
    connectButton.tap()

    let setupDismissed = XCTNSPredicateExpectation(
      predicate: NSPredicate(format: "exists == false"),
      object: setupLabel
    )
    _ = XCTWaiter.wait(for: [setupDismissed], timeout: 20)
  }
}

private enum ScreenshotConfiguration {
  static func serverURL(from app: XCUIApplication) -> String? {
    ProcessInfo.processInfo.environment["OMNIGENT_SCREENSHOT_APP_URL"]?.nonEmpty
      ?? app.launchArguments.omnigentServerURL
      ?? fastlaneLaunchArguments().omnigentServerURL
  }

  private static func fastlaneLaunchArguments() -> [String] {
    guard let cacheDirectory else { return [] }

    let path = cacheDirectory.appendingPathComponent("snapshot-launch_arguments.txt")
    guard let contents = try? String(contentsOf: path, encoding: .utf8) else {
      return []
    }
    return contents.omnigentShellTokens
  }

  private static var cacheDirectory: URL? {
    let cachePath = "Library/Caches/tools.fastlane"
    #if os(OSX)
      return URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent(cachePath)
    #elseif arch(i386) || arch(x86_64) || arch(arm64)
      guard let simulatorHostHome = ProcessInfo.processInfo.environment["SIMULATOR_HOST_HOME"]
      else {
        return nil
      }
      return URL(fileURLWithPath: simulatorHostHome).appendingPathComponent(cachePath)
    #else
      return nil
    #endif
  }
}

extension [String] {
  fileprivate var omnigentServerURL: String? {
    omnigentArgumentValue(after: "--omnigent-server-url")
      ?? compactMap { argument -> String? in
        guard argument.hasPrefix("--omnigent-server-url=") else { return nil }
        return String(argument.dropFirst("--omnigent-server-url=".count)).nonEmpty
      }.first
      ?? firstWebURL
  }

  fileprivate var firstWebURL: String? {
    first { argument in
      argument.hasPrefix("http://") || argument.hasPrefix("https://")
    }
  }

  fileprivate func omnigentArgumentValue(after argumentName: String) -> String? {
    guard let index = firstIndex(of: argumentName) else { return nil }
    let valueIndex = self.index(after: index)
    guard indices.contains(valueIndex) else { return nil }

    let value = self[valueIndex].trimmingCharacters(in: .whitespacesAndNewlines)
    return value.isEmpty ? nil : value
  }
}

extension String {
  fileprivate var nonEmpty: String? {
    let value = trimmingCharacters(in: .whitespacesAndNewlines)
    return value.isEmpty ? nil : value
  }

  fileprivate var omnigentShellTokens: [String] {
    guard
      let regex = try? NSRegularExpression(pattern: "(\\\".+?\\\"|'[^']+?'|\\S+)", options: [])
    else {
      return split(whereSeparator: \.isWhitespace).map(String.init)
    }

    let range = NSRange(location: 0, length: (self as NSString).length)
    return regex.matches(in: self, options: [], range: range).map { match in
      let token = (self as NSString).substring(with: match.range)
      if token.count >= 2,
        (token.hasPrefix("\"") && token.hasSuffix("\""))
          || (token.hasPrefix("'") && token.hasSuffix("'"))
      {
        return String(token.dropFirst().dropLast())
      }
      return token
    }
  }
}
