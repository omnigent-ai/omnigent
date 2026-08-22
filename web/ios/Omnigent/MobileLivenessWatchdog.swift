import Foundation

enum NativeBridgeProtocol {
  static let version = 1
}

@MainActor
final class MobileLivenessWatchdog {
  typealias Schedule = (_ delay: TimeInterval, _ action: @escaping () -> Void) -> () -> Void

  static let initialReadiness: TimeInterval = 20
  static let heartbeat: TimeInterval = 15

  private let schedule: Schedule
  private let onTimeout: () -> Void
  private let onIncompatible: () -> Void
  private var cancelScheduled: (() -> Void)?
  private var active = true
  private var onPinnedOrigin = true
  private var compatible = false

  convenience init(
    onTimeout: @escaping () -> Void, onIncompatible: @escaping () -> Void
  ) {
    self.init(
      schedule: MobileLivenessWatchdog.dispatchSchedule,
      onTimeout: onTimeout,
      onIncompatible: onIncompatible)
  }

  init(
    schedule: @escaping Schedule, onTimeout: @escaping () -> Void,
    onIncompatible: @escaping () -> Void = {}
  ) {
    self.schedule = schedule
    self.onTimeout = onTimeout
    self.onIncompatible = onIncompatible
  }

  func beginInitialWindow() {
    compatible = false
    arm(after: Self.initialReadiness)
  }

  func protocolReady(version: Int, expectedVersion: Int) -> Bool {
    guard version == expectedVersion else {
      cancel()
      onIncompatible()
      return false
    }
    compatible = true
    arm(after: Self.heartbeat)
    return true
  }

  func receivedHeartbeat() {
    if compatible { arm(after: Self.heartbeat) }
  }

  func setActive(_ value: Bool) {
    guard active != value else { return }
    active = value
    value ? arm(after: Self.initialReadiness) : cancel()
  }

  func setOnPinnedOrigin(_ value: Bool) {
    guard onPinnedOrigin != value else { return }
    onPinnedOrigin = value
    value ? arm(after: Self.initialReadiness) : cancel()
  }

  func cancel() {
    cancelScheduled?()
    cancelScheduled = nil
  }

  private func arm(after delay: TimeInterval) {
    cancel()
    guard active, onPinnedOrigin else { return }
    cancelScheduled = schedule(delay, onTimeout)
  }

  private static func dispatchSchedule(
    delay: TimeInterval, action: @escaping () -> Void
  ) -> () -> Void {
    let item = DispatchWorkItem(block: action)
    DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: item)
    return { item.cancel() }
  }
}
