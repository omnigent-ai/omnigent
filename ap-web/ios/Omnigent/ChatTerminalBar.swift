import SwiftUI

/// The native Chat/Terminal switcher rendered over the bottom of the web view.
///
/// On iOS 26+ the capsule uses the system Liquid Glass material; on iOS 18–25 it
/// falls back to `.ultraThinMaterial`, matching the look of `ServerSwitcher`.
struct ChatTerminalBar: View {
  @Binding var mode: WebViewMode
  let terminalEnabled: Bool
  let terminalStartingUp: Bool
  let onSelect: (WebViewMode) -> Void

  @Environment(\.colorScheme) private var colorScheme
  @Namespace private var selection

  var body: some View {
    HStack(spacing: 4) {
      segment(.chat, title: "Chat", systemImage: "message")
      segment(.terminal, title: "Terminal", systemImage: "terminal")
    }
    .padding(4)
    .modifier(GlassCapsule(colorScheme: colorScheme))
    .animation(.easeInOut(duration: 0.18), value: mode)
    .accessibilityElement(children: .contain)
    .accessibilityLabel("View mode")
  }

  @ViewBuilder
  private func segment(_ target: WebViewMode, title: String, systemImage: String) -> some View {
    let isSelected = mode == target
    let isDisabled = target == .terminal && !terminalEnabled

    Button {
      guard !isDisabled, mode != target else { return }
      onSelect(target)
    } label: {
      HStack(spacing: 5) {
        if target == .terminal && terminalStartingUp {
          ProgressView()
            .controlSize(.mini)
        } else {
          Image(systemName: systemImage)
            .font(.system(size: 13, weight: .medium))
        }
        Text(title)
          .font(.system(size: 13, weight: .medium))
      }
      .foregroundStyle(
        isSelected ? DesignTokens.foreground(colorScheme) : DesignTokens.mutedForeground(colorScheme)
      )
      .padding(.horizontal, 14)
      .frame(height: 34)
      .background {
        if isSelected {
          Capsule(style: .continuous)
            .fill(Color.primary.opacity(colorScheme == .dark ? 0.16 : 0.08))
            .matchedGeometryEffect(id: "selection", in: selection)
        }
      }
      .contentShape(Capsule(style: .continuous))
    }
    .buttonStyle(.plain)
    .disabled(isDisabled)
    .opacity(isDisabled ? 0.4 : 1)
    .accessibilityAddTraits(isSelected ? [.isSelected] : [])
  }
}

/// Wraps the bar in the system glass material where available, otherwise a
/// hand-rolled material capsule that mirrors `ServerSwitcher`'s styling.
private struct GlassCapsule: ViewModifier {
  let colorScheme: ColorScheme

  func body(content: Content) -> some View {
    if #available(iOS 26.0, *) {
      content.glassEffect(.regular.interactive(), in: .capsule)
    } else {
      content
        .background(.ultraThinMaterial, in: Capsule(style: .continuous))
        .overlay {
          Capsule(style: .continuous)
            .stroke(Color.primary.opacity(colorScheme == .dark ? 0.16 : 0.10), lineWidth: 0.5)
        }
        .shadow(color: .black.opacity(colorScheme == .dark ? 0.22 : 0.08), radius: 10, y: 4)
    }
  }
}
