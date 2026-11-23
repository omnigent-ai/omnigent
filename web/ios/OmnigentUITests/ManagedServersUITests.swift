import XCTest

/// UI smoke test for administrator-preset server URLs, driven through the
/// DEBUG-only `--omnigent-managed-servers` launch seam.
///
/// The seam exists because there is no way to fake a pushed managed app
/// configuration locally: the real payload arrives over declarative device
/// management, so a simulator run can never receive one. What this test covers
/// is everything downstream of the decoder — that preset servers are offered
/// under their own heading, and that tapping one connects. The decoding and
/// validation itself is covered by `ManagedConfigurationTests`.
@MainActor
final class ManagedServersUITests: XCTestCase {
  func testPresetServerIsOfferedAndConnects() throws {
    let marker = "OMNIGENT_MANAGED_SERVER_OK"
    let server = try MockHTTPServer { _, _ in
      (
        200, ["content-type": "text/html"],
        "<html><body>\(marker)</body></html>".data(using: .utf8)!
      )
    }
    let presetURL = "http://localhost:\(server.port)"

    let app = XCUIApplication(bundleIdentifier: "ai.omnigent.ios")
    app.launchArguments += [
      "--omnigent-managed-servers", presetURL,
      // No saved server, so the connect screen is what we land on.
      "--omnigent-reset-state",
    ]
    app.launch()

    // A preset server must not connect on its own — the user still chooses.
    XCTAssertTrue(
      app.staticTexts["Provided by your organization"].waitForExistence(timeout: 15),
      "Expected preset servers to be offered under their own heading.")

    let row = app.buttons.matching(identifier: "managed-servers-row").firstMatch
    XCTAssertTrue(row.waitForExistence(timeout: 5), "Expected a row for \(presetURL).")
    row.tap()

    XCTAssertTrue(
      app.webViews.staticTexts[marker].waitForExistence(timeout: 30),
      "Expected tapping a preset server to load it (marker \(marker)).")
  }

  /// A long server list scrolls under the URL field without hiding it, and the
  /// keyboard toolbar's Hide Keyboard button ends editing.
  func testServerListScrollsUnderFieldAndKeyboardHides() throws {
    let servers = (1...8).map { "https://team\($0).example.com" }.joined(separator: ",")
    let app = XCUIApplication(bundleIdentifier: "ai.omnigent.ios")
    app.launchArguments += ["--omnigent-managed-servers", servers, "--omnigent-reset-state"]
    app.launch()

    let field = app.textFields["server-url-field"]
    XCTAssertTrue(field.waitForExistence(timeout: 15))
    let fieldY = field.frame.minY
    let firstRow = app.buttons["https://team1.example.com"]
    let firstRowY = firstRow.frame.minY
    app.buttons.matching(identifier: "managed-servers-row").element(boundBy: 2).swipeUp()
    XCTAssertLessThan(firstRow.frame.minY, firstRowY, "The server list should have scrolled.")
    XCTAssertTrue(app.buttons["https://team8.example.com"].isHittable)
    XCTAssertEqual(
      field.frame.minY, fieldY, accuracy: 0.5,
      "The URL field must stay pinned while the list scrolls.")

    // Rotating flips the size class; the field must keep focus across it.
    field.tap()
    XCUIDevice.shared.orientation = .landscapeLeft
    XCTAssertEqual(field.value(forKey: "hasKeyboardFocus") as? Bool, true)
    XCUIDevice.shared.orientation = .portrait

    let hideKeyboard = app.buttons["Hide Keyboard"]
    XCTAssertTrue(hideKeyboard.waitForExistence(timeout: 5))
    hideKeyboard.tap()
    XCTAssertEqual(field.value(forKey: "hasKeyboardFocus") as? Bool, false)
  }
}
