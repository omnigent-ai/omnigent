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

  // MARK: - Deep links (F-CR-6 end-to-end)

  /// Launch the app with a deep link routed through the REAL handler via the
  /// DEBUG-only `--omnigent-open-url` launch argument (see `AppRootView`). The
  /// argument drives the same `handleDeepLink`/`DeepLink.parse` path that
  /// `.onOpenURL` would. This is the CI-friendly delivery: XCUITest can't
  /// reliably hand a custom-scheme URL to the app under test on this toolchain
  /// (`XCUIApplication.open` drops custom schemes), so a launch argument is the
  /// deterministic seam — no simulator URL routing, no Safari handoff flakiness.
  ///
  /// `--omnigent-reset-state` (also DEBUG-only) wipes persisted server
  /// state so each test starts with NO known/recent server — otherwise a
  /// prior test's saved `localhost:8000` would make a deep link to the same
  /// host route in-place (no consent alert), making the tests order-dependent.
  private func launchApp(openURL link: String) -> XCUIApplication {
    let app = XCUIApplication(bundleIdentifier: "ai.omnigent.ios")
    app.launchArguments += ["--omnigent-open-url", link, "--omnigent-reset-state"]
    app.launch()
    return app
  }

  /// A valid `omnigent://` deep link to a server the user has never connected
  /// to (a cold-started test app has no recents) must surface the consent
  /// alert — the one surface a page-click can't forge — proving the link made
  /// it through `DeepLink.parse` and reached `handleDeepLink`.
  func testValidDeepLinkShowsConsent() throws {
    // A real 32-char-hex conversation id — the canonical form the API emits.
    let app = launchApp(
      openURL: "omnigent://localhost:8000/c/e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9")

    let alert = app.alerts["Open this Omnigent link?"]
    XCTAssertTrue(
      alert.waitForExistence(timeout: 10),
      "A valid deep link to an unknown server should show the consent alert."
    )
    XCTAssertTrue(alert.buttons["Open"].exists, "Consent alert should offer Open.")
    XCTAssertTrue(alert.buttons["Cancel"].exists, "Consent alert should offer Cancel.")
    alert.buttons["Cancel"].tap()
  }

  /// A deep link that smuggles a query past the "/c/<id> only" shape via a
  /// percent-encoded `?` (`%3F`) must be REJECTED: `DeepLink.parse` returns nil,
  /// `handleDeepLink` returns early, and NO consent alert appears. Before the
  /// grammar fix the decoded `?` rode inside the id and the link was accepted.
  func testSmuggledQueryDeepLinkIsRejected() throws {
    // `%3F` decodes to a literal `?` in URL.path — the smuggling vector from
    // F-CR-6. The id grammar (hex only) rejects the `?`, so no alert.
    let app = launchApp(
      openURL:
        "omnigent://localhost:8000/c/e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9%3Fview=terminal")
    // Sanity: the app reached a responsive state where a VALID link would have
    // shown the alert — so the alert's absence means the link was rejected, not
    // that delivery silently failed.
    assertAppReachedSetup(app)

    let alert = app.alerts["Open this Omnigent link?"]
    XCTAssertFalse(
      alert.waitForExistence(timeout: 6),
      "A deep link with a smuggled query (encoded `?`) must be rejected — no consent alert."
    )
  }

  /// A deep link with a smuggled fragment (`%23` → `#`) is likewise rejected.
  func testSmuggledFragmentDeepLinkIsRejected() throws {
    let app = launchApp(
      openURL: "omnigent://localhost:8000/c/e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9%23evil")
    assertAppReachedSetup(app)

    let alert = app.alerts["Open this Omnigent link?"]
    XCTAssertFalse(
      alert.waitForExistence(timeout: 6),
      "A deep link with a smuggled fragment (encoded `#`) must be rejected — no consent alert."
    )
  }

  /// A deep link whose id isn't a canonical conversation id (e.g. a short
  /// stub like `conv_abc`) is still ACCEPTED: the validator is a denylist, not
  /// a grammar — it rejects only smuggled structure, and leaves id-format
  /// validity to the SPA's own router. So a benign non-canonical id reaches
  /// `handleDeepLink` and (for an unknown server) shows the consent alert.
  func testBenignNonCanonicalIdIsAccepted() throws {
    // `conv_abc` isn't a real server id, but it smuggles no structure, so the
    // parser forwards it — the SPA decides validity. Expect the consent alert
    // (unknown server), proving the link was ACCEPTED.
    let app = launchApp(openURL: "omnigent://localhost:8000/c/conv_abc")

    let alert = app.alerts["Open this Omnigent link?"]
    XCTAssertTrue(
      alert.waitForExistence(timeout: 10),
      "A deep link with a benign (non-smuggling) id should be accepted and show the consent alert."
    )
    alert.buttons["Cancel"].tap()
  }

  /// A deep link with an encoded `..` (`%2e%2e`) — a path-traversal-shaped id —
  /// is rejected by the grammar (no `.` in a conversation id).
  func testEncodedDotDotDeepLinkIsRejected() throws {
    let app = launchApp(openURL: "omnigent://localhost:8000/c/%2e%2e")
    assertAppReachedSetup(app)

    let alert = app.alerts["Open this Omnigent link?"]
    XCTAssertFalse(
      alert.waitForExistence(timeout: 6),
      "A deep link with an encoded `..` must be rejected — no consent alert."
    )
  }

  /// A deep link with a control character (`%00` NUL) in the id is rejected.
  func testControlCharDeepLinkIsRejected() throws {
    let app = launchApp(
      openURL: "omnigent://localhost:8000/c/e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9%00x")
    assertAppReachedSetup(app)

    let alert = app.alerts["Open this Omnigent link?"]
    XCTAssertFalse(
      alert.waitForExistence(timeout: 6),
      "A deep link with a control character in the id must be rejected — no consent alert."
    )
  }

  /// Sanity for the rejection tests: assert the app launched and is showing the
  /// setup page (or is otherwise responsive), so a VALID link would have had the
  /// chance to surface the consent alert. Without this, a rejection test could
  /// falsely pass if the launch-argument seam silently failed to fire.
  private func assertAppReachedSetup(_ app: XCUIApplication) {
    // Either the setup page's "Server URL" label is visible, OR — if the seam
    // routed a link that the app is processing — the app is at least responsive
    // (not crashed). The setup page is the expected landing for a reset-state
    // launch, so require it.
    XCTAssertTrue(
      app.staticTexts["Server URL"].waitForExistence(timeout: 15),
      "App should reach the setup page for a reset-state launch (delivery sanity check)."
    )
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
