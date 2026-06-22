# Omnigent iOS

Thin SwiftUI/WKWebView shell for Omnigent. Like the Electron app, this target
loads the server-served web UI instead of shipping a duplicate copy of the SPA.

## Development

Open `Omnigent.xcodeproj` in Xcode 16 or newer and run the `Omnigent` scheme on
an iOS 18 simulator.

Debug builds allow `http://` web content for local development by enabling
`NSAllowsArbitraryLoadsInWebContent`. Release builds keep App Transport
Security defaults and require remote servers to use `https://`.

## Scope

The first version provides native setup chrome, recent servers, WKWebView
loading, foreground local notifications, app badge updates, and notification
tap routing back into the SPA. It does not implement APNs, background polling,
or localhost proxy/CORS behavior.
