# Video recordings

Recordings linked in chat and opened from Files render in an inline player with native playback, seeking, volume, and fullscreen controls. Recordings load only after Play. Workspace recordings load their complete bytes, with download and retry controls when playback fails. Remote recordings offer an external-open fallback.

## Sub-features

- `recording-chat`: Markdown links to reachable workspace recordings show a compact 16:10 player up to 320 pixels wide and retain the link to the file viewer.
- `recording-remote`: Direct HTTP links ending in a video extension show a player, including URLs with signed query strings.
- `recording-files`: Video files open in the file viewer rather than the editor or binary placeholder.
- `recording-playback`: Play, pause, seek, and fullscreen use the browser's media controls without automatic playback on mount.
- `recording-failure`: Failed workspace reads and unsupported codecs show a download fallback and Retry; remote recordings offer Open video in new tab.
- `recording-lifecycle`: Workspace bytes load only on Play; closing the file panel or switching files stops playback, cancels pending reads, and releases loaded media without dropping unsaved editor protection. The same viewer moves between desktop and mobile layouts.
- `recording-actions`: Optional WebVTT chapters open in the file side panel, seek to agent actions, and highlight the current step during playback.
- `recording-actions-scroll`: Long action lists scroll within a bounded panel instead of stretching the video card.
- `recording-actions-download`: Download the original optional WebVTT from the recording download menu, with actions open or hidden.
- `recording-actions-collapse`: Hide the action panel to enlarge the video, and reopen it from the shared header without resetting playback.

## How to get to it (user POV)

- Chat: ask the agent to save a recording in the workspace and return a Markdown link labeled `Screen recording` targeting `demo.webm` or its absolute path. Click Play recording in its reply.
- Chat file link: click the recording name in the player header to open the file viewer.
- Unsaved edits remain guarded while the workspace is hidden: `tests/browser_ui/files/test_hidden_file_navigation.py::test_hidden_workspace_guards_unsaved_markdown_navigation`.
- Remote recording, with zero requests before Play: open a conversation containing a direct HTTP MP4, WebM, MOV, M4V, or OGV link.
- Files: open the Files panel, choose Explore or Changes, and select a recording.
- Deep link: open a conversation URL with `?file=demo.webm`.
- Mobile web: use the same deep link or file selection at a narrow viewport; playback stays inline.
- Annotated recording: save a `.chapters.vtt` file beside the video using the [annotation format](../docs/video-recordings.md), then click the recording title in chat to open its file preview. Select an action beside or below the preview player.
- File-preview action panel: use the toggle in the shared video header to hide or reopen it. Choose Download annotations (.vtt) in the recording download menu.

## Driving it with the repro environment

Preconditions: build this checkout and install its Playwright Chromium. The browser-contract lane uses a sealed mock backend and a generated, decodable WebM, so it needs no running server.

- Chat, chat file link, Files browsing, deep link, and mobile playback: `tests/browser_ui/files/test_video_rendering.py::test_workspace_recording_playback`.
- Compact chat cards, desktop/mobile header geometry, keyboard file opening, equal plain/annotated dimensions, stable chat height when navigation opens in the side panel, and optional annotation download controls: `tests/browser_ui/files/test_video_rendering.py::test_recording_header_size_and_optional_controls`.
- Desktop and mobile action navigation, scrolling long action lists, keyboard collapse/reopen, and playback preservation in the file preview: `tests/browser_ui/files/test_video_rendering.py::test_recording_action_navigation`.
- Optional annotations: `tests/browser_ui/files/test_video_rendering.py::test_optional_recording_annotations` covers malformed WebVTT in the file preview; component tests cover truncated responses and stale chapters after a failed refresh. The ordinary playback checks cover missing annotations.
- Moving one player between desktop/mobile layouts, then closing and reopening the desktop file panel during playback: `tests/browser_ui/files/test_video_rendering.py::test_closing_workspace_releases_recording`.
- Unsaved edits remain guarded while the workspace is hidden: `tests/browser_ui/files/test_hidden_file_navigation.py::test_hidden_workspace_guards_unsaved_markdown_navigation`.
- Remote recording, with zero requests before Play: `tests/browser_ui/files/test_video_rendering.py::test_remote_recording_playback`.
- Failed download: `tests/browser_ui/files/test_video_rendering.py::test_recording_download_failure`.
- Run `uv run --no-sync pytest tests/browser_ui/files/test_video_rendering.py --browser-ui-skip-build --video=on --output=/tmp/omnigent-video-evidence`.
- Component checks cover retry, unsupported codecs, cancellation, and object URL cleanup: `pnpm --filter web test src/components/VideoPlayer.test.tsx src/lib/video.test.ts src/components/blocks/ChatMarkdown.fileLinks.test.tsx`.
- Human check: with a runner online, save a real `demo.mp4` or `demo.webm` in its workspace. Ask the agent to reply with a Markdown link labeled `Screen recording` targeting `demo.webm`. Play, pause, seek, enter fullscreen, and download. Open the same recording from both Explore and Changes. Repeat in the embedded UI and on a mobile device.

## Gotchas

- This feature renders existing recordings and agent-supplied WebVTT chapters. Capture, automatic timestamp alignment, auto-zoom, and video transcoding are separate capabilities.
- Actions need video-relative seconds in the companion annotation file; session timestamps alone cannot align a recording. Missing or invalid annotations preserve ordinary playback. Remote video links do not discover annotation files.
- Browser codec support determines playback. A MOV container can contain codecs the browser cannot decode; download remains available.
- Workspace playback buffers the complete file in a Blob before playback. Large recordings need time and memory; bytes are released when the player unmounts.
- Workspace playback and downloads need a runner and resume a sleeping session for callers with edit access. Host-only filesystem previews cannot provide complete video bytes.
- Markdown link syntax is the supported transcript entry point. Raw HTML video tags and image syntax are not recording embeds.
- Video uploads in the composer remain governed by the existing attachment policy.
- Browser-contract recordings use mocked APIs. Supplement them with the real runner and Electron human checks above; native iOS/Android devices and embedded host authentication need separate checks.
