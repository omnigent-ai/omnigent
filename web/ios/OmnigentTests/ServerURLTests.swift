import XCTest

@testable import Omnigent

final class ServerURLTests: XCTestCase {
  func testReleasePolicyDefaultsBareHostToHTTPS() throws {
    let url = try ServerURL.normalize("example.com", allowsInsecureHTTP: false)
    XCTAssertEqual(url.absoluteString, "https://example.com")
  }

  func testDebugPolicyDefaultsBareHostToHTTP() throws {
    let url = try ServerURL.normalize("localhost:6767", allowsInsecureHTTP: true)
    XCTAssertEqual(url.absoluteString, "http://localhost:6767")
  }

  /// A remote host must not be downgraded to http just because the debug build
  /// allows insecure http: an http pin that the server upgrades to https breaks
  /// every pinned-origin check in the web view.
  func testDebugPolicyStillDefaultsRemoteHostToHTTPS() throws {
    let url = try ServerURL.normalize(
      "dbc-b7cf3f6a-ac9f.cloud.databricks.com", allowsInsecureHTTP: true)
    XCTAssertEqual(url.absoluteString, "https://dbc-b7cf3f6a-ac9f.cloud.databricks.com")
  }

  /// Loopback keeps defaulting to http so local dev still works.
  func testDebugPolicyDefaultsLoopbackVariantsToHTTP() throws {
    XCTAssertEqual(
      try ServerURL.normalize("127.0.0.1:6767", allowsInsecureHTTP: true).absoluteString,
      "http://127.0.0.1:6767")
  }

  /// An explicitly typed http:// URL is still honoured under the debug policy.
  func testDebugPolicyHonoursExplicitHTTPForRemoteHost() throws {
    XCTAssertEqual(
      try ServerURL.normalize("http://example.com", allowsInsecureHTTP: true).absoluteString,
      "http://example.com")
  }

  func testReleasePolicyRejectsHTTP() {
    XCTAssertThrowsError(try ServerURL.normalize("http://example.com", allowsInsecureHTTP: false)) {
      error in
      XCTAssertEqual(error as? ServerURLError, .insecureHTTPNotAllowed)
    }
  }

  func testRejectsNonWebSchemes() {
    XCTAssertThrowsError(try ServerURL.normalize("ftp://example.com", allowsInsecureHTTP: true)) {
      error in
      XCTAssertEqual(error as? ServerURLError, .unsupportedScheme("ftp"))
    }
  }
}

final class ServerAuthenticationTests: XCTestCase {
  func testClassifiesWorkspaceHosts() {
    for origin in [
      "https://databricks.com",
      "https://dbc-123.cloud.databricks.com",
      "https://azuredatabricks.net",
      "https://adb-123.azuredatabricks.net",
      "https://DBC-123.CLOUD.DATABRICKS.COM",
      "https://ADB-123.AZUREDATABRICKS.NET",
    ] {
      XCTAssertEqual(ServerAuthentication(origin: origin), .databricksWorkspace, origin)
    }
  }

  func testClassifiesAppsSeparatelyFromWorkspaces() {
    for origin in [
      "https://databricksapps.com",
      "https://my-app.aws.databricksapps.com",
      "https://MY-APP.AZURE.DATABRICKSAPPS.COM",
    ] {
      XCTAssertEqual(ServerAuthentication(origin: origin), .databricksApp, origin)
    }
  }

  func testOtherHostsAndLookalikesUseOIDC() {
    for origin in [
      "https://example.com",
      "https://localhost:6767",
      "https://notdatabricks.com",
      "https://notazuredatabricks.net",
      "https://notdatabricksapps.com",
      "https://databricks.com.example.org",
      "https://azuredatabricks.net.example.org",
      "https://databricksapps.com.example.org",
      "https://databricks.com@example.org",
      "https://example.org/databricks.com",
      "not a URL",
      "",
    ] {
      XCTAssertEqual(ServerAuthentication(origin: origin), .oidc, origin)
    }
    XCTAssertEqual(ServerAuthentication(origin: nil), .oidc)
    XCTAssertEqual(ServerAuthentication(host: nil), .oidc)
    XCTAssertEqual(ServerAuthentication(host: ""), .oidc)
  }

  func testClassificationDependsOnlyOnHost() {
    for origin in [
      "http://dbc-123.cloud.databricks.com",
      "https://dbc-123.cloud.databricks.com:8443/omnigent?o=123#conversation",
    ] {
      XCTAssertEqual(ServerAuthentication(origin: origin), .databricksWorkspace, origin)
    }
    XCTAssertEqual(
      ServerAuthentication(origin: "https://my-app.databricksapps.com:8443/c/abc"), .databricksApp)
  }

  func testHostAndOriginClassificationAgree() {
    for host in [
      "databricks.com", "DBC-123.CLOUD.DATABRICKS.COM", "adb-123.azuredatabricks.net",
      "databricksapps.com", "MY-APP.AWS.DATABRICKSAPPS.COM", "example.org",
      "notdatabricks.com", "azuredatabricks.net.example.org", "databricksapps.com.example.org",
    ] {
      XCTAssertEqual(
        ServerAuthentication(host: host), ServerAuthentication(origin: "https://\(host)"), host)
    }
  }

  func testOnlyAppsAuthenticationRemainsInline() {
    XCTAssertFalse(ServerAuthentication.databricksWorkspace.usesInWebViewAuth)
    XCTAssertTrue(ServerAuthentication.databricksApp.usesInWebViewAuth)
    XCTAssertFalse(ServerAuthentication.oidc.usesInWebViewAuth)
  }
}

final class AppDeviceSupportTests: XCTestCase {
  func testAppSupportsIPhoneAndIPad() throws {
    let deviceFamilies = try XCTUnwrap(
      Bundle.main.object(forInfoDictionaryKey: "UIDeviceFamily") as? [Int])

    XCTAssertEqual(deviceFamilies, [1, 2])
  }

  func testIPadSupportsAllOrientations() throws {
    let orientations = try XCTUnwrap(
      Bundle.main.object(forInfoDictionaryKey: "UISupportedInterfaceOrientations~ipad")
        as? [String])

    XCTAssertEqual(
      Set(orientations),
      Set([
        "UIInterfaceOrientationPortrait",
        "UIInterfaceOrientationPortraitUpsideDown",
        "UIInterfaceOrientationLandscapeLeft",
        "UIInterfaceOrientationLandscapeRight",
      ]))
  }
}

final class AppPrivacyInfoTests: XCTestCase {
  func testPrivacyUsageDescriptionsArePresent() throws {
    for key in [
      "NSCameraUsageDescription",
      "NSMicrophoneUsageDescription",
      "NSSpeechRecognitionUsageDescription",
    ] {
      let value = try XCTUnwrap(Bundle.main.object(forInfoDictionaryKey: key) as? String)

      XCTAssertFalse(value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
    }
  }
}
