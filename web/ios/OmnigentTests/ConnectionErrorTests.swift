import WebKit
import XCTest

@testable import Omnigent

@MainActor
final class ConnectionErrorTests: XCTestCase {
  private let jamfMessage =
    "Couldn’t reach the server. Check your device’s compliance status in Jamf."

  func testDNSFailuresShowJAMFHintOnlyWhenEnabled() {
    for code in [URLError.cannotFindHost, .dnsLookupFailed] {
      let error = URLError(code)
      XCTAssertEqual(message(for: error, enabled: true), jamfMessage)
      XCTAssertEqual(message(for: error, enabled: false), error.localizedDescription)
    }
  }

  func testDNSMappingDoesNotDependOnLocalizedDescription() {
    let error = NSError(
      domain: NSURLErrorDomain, code: NSURLErrorCannotFindHost,
      userInfo: [NSLocalizedDescriptionKey: "Serveur introuvable"])
    XCTAssertEqual(message(for: error, enabled: true), jamfMessage)
  }

  func testOtherNetworkErrorsKeepTheirOriginalMessage() {
    for code in [
      URLError.cannotConnectToHost, .timedOut, .notConnectedToInternet,
      .networkConnectionLost, .secureConnectionFailed, .serverCertificateUntrusted, .cancelled,
    ] {
      let error = URLError(code)
      XCTAssertEqual(message(for: error, enabled: true), error.localizedDescription)
    }
  }

  func testSameCodeInAnotherDomainIsNotADNSFailure() {
    for code in [NSURLErrorCannotFindHost, NSURLErrorDNSLookupFailed] {
      let error = NSError(
        domain: "OmnigentTests", code: code,
        userInfo: [NSLocalizedDescriptionKey: "Unrelated failure"])
      XCTAssertEqual(message(for: error, enabled: true), error.localizedDescription)
    }
  }

  func testNavigationFailureCallbacksUseCurrentFlag() {
    let defaultsName = "ConnectionErrorTests.\(UUID().uuidString)"
    let defaults = UserDefaults(suiteName: defaultsName)!
    defer { defaults.removePersistentDomain(forName: defaultsName) }
    let url = URL(string: "https://server.invalid")!
    let error = URLError(.cannotFindHost, userInfo: [NSURLErrorFailingURLErrorKey: url])
    // Nil is reserved for silent workspace cancellation; page-load failures always carry a message.
    var failures: [(URL, String?)] = []
    let view = OmnigentWebView(
      initialURL: url, model: WebViewModel(), settings: SettingsStore(defaults: defaults),
      databricksInternalFeaturesEnabled: false,
      loadFailed: { failures.append(($0, $1)) }, loadSucceeded: {}, pushServerPicker: {},
      requestSwitchServer: { _ in }, openServerSetup: {})
    let coordinator = view.makeCoordinator()
    let webView = NoNetworkWebView()
    // Mirror makeUIView: failure callbacks act only for the attached, model-held view.
    view.model.webView = webView
    coordinator.attach(webView)
    coordinator.load(url, in: webView)
    defer { coordinator.detach() }

    for enabled in [true, false] {
      coordinator.parent = OmnigentWebView(
        initialURL: url, model: view.model, settings: view.settings,
        databricksInternalFeaturesEnabled: enabled,
        loadFailed: view.loadFailed, loadSucceeded: {}, pushServerPicker: {},
        requestSwitchServer: { _ in }, openServerSetup: {})
      coordinator.webView(webView, didFailProvisionalNavigation: nil, withError: error)
      coordinator.webView(webView, didFail: nil, withError: error)
    }

    XCTAssertEqual(failures.map(\.0), Array(repeating: url, count: 4))
    XCTAssertEqual(
      failures.map(\.1),
      [jamfMessage, jamfMessage, error.localizedDescription, error.localizedDescription])
    XCTAssertFalse(failures.contains { $0.1 == nil })

    coordinator.webView(webView, didFail: nil, withError: URLError(.cancelled))
    let unrelatedError = URLError(
      .cannotFindHost,
      userInfo: [NSURLErrorFailingURLErrorKey: URL(string: "https://unrelated.invalid")!])
    coordinator.webView(webView, didFailProvisionalNavigation: nil, withError: unrelatedError)
    XCTAssertEqual(failures.count, 4, "Canceled and off-origin failures must still be ignored")
  }

  private func message(for error: Error, enabled: Bool) -> String {
    OmnigentWebView.connectionErrorMessage(
      for: error, databricksInternalFeaturesEnabled: enabled)
  }
}

@MainActor
private final class NoNetworkWebView: WKWebView {
  override func load(_ request: URLRequest) -> WKNavigation? { nil }
}
