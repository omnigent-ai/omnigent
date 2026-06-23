import SwiftUI

struct ConnectView: View {
  let prefill: String?
  let error: String?
  let onConnect: (URL) -> Void

  @Environment(\.colorScheme) private var colorScheme
  @EnvironmentObject private var settings: SettingsStore
  @State private var serverURL: String
  @State private var message: String?
  @State private var isConnecting = false

  init(prefill: String?, error: String?, onConnect: @escaping (URL) -> Void) {
    self.prefill = prefill
    self.error = error
    self.onConnect = onConnect
    _serverURL = State(initialValue: prefill ?? defaultServerURL)
    _message = State(initialValue: error)
  }

  var body: some View {
    VStack {
      Spacer(minLength: 24)

      VStack(spacing: 0) {
        Image(colorScheme == .dark ? "OmnigentLogoReverse" : "OmnigentLogo")
          .resizable()
          .scaledToFit()
          .frame(height: 80)
          .padding(.bottom, 12)

        Text("Enter the URL of the Omnigents server. The iOS app loads its web UI directly.")
          .font(.system(size: 14))
          .lineSpacing(2)
          .multilineTextAlignment(.center)
          .foregroundStyle(DesignTokens.mutedForeground(colorScheme))
          .padding(.bottom, 24)

        VStack(alignment: .leading, spacing: 8) {
          Text("Server URL")
            .font(.system(size: 14, weight: .medium))
            .foregroundStyle(DesignTokens.foreground(colorScheme))

          TextField(defaultServerURL, text: $serverURL)
            .textInputAutocapitalization(.never)
            .autocorrectionDisabled()
            .keyboardType(.URL)
            .font(.system(size: 14))
            .padding(.horizontal, 12)
            .frame(height: 38)
            .overlay {
              RoundedRectangle(cornerRadius: DesignTokens.radius)
                .stroke(DesignTokens.border(colorScheme), lineWidth: 1)
            }
            .submitLabel(.go)
            .onSubmit(connect)
        }

        Button(action: connect) {
          if isConnecting {
            ProgressView()
              .tint(primaryForeground)
          } else {
            Text("Connect")
          }
        }
        .buttonStyle(.plain)
        .font(.system(size: 14, weight: .medium))
        .frame(maxWidth: .infinity)
        .frame(height: 38)
        .background(primary)
        .foregroundStyle(primaryForeground)
        .clipShape(RoundedRectangle(cornerRadius: DesignTokens.radius))
        .padding(.top, 16)
        .disabled(isConnecting)

        Text(message ?? "")
          .font(.system(size: 13))
          .lineSpacing(2)
          .foregroundStyle(Color(red: 0.784, green: 0.196, blue: 0.298))
          .frame(maxWidth: .infinity, minHeight: 38, alignment: .leading)
          .padding(.top, 12)

        if !settings.recentServers.isEmpty {
          VStack(alignment: .leading, spacing: 8) {
            Text("Recent servers")
              .font(.system(size: 13, weight: .medium))
              .foregroundStyle(DesignTokens.mutedForeground(colorScheme))

            ForEach(settings.recentServers, id: \.self) { recent in
              Button {
                serverURL = recent
                connect()
              } label: {
                Text(recent)
                  .font(.system(size: 14))
                  .lineLimit(1)
                  .truncationMode(.middle)
                  .frame(maxWidth: .infinity, alignment: .leading)
                  .padding(.horizontal, 12)
                  .frame(height: 36)
                  .overlay {
                    RoundedRectangle(cornerRadius: DesignTokens.radius)
                      .stroke(DesignTokens.border(colorScheme), lineWidth: 1)
                  }
              }
              .buttonStyle(.plain)
              .foregroundStyle(DesignTokens.foreground(colorScheme))
            }
          }
          .padding(.top, 12)
        }
      }
      .frame(maxWidth: 384)

      Spacer(minLength: 24)
    }
    .padding(.horizontal, 16)
    .frame(maxWidth: .infinity, maxHeight: .infinity)
    .background(DesignTokens.background(colorScheme))
  }

  private var primary: Color {
    colorScheme == .dark ? DesignTokens.darkForeground : DesignTokens.lightForeground
  }

  private var primaryForeground: Color {
    colorScheme == .dark ? DesignTokens.lightForeground : .white
  }

  private func connect() {
    guard !isConnecting else { return }
    isConnecting = true
    message = nil

    Task {
      do {
        let normalized = try ServerURL.normalize(serverURL, allowsInsecureHTTP: allowsInsecureHTTP)
        let expanded = await WorkspaceURLExpander.expandIfNeeded(normalized)
        await MainActor.run {
          isConnecting = false
          onConnect(expanded)
        }
      } catch {
        await MainActor.run {
          isConnecting = false
          message = (error as? LocalizedError)?.errorDescription ?? String(describing: error)
        }
      }
    }
  }
}

private let defaultServerURL: String = {
  #if DEBUG
  "http://localhost:6767"
  #else
  "https://"
  #endif
}()

private let allowsInsecureHTTP: Bool = {
  #if DEBUG
  true
  #else
  false
  #endif
}()
