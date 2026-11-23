# Remote browser

The remote browser shows a Chrome that runs on a session's host as a live
picture in the workspace. The session's owner can click, type, scroll and
navigate in it; people the session is shared with can watch. It works in a
plain browser tab and in the desktop app.

It is separate from the desktop app's embedded Browser tab, and the agent's
`browser_*` tools do not drive it. An agent can still use the same Chrome
through any tool that speaks the DevTools protocol, and its work then shows
up in the pane.

## Turn it on

Both halves are needed.

1. On the server, enable the `remote_browser` feature:

   ```bash
   OMNIGENT_FEATURES=remote_browser omnigent server
   ```

2. On the host, start Chrome with a debugging port and a profile of its own,
   then give the runner that address before `omnigent host` starts:

   ```bash
   google-chrome --remote-debugging-port=9222 --user-data-dir="$HOME/.omnigent-remote-browser" &
   export OMNIGENT_REMOTE_BROWSER_CDP_URL=http://127.0.0.1:9222
   omnigent host --server https://omnigent.example.com
   ```

   Add `--headless=new` to run Chrome without a window. The address may be the
   `http://` debugging address or Chrome's `ws://` browser endpoint.

Then open a session on that host and choose **+** > **Remote browser** in the
workspace.

## Limits and safety

- The DevTools protocol has no sign-in, so the runner only accepts an address
  on `localhost`, `127.0.0.1` or `[::1]`. To reach a Chrome on another machine,
  forward its port to the host yourself.
- Anyone who owns a session on the host can drive this Chrome, including any
  site it is signed in to. Use a dedicated profile, not your everyday one.
- The address bar opens `http` and `https` addresses only.
- Chrome's own dialogs cannot be shown in the picture. Alerts and leave-page
  prompts are accepted, confirm and prompt dialogs are dismissed, and the pane
  says which happened.
- The pane follows one tab at a time; pick another from the toolbar when Chrome
  has several. When the same Chrome is open in several windows, the last one
  resized sets the page size for all of them.
- The picture is a stream of JPEG frames relayed through the server, so expect
  more delay than a local browser, growing with the distance to the host.
