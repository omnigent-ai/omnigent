# Agent verification recordings

Save a browser-supported video in the session workspace and link it in the
agent's response: `[Screen recording](recordings/verification.webm)`.
Users can play a compact 16:10 recording preview (up to 320 pixels wide) in chat
or open a larger preview in the file viewer. Both workspace and remote recordings
require an explicit Play action before loading video bytes. Chat recordings show no action list. For an
annotated recording, click its title to open navigation in the file side
panel; opening navigation adds no action list to the chat card.

## Optional clickable actions

Use [WebVTT](https://www.w3.org/TR/webvtt/all/), the timed-text format defined
for video chapters and captions. The player uses the browser's WebVTT parser
and chapter cue ranges. No custom JSON annotation schema is required.

Save an optional chapter file next to the recording. Append `.chapters.vtt`
to the entire video filename:

```text
recordings/verification.webm
recordings/verification.webm.chapters.vtt
```

```vtt
WEBVTT

open
00:00.000 --> 00:04.500
Open the application

submit
00:04.500 --> 00:09.000
Submit the form

verify
00:09.000 --> 00:15.000
Confirmation appears
```

- Use elapsed recording times, with millisecond precision, for each cue's start
  and end. Record offsets using the recorder's clock. Adjust them if you trim
  or concatenate footage. Session wall-clock timestamps are not video offsets.
- Use a plain-text action or assertion label. Cue identifiers are optional.
- Use non-overlapping chapter ranges in increasing order. A chapter highlights
  only while playback is inside its range; gaps have no highlighted action.
- The preview accepts up to 200 cues, 64 KiB of WebVTT, and 500 characters per
  displayed label. These are UI limits, not WebVTT format requirements.

The player reads chapters through the authenticated workspace file API.
In the file preview, actions appear beside the video when there is room, and
below it in narrow panels and mobile layouts. Click an action, or focus it and press Enter,
to seek. Selecting an action before loading starts the video at that offset.
Seeking an already loaded video preserves its paused or playing state.
The highlighted action follows playback and native seeking. Actions beyond a
known recording duration are disabled.

The recording title, video download, and action controls share a top toolbar.
Use its action toggle to hide or reopen the panel. The toggle stays in the same
place and preserves playback, keyboard focus, and the selected action. Plain
recordings use the same card width; hiding actions gives annotated recordings
the same player size as plain recordings.

Open the recording download menu and choose **Download annotations (.vtt)**
to save the original companion file. The
annotation download stays available while actions are hidden. Long lists
scroll within the panel. On wide previews, the panel stays as tall as the
video; narrow previews limit the list height. The toolbar stays outside the
scrolling list.


With no companion file, the player shows its ordinary controls and no action
panel. Invalid, empty, oversized, or truncated files also leave ordinary video
playback available. After the agent finishes a turn, an open preview refreshes
the chapter file. Direct remote video links offer **Open video in new tab** instead of a download
promise, since another origin controls its download behavior. They have no chapter-file
discovery; save both files in the workspace to use action navigation.

## Scope

This UI plays existing recordings and optional chapter files. It does not
capture video or infer timestamps from tool calls. WebVTT chapter titles do
not define structured test results; pass/fail summaries and grouped assertions
would need a separate test-report contract. Chapter looping and a segmented
timeline can use the existing WebVTT cue ranges without changing the format.
Workspace video bytes currently load fully before playback; native codec
support determines which files play.
