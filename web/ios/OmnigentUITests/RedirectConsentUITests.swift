import XCTest

/// UI smoke test for the deep-link consent flow (the surface the [F-CR-7] fix
/// lives behind), driven in the simulator via the DEBUG-only `--omnigent-open-url`
/// launch seam added by F-CR-6 (#3179).
///
/// NOTE: this does NOT exercise the cross-origin redirect itself. A deep link
/// to `localhost` infers `http` (`DeepLink.defaultScheme`), and
/// `WorkspaceURLExpander.expandIfNeeded`'s workspace probe is https-only — so
/// for a loopback link the probe never fires. The redirect-blocking policy is
/// verified over a real local network by `WorkspaceURLExpanderRedirectTests`,
/// and the `response.url` defense-in-depth check by
/// `WorkspaceURLExpanderTests.testRejectsResponseFromDifferentOrigin`.
///
/// What this test does verify end-to-end: an `omnigent://` deep link to an
/// unknown server presents the consent alert, approving it runs the
/// post-consent `expandIfNeeded` path, and the WebView loads the approved
/// server — i.e. the consent flow still works with the fix in place.
@MainActor
final class RedirectConsentUITests: XCTestCase {
  func testDeepLinkConsentOpensApprovedServer() throws {
    let marker = "OMNIGENT_DEEPLINK_CONSENT_OK"
    let server = try MockHTTPServer { method, _ in
      (
        200, ["content-type": "text/html"],
        "<html><body>\(marker)</body></html>".data(using: .utf8)!
      )
    }

    let deepLink = "omnigent://localhost:\(server.port)/c/conv_smoke"

    let app = XCUIApplication(bundleIdentifier: "ai.omnigent.ios")
    app.launchArguments += [
      "--omnigent-open-url", deepLink,
      "--omnigent-reset-state",
    ]
    app.launch()

    let alert = app.alerts["Open this Omnigent link?"]
    XCTAssertTrue(
      alert.waitForExistence(timeout: 15), "Expected the consent alert for \(deepLink).")
    alert.buttons["Open"].tap()

    XCTAssertTrue(
      app.webViews.firstMatch.waitForExistence(timeout: 30), "Expected a web view after approving.")
    XCTAssertTrue(
      app.webViews.staticTexts[marker].waitForExistence(timeout: 30),
      "Expected the WebView to load the approved server (marker \(marker))."
    )
  }
}
