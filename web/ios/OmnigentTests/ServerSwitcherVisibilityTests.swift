import Combine
import XCTest

@testable import Omnigent

@MainActor
final class ServerSwitcherVisibilityTests: XCTestCase {
  func testSidebarSelectionHidesTheNativeSwitcherForEveryServerKind() {
    for address in [
      "https://workspace.cloud.databricks.com/omnigent?o=123",
      "https://example.com",
    ] {
      let shell = makeShell(address)
      let model = WebViewModel()
      model.serverSwitcherHidden = true
      XCTAssertFalse(shell.showsServerSwitcher(for: model), address)
      model.serverSwitcherHidden = false
      XCTAssertTrue(shell.showsServerSwitcher(for: model), address)
    }
  }

  func testWatchdogRevealsTheWorkspaceSwitcherWhenThePageDoesNotRespond() async {
    let shell = makeShell("https://workspace.cloud.databricks.com/omnigent?o=123")
    let model = WebViewModel()
    let revealed = expectation(description: "watchdog reveals the native switcher")
    let subscription = model.$serverSwitcherHidden.filter { !$0 }.first().sink { _ in
      revealed.fulfill()
    }
    defer {
      subscription.cancel()
      model.cancelServerSwitcherWatchdog()
    }

    model.armServerSwitcherWatchdog()
    XCTAssertFalse(shell.showsServerSwitcher(for: model))
    await fulfillment(of: [revealed], timeout: 10)
    XCTAssertTrue(shell.showsServerSwitcher(for: model))
  }

  func testCancelledWatchdogKeepsSelectionInTheSidebar() async {
    let shell = makeShell("https://workspace.cloud.databricks.com/omnigent?o=123")
    let model = WebViewModel()
    let revealed = expectation(description: "cancelled watchdog must not reveal the switcher")
    revealed.isInverted = true
    let subscription = model.$serverSwitcherHidden.filter { !$0 }.sink { _ in
      revealed.fulfill()
    }
    defer {
      subscription.cancel()
      model.cancelServerSwitcherWatchdog()
    }

    model.armServerSwitcherWatchdog()
    model.cancelServerSwitcherWatchdog()
    // Wait past the production watchdog's six-second deadline.
    await fulfillment(of: [revealed], timeout: 7)
    XCTAssertFalse(shell.showsServerSwitcher(for: model))
  }

  private func makeShell(_ address: String) -> WebShellView {
    WebShellView(
      initialURL: URL(string: address)!, connectToNewServer: {}, switchToServer: { _ in },
      loadFailed: { _, _ in }, loadSucceeded: {})
  }
}

final class RecoveryPageTests: XCTestCase {
  func testRecoveryPageAppliesOnlyToTheDestinationItWasRecordedFor() {
    let server = URL(string: "https://omnigent.example.com")!
    let page = RecoveryPage(
      url: URL(string: "https://omnigent.example.com/c/old")!, initialURL: server)
    XCTAssertEqual(page.url(for: server), URL(string: "https://omnigent.example.com/c/old"))
    XCTAssertNil(page.url(for: URL(string: "https://omnigent.example.com/c/new")!))
    XCTAssertNil(page.url(for: URL(string: "https://other.example.com")!))
  }
}

final class RecoveryIntentTests: XCTestCase {
  func testRecoveryAppliesOnlyToTheDestinationItWasStartedFor() {
    let workspace = URL(string: "https://workspace.cloud.databricks.com/ml/omnigents?o=1")!
    let intent = RecoveryIntent(initialURL: workspace)
    XCTAssertEqual(intent.intent(for: workspace), .recover)
    XCTAssertEqual(intent.intent(for: URL(string: "https://omnigent.example.com")!), .connect)
    XCTAssertEqual(
      intent.intent(
        for: URL(string: "https://workspace.cloud.databricks.com/ml/omnigents/c/x?o=1")!),
      .connect)
  }
}
