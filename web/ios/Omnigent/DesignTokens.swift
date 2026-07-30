import SwiftUI

enum DesignTokens {
  static let radius: CGFloat = 8

  static let lightBackground = Color.white
  static let lightForeground = Color(red: 0.067, green: 0.090, blue: 0.110)
  static let lightMutedForeground = Color(red: 0.435, green: 0.435, blue: 0.435)
  static let lightBorder = Color(red: 0.910, green: 0.925, blue: 0.941)

  static let darkBackground = Color(red: 0.118, green: 0.098, blue: 0.153)
  static let darkForeground = Color(red: 0.910, green: 0.925, blue: 0.941)
  static let darkMutedForeground = Color(red: 0.572, green: 0.643, blue: 0.702)
  static let darkBorder = Color(red: 0.215, green: 0.219, blue: 0.230)

  static func background(_ scheme: ColorScheme) -> Color {
    scheme == .dark ? darkBackground : lightBackground
  }

  static func foreground(_ scheme: ColorScheme) -> Color {
    scheme == .dark ? darkForeground : lightForeground
  }

  static func mutedForeground(_ scheme: ColorScheme) -> Color {
    scheme == .dark ? darkMutedForeground : lightMutedForeground
  }

  static func border(_ scheme: ColorScheme) -> Color {
    scheme == .dark ? darkBorder : lightBorder
  }
}
