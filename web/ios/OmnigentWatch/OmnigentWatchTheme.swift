import SwiftUI

enum OmnigentWatchTheme {
  static let brandAccent = Color(hex: 0xDF3C85)
  static let brandForeground = Color(hex: 0xFF9FD6)

  static let background = Color(hex: 0x0E1013)
  static let surface = Color(hex: 0x181F25)
  static let raisedSurface = Color(hex: 0x1F272D)
  static let foreground = Color(hex: 0xE8ECF0)
  static let mutedForeground = Color(hex: 0x92A4B3)

  static let active = Color(hex: 0x5CA4F5)
  static let success = Color(hex: 0x2EA65C)
  static let warning = Color(hex: 0xD4972A)
  static let error = Color(hex: 0xF04858)

  static let canvas = LinearGradient(
    colors: [
      brandAccent.opacity(0.2),
      background,
      background,
    ],
    startPoint: .topTrailing,
    endPoint: .bottomLeading
  )
}

struct OmnigentWatchRowBackground: View {
  var color = OmnigentWatchTheme.surface

  var body: some View {
    RoundedRectangle(cornerRadius: 14, style: .continuous)
      .fill(color)
      .overlay {
        RoundedRectangle(cornerRadius: 14, style: .continuous)
          .stroke(Color.white.opacity(0.04), lineWidth: 1)
      }
  }
}

extension Color {
  fileprivate init(hex: UInt32) {
    self.init(
      red: Double((hex >> 16) & 0xFF) / 255,
      green: Double((hex >> 8) & 0xFF) / 255,
      blue: Double(hex & 0xFF) / 255
    )
  }
}
