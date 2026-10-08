# Video recordings

Recordings linked in chat and opened from Files render in an inline player with native playback, seeking, volume, and fullscreen controls. Workspace recordings load their complete bytes on Play, with download and retry controls when playback fails.

## Sub-features

- `recording-chat`: Markdown links to reachable workspace recordings show a player and retain the link to the file viewer.
- `recording-remote`: Direct HTTP links ending in a video extension show a player, including URLs with signed query strings.
- `recording-files`: Video files open in the file viewer rather than the editor or binary placeholder.
- `recording-playback`: Play, pause, seek, and fullscreen use the browser's media controls without automatic playback on mount.
- `recording-failure`: Failed reads and unsupported codecs show a download fallback and Retry.
- `recording-lifecycle`: Workspace bytes load only on Play; switching files cancels pending reads and releases loaded media.

## How to get to it (user POV)

- Chat: ask the agent to save a recording in the workspace and return a Markdown link labeled `Screen recording` targeting `demo.webm` or its absolute path. Click Play recording in its reply.
- Chat file link: click the recording's link below the inline player to open the file viewer.
- Remote recording: open a conversation containing a direct HTTP MP4, WebM, MOV, M4V, or OGV link.
- Files: open the Files panel, choose Explore or Changes, and select a recording.
- Deep link: open a conversation URL with `?file=demo.webm`.
- Mobile web: use the same deep link or file selection at a narrow viewport; playback stays inline.

## Driving it with the repro environment

Preconditions: build this checkout and install its Playwright Chromium. The browser-contract lane uses a sealed mock backend and a generated, decodable WebM, so it needs no running server.

- Chat, chat file link, Files browsing, deep link, and mobile playback: `tests/browser_ui/files/test_video_rendering.py::test_workspace_recording_playback`.
- Remote recording: `tests/browser_ui/files/test_video_rendering.py::test_remote_recording_playback`.
- Failed download: `tests/browser_ui/files/test_video_rendering.py::test_recording_download_failure`.
- Run `uv run --no-sync pytest tests/browser_ui/files/test_video_rendering.py --browser-ui-skip-build --video=on --output=/tmp/omnigent-video-evidence`.
- Component checks cover retry, unsupported codecs, cancellation, and object URL cleanup: `pnpm --filter web test src/components/VideoPlayer.test.tsx src/lib/video.test.ts src/components/blocks/ChatMarkdown.fileLinks.test.tsx`.
- Human check: with a runner online, save a real `demo.mp4` or `demo.webm` in its workspace. Ask the agent to reply with a Markdown link labeled `Screen recording` targeting `demo.webm`. Play, pause, seek, enter fullscreen, and download. Open the same recording from both Explore and Changes. Repeat in the embedded UI and on a mobile device.

## Gotchas

- This feature renders existing recordings. Capture, annotation, auto-zoom, and video transcoding are separate capabilities.
- Browser codec support determines playback. A MOV container can contain codecs the browser cannot decode; download remains available.
- Workspace playback buffers the complete file in a Blob before playback. Large recordings need time and memory; bytes are released when the player unmounts.
- The raw download endpoint needs a runner. Host-only offline filesystem previews cannot provide complete video bytes.
- Markdown link syntax is the supported transcript entry point. Raw HTML video tags and image syntax are not recording embeds.
- Video uploads in the composer remain governed by the existing attachment policy.
- Browser-contract recordings use mocked APIs. Supplement them with the real runner and Electron human checks above; native iOS/Android devices and embedded host authentication need separate checks.
