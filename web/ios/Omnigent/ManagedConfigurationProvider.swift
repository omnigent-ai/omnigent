import Foundation
import ManagedApp
import UIKit

/// Subscribes to the managed app configuration a management service pushes to
/// this install and publishes its server URLs and feature flags.
///
/// Watches both channels a service can use — a `com.apple.configuration.app.managed`
/// declaration via the ManagedApp framework, and the classic
/// `com.apple.configuration.managed` defaults key — because a service that
/// doesn't send declarations would otherwise configure nothing. Views stay
/// unaware of which channel supplied the configuration.
@MainActor
final class ManagedConfigurationProvider: ObservableObject {
  /// Publish servers and flags together so views see a consistent configuration.
  @Published private var configuration: OmnigentManagedConfiguration = .empty

  var serverURLs: [URL] { configuration.serverURLs }
  var databricksInternalFeaturesEnabled: Bool { configuration.databricksInternalFeaturesEnabled }

  private let defaults: UserDefaults
  private var declarativeConfiguration: OmnigentManagedConfiguration?
  private var legacyConfiguration: OmnigentManagedConfiguration = .empty
  private var subscription: Task<Void, Never>?
  private var legacyObservers: [NSObjectProtocol] = []

  /// Test and preview seam: a fixed list, no subscriptions.
  init(serverURLs: [URL]) {
    configuration = OmnigentManagedConfiguration(serverURLs: serverURLs)
    defaults = .standard
  }

  init(defaults: UserDefaults = .standard) {
    self.defaults = defaults

    #if DEBUG
      // Wins over both channels: this is the local test seam, and neither
      // channel can be faked in the Simulator's declarative form.
      if let injected = ProcessInfo.processInfo.omnigentManagedServers {
        configuration = OmnigentManagedConfiguration(serverURLs: injected)
        return
      }
    #endif

    observeLegacyConfiguration()
    subscribeToDeclarativeConfiguration()
  }

  deinit {
    subscription?.cancel()
    for observer in legacyObservers {
      NotificationCenter.default.removeObserver(observer)
    }
  }

  /// Runs for the lifetime of the app: the sequence re-yields whenever the
  /// administrator updates the configuration, so the connect screen and the
  /// server switcher stay current without a relaunch.
  private func subscribeToDeclarativeConfiguration() {
    subscription = Task { [weak self] in
      let provider = ManagedAppConfigurationProvider()
      for await configuration in await provider.configurations(OmnigentManagedConfiguration.self) {
        // nil clears stale declarative values and falls back to classic configuration.
        self?.declarativeConfiguration = configuration
        self?.republish()
      }
    }
  }

  /// A service can rewrite the classic key at any time, and it lands in the
  /// app's own defaults domain with no callback of its own.
  ///
  /// Becoming active is the dependable re-read point:
  /// `UserDefaults.didChangeNotification` does **not** fire for a write made by
  /// another process, which is exactly how a configuration arrives, so it is
  /// only a best-effort fast path here.
  private func observeLegacyConfiguration() {
    readLegacyConfiguration()
    for name in [UIApplication.didBecomeActiveNotification, UserDefaults.didChangeNotification] {
      legacyObservers.append(
        NotificationCenter.default.addObserver(forName: name, object: nil, queue: .main) {
          [weak self] _ in
          Task { @MainActor in self?.readLegacyConfiguration() }
        }
      )
    }
  }

  private func readLegacyConfiguration() {
    // Drop the in-process cache first: the value was written by another process,
    // so a plain read can return the snapshot this process already had.
    defaults.synchronize()
    let configuration = OmnigentManagedConfiguration.read(legacyFrom: defaults)
    guard configuration != legacyConfiguration else { return }
    legacyConfiguration = configuration
    republish()
  }

  private func republish() {
    configuration = OmnigentManagedConfiguration.resolve(
      declarative: declarativeConfiguration, legacy: legacyConfiguration)
  }
}

#if DEBUG
  extension ProcessInfo {
    /// The `--omnigent-managed-servers <url,url>` DEBUG-only launch argument.
    /// A declarative configuration can't be delivered to the Simulator at all,
    /// so this is how UI tests and a local run exercise the preset rows without
    /// touching the app's defaults. Compiled out of Release, so it can never
    /// stand in for a real configuration.
    fileprivate var omnigentManagedServers: [URL]? {
      guard let value = omnigentArgumentValue(after: "--omnigent-managed-servers") else {
        return nil
      }
      let urls = value.split(separator: ",").compactMap {
        try? ServerURL.normalize(String($0), allowsInsecureHTTP: true)
      }
      return urls.isEmpty ? nil : urls
    }
  }
#endif
