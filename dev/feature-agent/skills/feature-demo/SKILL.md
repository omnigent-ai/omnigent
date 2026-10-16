---
name: feature-demo
description: Record at least one captioned demo for every covered CUJ through its real entry point.
---

# Demo recordings

The demo is required. Record **at least one** clip for every covered CUJ; a CUJ
with several entry points or steps may have more. Each clip shows the user
action and the product's response through the CUJ's real entry point, never
pytest output, assertions, test source, or debug logs.

Follow [`dev/recording-lanes.md`](../../../recording-lanes.md) for the recorder,
the per-surface mechanics, and captioning. Use the CUJ ID as the facet and
`demo` as the kind:

- Save each raw capture as `recordings/<ticket-key>/raw/demo-<cuj>.<ext>` (add a
  short suffix, e.g. `demo-cuj-1-edit`, for extra clips of the same CUJ).
- Burn timed captions into each clip with `dev/caption_recording.py`, rendering
  to `recordings/<ticket-key>/demo-<cuj>.mp4`. Captions list the actions shown
  and end with the observed result.
- Play and inspect each render before declaring it in the handoff and PR Demo.
  If captioning fails, use the guide's uncaptioned fallback path and say why in
  that clip's `caption` and the PR Demo. A usable uncaptioned clip still counts.
- For API or configuration CUJs, record a VHS terminal session that runs the
  real CLI or API call and shows its output. Written evidence alone is not a
  demo.
- Build the SPA before starting the recorder. Inside a server-spawned runner
  (`OMNIGENT_RUNNER_ID` is set), strip the inherited runner variables as the
  guide describes, and retry before reporting a blocker.

Never commit recordings; they ride in the PR Demo section and the CI artifact
bundle.

## When a recording can't be made

Recording should almost always work. When a covered CUJ really can't be recorded
(the recorder can't start in this environment, or the surface needs hardware or
an external account the sandbox doesn't have), still publish a **ready** PR and
drive it as usual. For that CUJ:

- set `recording_blocker` to the specific blocker, with the command and error;
- write `manual_test`: step-by-step instructions a human can follow to test the
  journey, including setup, the inputs to give, and what they should see;
- put both in the PR's Demo section in place of the clip.

The outcome is then `partially_implemented`, with that CUJ's recording in
`remaining_work`. Only declare clips you actually produced. Then load
`feature-publish`.
