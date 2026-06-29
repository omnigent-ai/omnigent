import Foundation
import UserNotifications

@MainActor
final class NativeNotificationManager: NSObject, UNUserNotificationCenterDelegate {
  static let shared = NativeNotificationManager()

  private let center = UNUserNotificationCenter.current()
  private var activationHandler: ((String) -> Void)?

  private override init() {
    super.init()
  }

  func start() {
    center.delegate = self
  }

  func setActivationHandler(_ handler: @escaping (String) -> Void) {
    activationHandler = handler
  }

  func setBadgeCount(_ count: Int) {
    Task {
      await requestAuthorizationIfNeeded()
      do {
        try await center.setBadgeCount(max(0, count))
      } catch {
        NSLog("[omnigent] failed to set badge count: \(String(describing: error))")
      }
    }
  }

  func notify(title: String, body: String?, navigatePath: String?) {
    Task {
      let granted = await requestAuthorizationIfNeeded()
      guard granted else { return }

      let content = UNMutableNotificationContent()
      content.title = title
      content.body = body ?? ""
      content.sound = .default
      if let navigatePath, navigatePath.starts(with: "/") {
        content.userInfo = ["navigatePath": navigatePath]
      }

      let request = UNNotificationRequest(
        identifier: "omnigent.\(UUID().uuidString)",
        content: content,
        trigger: nil
      )

      do {
        try await center.add(request)
      } catch {
        NSLog("[omnigent] failed to add notification: \(String(describing: error))")
      }
    }
  }

  nonisolated func userNotificationCenter(
    _ center: UNUserNotificationCenter,
    willPresent notification: UNNotification,
    withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
  ) {
    completionHandler([.banner, .list, .sound])
  }

  nonisolated func userNotificationCenter(
    _ center: UNUserNotificationCenter,
    didReceive response: UNNotificationResponse,
    withCompletionHandler completionHandler: @escaping () -> Void
  ) {
    let path = response.notification.request.content.userInfo["navigatePath"] as? String
    Task { @MainActor in
      if let path, path.starts(with: "/") {
        activationHandler?(path)
      }
      completionHandler()
    }
  }

  private func requestAuthorizationIfNeeded() async -> Bool {
    let settings = await center.notificationSettings()
    switch settings.authorizationStatus {
    case .authorized, .provisional, .ephemeral:
      return true
    case .denied:
      return false
    case .notDetermined:
      do {
        return try await center.requestAuthorization(options: [.alert, .sound, .badge])
      } catch {
        return false
      }
    @unknown default:
      return false
    }
  }
}
