# Floating New Agent panel

On macOS, the desktop app's ⇧⌘Space opens a small New Agent panel over whatever
app is in front, without switching to Omnigent. The user types a prompt, picks
where it runs, and presses ⌘↵: the session starts in an Omnigent window behind
the panel, the panel hides, and the user stays in their app. Escape hides the
panel and keeps the draft. It is behind the `floating_composer` release flag,
off by default.

## Sub-features

- `open`: ⇧⌘Space from any app shows the panel on the pointer's display without
  activating Omnigent; pressing it again while the panel is up focuses it rather
  than hiding it.
- `dismiss`: Escape, the panel's Close button and ⌘W hide it, and so does
  focusing an Omnigent window, except while the panel's file picker is open.
  The draft, attachments and choices survive until the next open.
- `launch`: ⌘↵ (or the send button) creates the session on This Mac in the chosen
  folder, or in a new sandbox. The session opens in an Omnigent window of the same
  server without coming forward, the panel hides, and the first message is
  delivered once. A successful launch clears the panel for the next prompt.
- `launch-failure`: when the first message can't be delivered (the server never
  stored it, or the runner refused it), the panel keeps the prompt and says why.
- `locations`: the panel offers only This Mac and a new sandbox. Remote hosts,
  the import prompt and the page heading are hidden.
- `links`: links in the panel to Settings pages open in an Omnigent window, which
  comes forward.
- `sign-out`: signing out of the server closes its panel.
- `gate`: off unless the server enables `floating_composer`, and only in the
  desktop app on macOS. With it off, ⇧⌘Space is not registered and `/compose/floating`
  shows "Page not found".

## How to get to it (user POV)

- Desktop app on macOS, from any app or Space: press ⇧⌘Space while an Omnigent
  window of a server with the flag on is open. The panel opens for the server of
  the most recently focused Omnigent window.
- Settings → General → Desktop app → **Open New Agent from anywhere** turns
  ⇧⌘Space off or records another chord. The row shows only with the flag on, in a
  desktop app that can show the panel. A chord **Show Omnigent from anywhere**
  already uses is refused.
- There is no menu, Dock or command-palette entry yet.

## Driving it with the repro environment

Preconditions: a Mac, the desktop app built from this checkout, and a server
started with `OMNIGENT_FEATURES=floating_composer`. Browser and E2E lanes can't
drive a second, non-activating desktop window, so the panel is checked by hand.
Follow the [verification skill](skills/verify-omnigent/SKILL.md) for the server.

1. Open the desktop app and sign in. Switch to another app, such as Notes, and
   press ⇧⌘Space. The panel appears over Notes, and Notes stays the active app.
2. Type a prompt and press Escape. The panel hides. Press ⇧⌘Space again: the
   prompt is still there.
3. Pick This Mac and a folder, then press ⌘↵. The panel hides, Notes stays in
   front, and the Omnigent window behind shows the new session, which receives the
   prompt.
4. Repeat with a new sandbox when the server has a sandbox provider.
5. In Settings → General, turn **Open New Agent from anywhere** off: ⇧⌘Space
   reaches other apps. Turn it on, record ⌃⌥Space and allow it: ⌃⌥Space opens the
   panel. Try the **Show Omnigent from anywhere** chord: it's refused.
6. Restart the server without the flag and reload the app. ⇧⌘Space reaches other
   apps again, and `/compose/floating` shows "Page not found".

Component and unit coverage for each part:

- Flag default and `/v1/info` exposure: `tests/server/test_feature_flags.py`.
- The main window announcing that it can show the panel:
  `web/src/hooks/useFloatingComposerSupport.test.ts`.
- The route's flag and desktop gate: `web/src/App.test.tsx`.
- The page, Escape and the launch's delivery, failure and reset:
  `web/src/shell/floatingComposer/FloatingComposerPage.test.tsx` and
  `web/src/shell/floatingComposer/floatingLaunch.test.ts`.
- New Chat inside the panel and its locations:
  `web/src/shell/NewChatDialog.flow.test.tsx` and
  `web/src/shell/floatingComposer/panelDestinations.test.ts`.
- The Settings row and the shortcut calls by id:
  `web/src/pages/settings/GlobalShortcutControl.test.tsx`,
  `web/src/pages/SettingsPage.test.tsx`,
  `web/electron/test/update-main.test.js` and
  `web/electron/test/preload_update.test.js`.
- The desktop panel window, its targeting, frame and shortcut:
  `web/electron/test/floating_composer_window.test.js`,
  `web/electron/test/floating_composer_support.test.js`,
  `web/electron/test/floating_composer_frame.test.js` and
  `web/electron/test/globalShowShortcut.test.js`.

## Gotchas

- macOS only. On Windows and Linux the shortcut is never registered.
- ⇧⌘Space does nothing until an Omnigent window has loaded a page of a server
  with the flag on. Reloading or navigating a window to another server makes it
  announce again; a window still showing an older web bundle doesn't count.
- Turning the flag off on a running server doesn't unregister ⇧⌘Space until the
  Omnigent windows reload.
- The first press after launch builds the panel, so it opens only once the page
  has loaded. Later presses reuse the hidden panel.
- The panel's draft lives in that panel's page: it survives hiding, but not
  quitting the app or a rebuilt panel.
- A panel that needs to sign in again closes instead of signing in inside the panel.
- A second ⇧⌘Space on a visible panel that lost focus may not bring keyboard
  focus back to it; click the panel.
