import SwiftUI

struct AppRootView: View {
  @EnvironmentObject private var settings: SettingsStore
  @State private var mode: Mode

  init() {
    _mode = State(initialValue: .setup(prefill: nil, error: nil))
  }

  var body: some View {
    Group {
      switch mode {
      case .setup(let prefill, let error):
        ConnectView(prefill: prefill ?? settings.serverURL, error: error) { url in
          settings.serverURL = url.absoluteString
          mode = .web(url)
        }
      case .web(let url):
        WebShellView(
          initialURL: url,
          connectToNewServer: {
            mode = .setup(prefill: settings.serverURL, error: nil)
          },
          switchToServer: { nextURL in
            settings.serverURL = nextURL.absoluteString
            mode = .web(nextURL)
          },
          loadFailed: { failedURL, message in
            mode = .setup(
              prefill: failedURL.omnigentOrigin ?? failedURL.absoluteString, error: message)
          },
          loadSucceeded: { loadedURL in
            settings.rememberRecentServer(loadedURL)
          }
        )
      }
    }
    .task {
      guard shouldAutoOpenSavedServer else { return }
      if case .setup(nil, nil) = mode,
        let saved = settings.serverURL,
        let url = URL(string: saved)
      {
        mode = .web(url)
      }
    }
  }

  private enum Mode: Equatable {
    case setup(prefill: String?, error: String?)
    case web(URL)
  }

  private var shouldAutoOpenSavedServer: Bool {
    #if DEBUG
      let processInfo = ProcessInfo.processInfo
      return processInfo.environment["OMNIGENT_SCREENSHOT_APP_URL"] == nil
        && !processInfo.arguments.contains("-FASTLANE_SNAPSHOT")
    #else
      true
    #endif
  }
}
