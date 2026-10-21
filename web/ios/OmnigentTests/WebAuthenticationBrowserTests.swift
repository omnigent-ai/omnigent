import AuthenticationServices
import UIKit
import XCTest

@testable import Omnigent

@MainActor
final class WebAuthenticationBrowserTests: XCTestCase {
  private let authorizeURL = URL(string: "https://omnigent.example.com/auth/login?state=s")!
  private let callbackURL = URL(string: "ai.omnigent.ios:/oauth/callback?code=c&state=s")!
  private var sessions: [FakeWebAuthenticationSession] = []
  private var startResult = true
  private var onStart: (() -> Void)?
  private lazy var browser = WebAuthenticationBrowser {
    [unowned self] url, callback, _, completion in
    let session = FakeWebAuthenticationSession(url: url, callback: callback, completion: completion)
    session.startResult = startResult
    session.onStart = onStart
    sessions.append(session)
    return session
  }

  func testReturnsCallbackURLForTheRequestedCallback() async throws {
    let task = try await present(callback: .customScheme("ai.omnigent.ios"))
    let session = try XCTUnwrap(sessions.first)
    XCTAssertEqual(session.url, authorizeURL)
    XCTAssertTrue(session.callback.matchesURL(callbackURL))
    XCTAssertFalse(session.callback.matchesURL(URL(string: "other.app:/oauth/callback")!))
    XCTAssertTrue(browser.isPresenting)

    session.completion(callbackURL, nil)
    session.completion(URL(string: "ai.omnigent.ios:/oauth/callback?code=late")!, nil)

    let url = try await task.value
    XCTAssertEqual(url, callbackURL)
    XCTAssertFalse(browser.isPresenting)
  }

  func testStartFailureIsUnavailable() async throws {
    startResult = false
    do {
      _ = try await browser.callbackURL(
        for: authorizeURL, callback: .customScheme("ai.omnigent.ios"), anchor: anchor())
      XCTFail("Expected start failure")
    } catch {
      XCTAssertEqual(error as? WebAuthenticationBrowserError, .unavailable)
    }
    XCTAssertFalse(browser.isPresenting)
  }

  func testRejectsSecondPresentation() async throws {
    let first = try await present()
    do {
      _ = try await browser.callbackURL(
        for: authorizeURL, callback: .customScheme("ai.omnigent.ios"), anchor: anchor())
      XCTFail("Expected single-flight rejection")
    } catch {
      XCTAssertEqual(error as? WebAuthenticationBrowserError, .inProgress)
    }
    XCTAssertEqual(sessions.count, 1)
    browser.cancel()
    await assertCancelled(first)
  }

  func testCompletionErrorsMapToBrowserErrors() async throws {
    let userCancelled = NSError(
      domain: ASWebAuthenticationSessionErrorDomain,
      code: ASWebAuthenticationSessionError.Code.canceledLogin.rawValue)
    let cancelled = try await present()
    sessions.last!.completion(nil, userCancelled)
    await assertCancelled(cancelled)

    let failed = try await present()
    sessions.last!.completion(callbackURL, NSError(domain: "test", code: 1))
    await assertError(failed, .failed)

    let missing = try await present()
    sessions.last!.completion(nil, nil)
    await assertError(missing, .missingCallback)
    XCTAssertFalse(browser.isPresenting)
  }

  func testCancelResumesWithoutCompletionAndIgnoresStaleCallback() async throws {
    let first = try await present()
    let oldSession = sessions[0]
    browser.cancel()
    XCTAssertEqual(oldSession.cancelCount, 1)
    XCTAssertFalse(browser.isPresenting)
    await assertCancelled(first)

    let second = try await present()
    oldSession.completion(URL(string: "ai.omnigent.ios:/oauth/callback?code=stale")!, nil)
    sessions[1].completion(callbackURL, nil)
    let url = try await second.value
    XCTAssertEqual(url, callbackURL)
  }

  func testCancellingCallerCancelsSession() async throws {
    let task = try await present()
    task.cancel()
    await assertCancelled(task)
    XCTAssertEqual(sessions[0].cancelCount, 1)
    XCTAssertFalse(browser.isPresenting)
  }

  func testAlreadyCancelledCallerDoesNotPresent() async throws {
    let anchor = try anchor()
    let task = Task { [browser, authorizeURL] in
      withUnsafeCurrentTask { $0?.cancel() }
      return try await browser.callbackURL(
        for: authorizeURL, callback: .customScheme("ai.omnigent.ios"), anchor: anchor)
    }
    await assertCancelled(task)
    XCTAssertTrue(sessions.isEmpty)
  }

  /// Starts a presentation and waits until its session has started.
  private func present(
    callback: ASWebAuthenticationSession.Callback = .customScheme("ai.omnigent.ios")
  ) async throws -> Task<URL, Error> {
    let anchor = try anchor()
    let started = expectation(description: "session started")
    onStart = { started.fulfill() }
    let task = Task { [browser, authorizeURL] in
      try await browser.callbackURL(for: authorizeURL, callback: callback, anchor: anchor)
    }
    await fulfillment(of: [started], timeout: 2)
    onStart = nil
    return task
  }

  private func anchor() throws -> ASPresentationAnchor {
    let scene = try XCTUnwrap(
      UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
    return ASPresentationAnchor(windowScene: scene)
  }

  private func assertCancelled(_ task: Task<URL, Error>) async {
    do {
      _ = try await task.value
      XCTFail("Expected cancellation")
    } catch {
      XCTAssertTrue(error is CancellationError, "\(error)")
    }
  }

  private func assertError(_ task: Task<URL, Error>, _ expected: WebAuthenticationBrowserError)
    async
  {
    do {
      _ = try await task.value
      XCTFail("Expected \(expected)")
    } catch {
      XCTAssertEqual(error as? WebAuthenticationBrowserError, expected)
    }
  }
}

@MainActor
private final class FakeWebAuthenticationSession: WebAuthenticationSession {
  let url: URL
  let callback: ASWebAuthenticationSession.Callback
  let completion: ASWebAuthenticationSession.CompletionHandler
  var startResult = true
  var cancelCount = 0
  var onStart: (() -> Void)?

  init(
    url: URL, callback: ASWebAuthenticationSession.Callback,
    completion: @escaping ASWebAuthenticationSession.CompletionHandler
  ) {
    self.url = url
    self.callback = callback
    self.completion = completion
  }

  func start() -> Bool {
    onStart?()
    return startResult
  }

  func cancel() { cancelCount += 1 }
}
