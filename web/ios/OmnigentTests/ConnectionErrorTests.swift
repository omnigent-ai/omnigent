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

  func testNavigationFailureCallbacksUseCurrentFlag() async throws {
    let defaultsName = "ConnectionErrorTests.\(UUID().uuidString)"
    let defaults = UserDefaults(suiteName: defaultsName)!
    defer { defaults.removePersistentDomain(forName: defaultsName) }
    let url = URL(string: "https://server.invalid/mounted")!
    let error = URLError(.cannotFindHost, userInfo: [NSURLErrorFailingURLErrorKey: url])
    // Nil is reserved for silent workspace cancellation; page-load failures always carry a message.
    var failures: [(URL, String?)] = []
    let view = OmnigentWebView(
      initialURL: url, model: WebViewModel(), settings: SettingsStore(defaults: defaults),
      databricksInternalFeaturesEnabled: false,
      loadFailed: { failures.append(($0, $1)) }, loadSucceeded: {}, pushServerPicker: {},
      requestSwitchServer: { _ in }, openServerSetup: {})
    let transport = PendingWatchTransport()
    transport.activationState = .activated
    let bridge = WatchBridge(defaults: defaults, transport: transport)
    bridge.start()
    let coordinator = OmnigentWebView.Coordinator(view, watchBridge: bridge)
    let configuration = WKWebViewConfiguration()
    configuration.websiteDataStore = .nonPersistent()
    let webView = NoNetworkWebView(frame: .zero, configuration: configuration)
    // Mirror makeUIView: failure callbacks act only for the attached, model-held view.
    view.model.webView = webView
    coordinator.attach(webView)
    // The page loads, and the origin is pinned, once the manifest read falls back.
    let loaded = expectation(description: "page load after the manifest read")
    webView.onLoad = { loaded.fulfill() }
    coordinator.load(url, in: webView)
    defer { coordinator.detach() }
    await fulfillment(of: [loaded], timeout: 10)

    let cookies = PendingWatchCookies()
    cookies.cookies.continuation.yield([
      HTTPCookie(properties: [
        .name: "session", .value: "test", .domain: "server.invalid", .path: "/",
      ])!
    ])
    await bridge.sync(serverURL: url, cookieStore: cookies)?.value
    let signedIn = try XCTUnwrap(transport.contexts.last?[WatchCredentials.contextKey] as? Data)
    XCTAssertFalse(signedIn.isEmpty)
    for (destination, isMainFrame) in [
      ("https://server.invalid/mounted/auth/logout", false),
      ("https://other.invalid/mounted/auth/logout", true),
      ("https://server.invalid/other/auth/logout", true),
    ] {
      XCTAssertNil(
        coordinator.authNavigationRoute(for: URL(string: destination)!, isMainFrame: isMainFrame))
      XCTAssertEqual(transport.contexts.last?[WatchCredentials.contextKey] as? Data, signedIn)
    }
    XCTAssertEqual(
      coordinator.authNavigationRoute(
        for: URL(string: "https://server.invalid/mounted/auth/logout?next=ignored")!,
        isMainFrame: true),
      .logout)
    // No page completes and no native OIDC session exists; abandoned re-login must stay signed out.
    XCTAssertEqual(transport.contexts.last?[WatchCredentials.contextKey] as? Data, Data())
    XCTAssertNil(
      coordinator.authNavigationRoute(
        for: URL(string: "https://identity.invalid/logout")!, isMainFrame: true))
    XCTAssertEqual(transport.contexts.last?[WatchCredentials.contextKey] as? Data, Data())

    let oldCookie = HTTPCookie(properties: [
      .name: "session", .value: "test", .domain: "server.invalid", .path: "/",
    ])!
    for path in ["auth/login", "auth/login/", "login", "auth", "auth/callback"] {
      bridge.signOut()
      cookies.cookies.continuation.yield([oldCookie])
      await coordinator.syncWatchCredentials(
        afterLoading: url.appending(path: path), cookieStore: cookies)?.value
      XCTAssertEqual(transport.contexts.last?[WatchCredentials.contextKey] as? Data, Data(), path)
    }
    cookies.cookies.continuation.yield([oldCookie])
    await bridge.sync(serverURL: url, cookieStore: cookies)?.value
    XCTAssertEqual(
      coordinator.authNavigationRoute(for: url.appending(path: "auth/login"), isMainFrame: true),
      .login)
    XCTAssertEqual(transport.contexts.last?[WatchCredentials.contextKey] as? Data, Data())

    for serverURL in [URL(string: "https://server.invalid")!, url] {
      coordinator.parent.serverURL = serverURL
      cookies.cookies.continuation.yield([oldCookie])
      await bridge.sync(serverURL: serverURL, cookieStore: cookies)?.value
      let active = try XCTUnwrap(transport.contexts.last?[WatchCredentials.contextKey] as? Data)
      XCTAssertFalse(active.isEmpty)
      let login = serverURL.appending(path: "login")
      for (destination, isMainFrame) in [
        (login, false),
        (URL(string: "https://other.invalid/login")!, true),
        (serverURL.appending(path: "other/login"), true),
      ] {
        XCTAssertNil(coordinator.authNavigationRoute(for: destination, isMainFrame: isMainFrame))
        XCTAssertEqual(transport.contexts.last?[WatchCredentials.contextKey] as? Data, active)
      }
      // Accounts login clears before any completion, without becoming a native OIDC route.
      XCTAssertNil(coordinator.authNavigationRoute(for: login, isMainFrame: true))
      XCTAssertEqual(transport.contexts.last?[WatchCredentials.contextKey] as? Data, Data())
    }
    coordinator.parent.serverURL = view.serverURL

    let anonymousCookies = PendingWatchCookies()
    anonymousCookies.cookies.continuation.yield([])
    await coordinator.syncWatchCredentials(afterLoading: url, cookieStore: anonymousCookies)?.value
    let anonymous = try JSONDecoder().decode(
      WatchCredentials.self,
      from: XCTUnwrap(transport.contexts.last?[WatchCredentials.contextKey] as? Data))
    XCTAssertEqual(anonymous.cookie, "", "Cookie-free app deployments must still work")

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

    let server = try MockHTTPServer(requestHandler: { request in
      XCTAssertEqual(request.method, "GET")
      switch request.path {
      case "/.well-known/omnigent.json":
        return (
          200, ["content-type": "application/json"],
          Data(
            #"{"manifest_version":1,"auth":{"mode":"oidc","session_cookie":"ap_session","native_redirect_uris":["ai.omnigent.ios:/oauth/callback"]}}"#
              .utf8)
        )
      case "/mounted/v1/me":
        XCTAssertEqual(request.headers["cookie"], "ap_session=connected")
        return (200, ["content-type": "application/json"], Data("{}".utf8))
      default:
        XCTFail("Unexpected OIDC request: \(request.path)")
        return (404, [:], Data())
      }
    })
    let connectedURL = URL(string: "http://127.0.0.1:\(server.port)/mounted")!
    let connectedCookie = HTTPCookie(properties: [
      .name: "ap_session", .value: "connected", .domain: "127.0.0.1", .path: "/mounted",
      .expires: Date().addingTimeInterval(3600),
    ])!
    await webView.configuration.websiteDataStore.httpCookieStore.setCookie(connectedCookie)
    coordinator.parent.serverURL = url
    let nativeLoaded = expectation(description: "native OIDC connection reuses its accepted cookie")
    webView.onLoad = { nativeLoaded.fulfill() }
    coordinator.load(connectedURL, in: webView)
    await fulfillment(of: [nativeLoaded], timeout: 10)

    let connectedCookies = PendingWatchCookies()
    for staleServer in [url, connectedURL.appending(path: "other-mount")] {
      coordinator.parent.serverURL = staleServer
      connectedCookies.cookies.continuation.yield([connectedCookie])
      await coordinator.syncWatchCredentials(
        afterLoading: connectedURL.appending(path: "c/session"), cookieStore: connectedCookies)?
        .value
      let data = try XCTUnwrap(transport.contexts.last?[WatchCredentials.contextKey] as? Data)
      let credential = try? JSONDecoder().decode(WatchCredentials.self, from: data)
      XCTAssertEqual(credential?.apiURL, connectedURL.appending(path: "v1"))
      XCTAssertEqual(credential?.cookie, "ap_session=connected")
    }
    await coordinator.syncWatchCredentials(
      afterLoading: connectedURL.appending(path: "auth/login"), cookieStore: connectedCookies)?
      .value
    XCTAssertEqual(transport.contexts.last?[WatchCredentials.contextKey] as? Data, Data())
  }

  private func message(for error: Error, enabled: Bool) -> String {
    OmnigentWebView.connectionErrorMessage(
      for: error, databricksInternalFeaturesEnabled: enabled)
  }
}

@MainActor
private final class NoNetworkWebView: WKWebView {
  var onLoad: (() -> Void)?

  override func load(_ request: URLRequest) -> WKNavigation? {
    onLoad?()
    onLoad = nil
    return nil
  }
}
