import SwiftUI

/// Floating gradient blobs on a slow elliptical orbit, under a veil and a fading
/// grid. Native port of the desktop onboarding's `BlobGraphic` (web/src/components/
/// onboarding/BlobGraphic.css); colors, timings and keyframes mirror it.
struct WelcomeBlobBackground: View {
  @Environment(\.colorScheme) private var colorScheme
  @Environment(\.accessibilityReduceMotion) private var reduceMotion

  var body: some View {
    // The blobs drift slowly, so 30fps looks identical and halves the redraw cost.
    TimelineView(.animation(minimumInterval: 1.0 / 30, paused: reduceMotion)) { timeline in
      let time = reduceMotion ? 0 : timeline.date.timeIntervalSinceReferenceDate
      orbit(at: time)
    }
    .frame(maxWidth: .infinity, maxHeight: .infinity)
    .background(colorScheme == .dark ? Self.darkBase : Self.lightBase)
    .overlay(colorScheme == .dark ? Color.black.opacity(0.2) : Color.white.opacity(0.08))
    .overlay(grid)
    .clipped()
    .accessibilityHidden(true)
  }

  private func orbit(at time: TimeInterval) -> some View {
    let angle = -60 + 360 * time.truncatingRemainder(dividingBy: 44) / 44
    return ZStack {
      ForEach(WelcomeBlob.all) { blob in
        let pose = blob.pose(at: time)
        Circle()
          .fill(
            RadialGradient(
              stops: [
                .init(color: blob.color.opacity(0.28), location: 0),
                .init(color: blob.color.opacity(0.1), location: 0.58),
                .init(color: blob.color.opacity(0.04), location: 1),
              ],
              center: blob.gradientCenter, startRadius: 0, endRadius: 260)
          )
          .frame(width: 280, height: 280)
          .scaleEffect(pose.scale)
          .position(x: blob.center.x + pose.dx, y: blob.center.y + pose.dy)
      }
    }
    .frame(width: 300, height: 300)
    // The web veil adds a 16px backdrop blur on top of each blob's 24px blur.
    .blur(radius: 28)
    .saturation(1.2)
    .rotationEffect(.degrees(angle))
    .scaleEffect(x: 1, y: 0.62)
  }

  private var grid: some View {
    Canvas { context, size in
      var path = Path()
      for x in stride(from: 0, through: size.width, by: 24) {
        path.addRect(CGRect(x: x, y: 0, width: 1, height: size.height))
      }
      for y in stride(from: 0, through: size.height, by: 24) {
        path.addRect(CGRect(x: 0, y: y, width: size.width, height: 1))
      }
      context.fill(
        path,
        with: .color(colorScheme == .dark ? .black.opacity(0.38 * 0.8) : .white.opacity(0.32)))
    }
    .mask(
      LinearGradient(
        stops: [
          .init(color: .clear, location: 0),
          .init(color: .black, location: 0.32),
          .init(color: .black, location: 0.68),
          .init(color: .clear, location: 1),
        ],
        startPoint: .top, endPoint: .bottom)
    )
  }

  // color-mix(in srgb, var(--muted) 48%, var(--card)) in the light theme.
  private static let lightBase = Color(white: 0.971)
  private static let darkBase = Color(red: 0.051, green: 0.051, blue: 0.059)
}

struct WelcomeBlob: Identifiable {
  struct Pose {
    var dx: CGFloat
    var dy: CGFloat
    var scale: CGFloat
  }

  struct Keyframe {
    var at: Double
    var pose: Pose
  }

  let id: String
  let color: Color
  let gradientCenter: UnitPoint
  /// Center within the 300pt orbit square.
  let center: CGPoint
  let duration: Double
  /// The web animation's negative delay, so each blob starts mid-cycle.
  let offset: Double
  let keyframes: [Keyframe]

  func pose(at time: TimeInterval) -> Pose {
    let progress = (time + offset).truncatingRemainder(dividingBy: duration) / duration
    let nextIndex = keyframes.firstIndex { $0.at > progress } ?? keyframes.count - 1
    let from = keyframes[nextIndex - 1]
    let to = keyframes[nextIndex]
    // A sine ease approximates the web's cubic-bezier(0.45, 0, 0.55, 1).
    let linear = (progress - from.at) / (to.at - from.at)
    let eased = CGFloat(0.5 - 0.5 * cos(Double.pi * linear))
    return Pose(
      dx: from.pose.dx + (to.pose.dx - from.pose.dx) * eased,
      dy: from.pose.dy + (to.pose.dy - from.pose.dy) * eased,
      scale: from.pose.scale + (to.pose.scale - from.pose.scale) * eased)
  }

  /// Keyframe table from `(percent, dx, dy, scale)` rows, closed at 0% and 100%.
  private static func frames(_ rows: [(Double, CGFloat, CGFloat, CGFloat)]) -> [Keyframe] {
    let rest = Keyframe(at: 0, pose: Pose(dx: 0, dy: 0, scale: 1))
    let middle = rows.map { Keyframe(at: $0.0 / 100, pose: Pose(dx: $0.1, dy: $0.2, scale: $0.3)) }
    return [rest] + middle + [Keyframe(at: 1, pose: rest.pose)]
  }

  // Drawn in this order: the web stacks pink (z1), blue (z2), orange (z3).
  static let all = [
    WelcomeBlob(
      id: "pink", color: Color(red: 224 / 255, green: 68 / 255, blue: 199 / 255),
      gradientCenter: UnitPoint(x: 0.36, y: 0.32), center: CGPoint(x: 102, y: 204),
      duration: 9.6, offset: 2.8,
      keyframes: frames([
        (18, 24, -18, 1.05), (39, 38, 12, 0.97), (57, 6, 30, 1.08),
        (78, -28, 14, 0.95), (91, -14, -24, 1.03),
      ])),
    WelcomeBlob(
      id: "blue", color: Color(red: 32 / 255, green: 158 / 255, blue: 214 / 255),
      gradientCenter: UnitPoint(x: 0.42, y: 0.64), center: CGPoint(x: 198, y: 204),
      duration: 15.6, offset: 10.2,
      keyframes: frames([
        (14, -30, -14, 1.06), (31, -36, 20, 0.96), (49, -4, 34, 1.08),
        (68, 30, 10, 0.97), (86, 18, -28, 1.04),
      ])),
    WelcomeBlob(
      id: "orange", color: Color(red: 249 / 255, green: 115 / 255, blue: 22 / 255),
      gradientCenter: UnitPoint(x: 0.62, y: 0.38), center: CGPoint(x: 150, y: 54),
      duration: 12.2, offset: 7.2,
      keyframes: frames([
        (16, -24, 20, 0.96), (34, -8, 38, 1.06), (53, 28, 24, 0.98),
        (71, 34, -16, 1.07), (88, -12, -30, 0.95),
      ])),
  ]
}
