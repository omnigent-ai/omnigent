# Vega-Lite charts

With the `vega_lite_charts` release flag on, agents' Vega-Lite specs render as
interactive charts in the app theme: fenced `vega-lite` blocks in assistant
messages, and `.vl.json` / `.vega-lite.json` workspace files in the file viewer.
Charts show tooltips, follow light and dark mode, and download as PNG or SVG.

## Sub-features

- `chat-fence`: a closed ` ```vega-lite ` (or ` ```vegalite `) fence in an
  assistant message renders as a chart with a **Chart** header, a **Spec**
  toggle and **PNG** / **SVG** download buttons. While the fence is still
  streaming, the spec shows as code.
- `file-preview`: a `.vl.json` or `.vega-lite.json` file opens as a chart;
  **View source** shows the JSON in the editor and **View preview** returns.
- `interaction`: tooltips on marks that set `tooltip`, plus legend toggles,
  zoom and pan when the spec defines those selections.
- `theme`: axis, legend and title text use the app's colors and redraw when the
  theme changes. Downloads draw on the theme background so they read outside
  the app.
- `safety`: only inline data draws. A spec that loads a URL, a full Vega spec,
  invalid JSON, a sequence or impute range over 100,000 rows, `sequence()` in an
  expression, a custom format type, a number format over 1,000 wide, oversized
  bin, density, quantile or regression settings, a `url(...)` color, a
  non-keyword cursor, or a spec over 1,000,000 characters shows an error with
  the source. While drawing, `sequence()`, `pad()`, `timeSequence()` and
  `utcSequence()` are capped at 100,000 items and `format()` widths at 1,000,
  however the call is written. Colors, cursors, blend modes and backgrounds an
  expression builds into a `url(...)` never reach the page or a download: in the
  first draw they fail the chart, and later they are cleared. Images and links don't load, expressions run in Vega's interpreter,
  inputs stay inside the chart, and a chart can't listen to page elements, keys
  or timers.
- `states`: drawing, drawn, refused or failed to compile (error and source),
  and a truncated file (asks for the source view).
- `gate`: with the flag off, fences stay code blocks and chart files open as JSON.

## How to get to it (user POV)

- Desktop web and desktop app chat: an assistant reply containing a
  `vega-lite` fence, in the main conversation, a grid pane or a sub-agent
  transcript.
- Mobile web and mobile apps chat: the same reply in the session transcript.
- File viewer: open a `.vl.json` or `.vega-lite.json` file from Files, from a
  file link in chat, or when an agent opens the file in the session UI.

## Driving it with the repro environment

Preconditions: enable `OMNIGENT_FEATURES=vega_lite_charts` on the test server
before launch and follow the [verification skill](skills/verify-omnigent/SKILL.md).

Script the mock model to reply with a short sentence followed by a `vega-lite`
fence holding a bar chart with inline `data.values` and `"tooltip": true` on
the mark. Confirm the bars draw, hovering a bar shows its fields, **Spec** shows
the JSON and **PNG** and **SVG** save files that open in an image viewer. Switch
the theme and confirm the axis text recolors. Script a second reply whose spec
uses `data.url` and confirm the error names inline data and shows the spec.
Have the agent write the same spec to `chart.vl.json`, open it from Files, and
toggle **View source** / **View preview**. Restart the server without the flag
and reload: the fence shows as code and the file opens as JSON.

- Chat fence, tooltip, PNG export and blocked remote image in real Chromium:
  `tests/browser_ui/files/test_vega_lite_charts.py::test_chat_fence_draws_interactive_chart_and_exports_png`.
- File preview and source toggle in real Chromium:
  `tests/browser_ui/files/test_vega_lite_charts.py::test_chart_file_previews_as_chart_with_source_toggle`.
  Both use a mocked API with the flag on.
- Release flag and `/v1/info` exposure:
  `tests/server/test_feature_flags.py` and
  `tests/server/integration/test_utility_endpoints.py`.
- Spec checks, rendering, theme, loader and tooltip escaping:
  `web/src/components/charts/vegaLiteSpec.test.ts`,
  `web/src/components/charts/vegaLiteRuntime.test.ts` and
  `web/src/components/charts/VegaLiteChart.test.tsx`.
- Chat gating and streaming: `web/src/components/ai-elements/message.test.tsx`
  and `web/src/components/blocks/BlockRenderer.test.tsx`. File routing:
  `web/src/shell/CodeViewer.test.tsx` and `web/src/shell/FileViewer.test.tsx`.

## Gotchas

- Only assistant text draws charts. The same fence in a user message, a tool
  result or a pull request comment stays code.
- The renderer loads on the first chart. A tab held across a redeploy can fail
  to load it; the chart then shows an error with its source, and a reload fixes it.
- The flag is read at page boot; reload after changing `OMNIGENT_FEATURES`.
- A spec's own `config` overrides the theme colors, so a spec that sets its own
  colors won't follow dark mode.
- The file viewer's preview/source choice is shared with HTML, notebooks and
  Markdown: after choosing source for one of them, a chart file also opens in source.
- Charts draw on the page's main thread. The checks stop common runaway specs,
  but a spec built to be slow, such as a costly expression, can still stall the tab.
- Timer-driven animations don't run, because charts can't use timers.
- **View data** tables, CSV download and an agent-facing skill aren't built yet.
