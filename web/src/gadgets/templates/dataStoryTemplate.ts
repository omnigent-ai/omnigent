// Data story template: a kit plus a view between the VIEW_START and VIEW_END markers, which the
// gadget session replaces with its design. The scripts are plain ES2018 inside template literals:
// keep them free of backticks, `${`, and unescaped backslashes.

import type { CreateGadgetRevisionRequest } from "../api/gadgetApi";
import {
  GADGET_KIT_HELPERS,
  GADGET_KIT_STYLE,
  GADGET_LAYOUT_SCHEMA,
  GADGET_RENDER_REPORT_SCHEMA,
} from "./templateKit";

export const DATA_STORY_IDEAS_COLLECTION = "ideas";
export const DATA_STORY_COLLECTION = "story";
export const DATA_STORY_SERIES_COLLECTION = "series";
export const DATA_STORY_LAYOUT_COLLECTION = "layout";
export const DATA_STORY_REPORTS_COLLECTION = "render_reports";
// The one record in `ideas`, `story` and `layout`; each search, build or design replaces it.
export const DATA_STORY_RECORD_ID = "current";
export const DATA_STORY_VIEW_START = "<!-- story:view:start -->";
export const DATA_STORY_VIEW_END = "<!-- story:view:end -->";

const id = (max: number) => ({ type: "string", minLength: 1, maxLength: max });
const text = (max: number) => ({ type: "string", maxLength: max });
const time = { type: "string", minLength: 10, maxLength: 40 };

const citedSessions = {
  type: "array",
  maxItems: 6,
  items: {
    type: "object",
    additionalProperties: false,
    required: ["id"],
    properties: { id: id(128), title: text(160), quote: text(240) },
  },
};

const sources = {
  type: "array",
  maxItems: 8,
  items: {
    type: "object",
    additionalProperties: false,
    required: ["name"],
    properties: {
      name: id(160),
      kind: text(30),
      detail: text(400),
      as_of: text(40),
    },
  },
};

// Story ideas from the latest search, one record.
export const DATA_STORY_IDEAS_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["scanned_at", "window_days", "ideas"],
  properties: {
    scanned_at: time,
    window_days: { type: "integer", minimum: 1, maximum: 60 },
    sessions_read: { type: "integer", minimum: 0, maximum: 500 },
    note: text(300),
    example: { type: "boolean" },
    ideas: {
      type: "array",
      maxItems: 6,
      items: {
        type: "object",
        additionalProperties: false,
        required: ["id", "title", "question"],
        properties: {
          id: id(64),
          title: id(90),
          question: id(240),
          why: text(400),
          chart_idea: text(200),
          effort: { type: "string", enum: ["quick", "medium", "deep"] },
          sources,
          sessions: citedSessions,
        },
      },
    },
  },
} satisfies Record<string, unknown>;

// The built story's words and structure, one record. Chart data lives in `series` records, so the
// story stays well under a record's size limit.
export const DATA_STORY_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["title", "question", "built_at", "sections"],
  properties: {
    idea_id: text(64),
    title: id(120),
    question: id(240),
    built_at: time,
    refreshed_at: text(40),
    example: { type: "boolean" },
    headline: {
      type: "object",
      additionalProperties: false,
      required: ["label", "value"],
      properties: {
        label: id(120),
        value: id(40),
        unit: text(20),
        delta: text(60),
        direction: { type: "string", enum: ["up", "down", "flat"] },
        good: { type: "string", enum: ["up", "down", "none"] },
        context: text(200),
      },
    },
    summary: text(1200),
    sections: {
      type: "array",
      maxItems: 8,
      items: {
        type: "object",
        additionalProperties: false,
        required: ["id", "heading"],
        properties: {
          id: id(64),
          heading: id(120),
          body: text(1500),
          chart: text(64),
          takeaway: text(300),
        },
      },
    },
    sources,
    sessions: citedSessions,
    caveats: text(800),
  },
} satisfies Record<string, unknown>;

// One chart's data, with the chart's ID as the record ID.
export const DATA_STORY_SERIES_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["kind", "series"],
  properties: {
    title: text(120),
    kind: { type: "string", enum: ["line", "area", "bar", "column"] },
    x_label: text(60),
    y_label: text(60),
    unit: text(20),
    series: {
      type: "array",
      minItems: 1,
      maxItems: 6,
      items: {
        type: "object",
        additionalProperties: false,
        required: ["name", "points"],
        properties: {
          name: id(60),
          points: {
            type: "array",
            maxItems: 400,
            items: {
              type: "object",
              additionalProperties: false,
              required: ["x", "y"],
              properties: { x: id(40), y: { type: "number" } },
            },
          },
        },
      },
    },
    annotations: {
      type: "array",
      maxItems: 8,
      items: {
        type: "object",
        additionalProperties: false,
        required: ["x", "label"],
        properties: { x: id(40), series: text(60), label: id(120) },
      },
    },
  },
} satisfies Record<string, unknown>;

const KIT_STYLE = `
${GADGET_KIT_STYLE}
:root { --s1: #4f46e5; --s2: #0891b2; --s3: #d97706; --s4: #db2777; --s5: #16a34a; --s6: #64748b; --up: #16a34a; --down: #dc2626; }
body[data-theme='dark'] { --s1: #8b84ff; --s2: #22d3ee; --s3: #fbbf24; --s4: #f472b6; --s5: #4ade80; --s6: #94a3b8; --up: #4ade80; --down: #f87171; }
input[type='text'] { font: inherit; color: inherit; background: var(--surface); border: 1px solid var(--line); border-radius: 8px; padding: 6px 10px; min-width: 0; }
input[type='text']:focus-visible { outline: 2px solid var(--focus); outline-offset: 1px; }
.muted { color: var(--muted); }

/* Story ideas */
.ideas-box { background: var(--surface); border: 1px solid var(--line); border-radius: 14px; padding: 4px 16px 14px; }
.ideas-box > summary { cursor: pointer; font-weight: 600; padding: 10px 0; }
.ideas-note { margin: 0 0 12px; color: var(--muted); font-size: 13px; }
.ideas { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 12px; }
.idea { display: flex; flex-direction: column; gap: 6px; padding: 12px 14px; border: 1px solid var(--line); border-radius: 12px; background: var(--bg); }
.idea[data-picked='true'] { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
.idea h3 { font-size: 15px; }
.idea .question { margin: 0; }
.idea .why { margin: 0; color: var(--muted); font-size: 13px; }
.idea .chips { display: flex; flex-wrap: wrap; gap: 4px; }
.chip { font-size: 11.5px; padding: 1px 8px; border-radius: 999px; background: var(--faint); color: var(--muted); }
.chip[data-effort='quick'] { background: var(--done-soft); color: var(--done); }
.chip[data-effort='deep'] { background: var(--waiting-soft); color: var(--waiting); }
.idea .cited { font-size: 12.5px; color: var(--muted); }
.idea .build { margin-top: auto; align-self: flex-start; }
.custom { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-top: 14px; }
.custom label { font-weight: 600; font-size: 13px; }
.custom input { flex: 1; min-width: 200px; }

/* Building */
.building { display: grid; grid-template-columns: minmax(0, 1fr) 180px; gap: 18px; align-items: center; padding: 18px 20px; background: var(--surface); border: 1px solid var(--accent); border-radius: 14px; }
.building h2 { margin: 0 0 4px; font-size: 17px; }
.building .what { margin: 0 0 8px; color: var(--muted); }
.progress li[data-done='true'] { color: var(--text); }

/* The plain story */
.story { display: grid; gap: 26px; }
.story-top { display: grid; gap: 14px; }
.headline-card { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 6px 22px; align-items: center; padding: 18px 20px; background: var(--surface); border: 1px solid var(--line); border-radius: 14px; }
.headline-card .big { font-size: 44px; line-height: 1; font-weight: 700; letter-spacing: -0.02em; font-variant-numeric: tabular-nums; }
.headline-card .big small { font-size: 18px; font-weight: 600; color: var(--muted); margin-left: 4px; }
.headline-card .label { font-weight: 600; }
.headline-card .delta { font-weight: 600; font-variant-numeric: tabular-nums; }
.headline-card .delta[data-tone='good'] { color: var(--up); }
.headline-card .delta[data-tone='bad'] { color: var(--down); }
.headline-card .context { color: var(--muted); font-size: 13px; }
.summary-text { font-size: 16px; line-height: 1.6; max-width: 720px; margin: 0; }
.section { display: grid; gap: 10px; }
.section h2 { font-size: 18px; margin: 0; }
.section .body { margin: 0; max-width: 720px; line-height: 1.6; }
.takeaway { margin: 0; padding: 8px 12px; border-left: 3px solid var(--accent); background: var(--accent-soft); border-radius: 0 8px 8px 0; max-width: 720px; }
.chart-box { background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 12px 14px 8px; }
.chart-box h3 { font-size: 13px; margin: 0 0 6px; color: var(--muted); font-weight: 600; }
.chart.chart { width: 100%; height: auto; display: block; overflow: visible; }
.chart .grid { stroke: var(--faint); stroke-width: 1; }
.chart .axis-label { fill: var(--muted); font-size: 11px; }
.chart .line { fill: none; stroke-width: 2.2; stroke-linejoin: round; stroke-linecap: round; }
.chart .area-fill { opacity: 0.14; }
.chart .dot { stroke: var(--surface); stroke-width: 1.5; }
.chart .dot:focus, .chart .bar:focus { outline: none; stroke: var(--text); stroke-width: 2; }
.chart .bar { opacity: 0.9; }
.chart .note-line { stroke: var(--muted); stroke-dasharray: 3 4; }
.chart .note-text { fill: var(--text); font-size: 11.5px; font-weight: 600; paint-order: stroke; stroke: var(--surface); stroke-width: 4px; stroke-linejoin: round; }
.chart .note-dot { fill: var(--accent); }
.chart .draw { stroke-dasharray: var(--len); stroke-dashoffset: var(--len); animation: draw 900ms ease-out forwards; }
@keyframes draw { to { stroke-dashoffset: 0; } }
.legend { display: flex; flex-wrap: wrap; gap: 6px 12px; margin-top: 4px; font-size: 12px; }
.legend button { display: inline-flex; align-items: center; gap: 6px; border: none; background: none; padding: 2px 0; cursor: pointer; color: var(--text); }
.legend button[aria-pressed='false'] { color: var(--muted); text-decoration: line-through; }
.legend i { width: 10px; height: 10px; border-radius: 3px; display: inline-block; }
.tip { position: fixed; z-index: 5; pointer-events: none; padding: 4px 8px; border-radius: 6px; background: var(--text); color: var(--bg); font-size: 12px; white-space: nowrap; }
.about { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 14px 28px; padding: 14px 16px; background: var(--surface); border: 1px solid var(--line); border-radius: 14px; font-size: 13px; }
.about h3 { font-size: 13px; margin: 0 0 6px; }
.about ul { margin: 0; padding-left: 18px; color: var(--muted); }
.about li { margin: 3px 0; }
.about .caveats { grid-column: 1 / -1; color: var(--muted); margin: 0; }

/* The empty state's chart */
.story-art { width: 100%; height: auto; display: block; }
.story-art .grid { stroke: var(--line); stroke-width: 1; stroke-dasharray: 3 5; }
.story-art .bar { fill: var(--accent-soft); }
.story-art .trend { fill: none; stroke: var(--accent); stroke-width: 3; stroke-linecap: round; stroke-dasharray: 320; stroke-dashoffset: 320; animation: trace 3.6s ease-in-out infinite; }
.story-art .pin { fill: var(--waiting); animation: pin 3.6s ease-in-out infinite; }
@keyframes trace { 0% { stroke-dashoffset: 320; } 55%, 100% { stroke-dashoffset: 0; } }
@keyframes pin { 0%, 50% { opacity: 0; } 65%, 100% { opacity: 1; } }
@media (max-width: 640px) {
  .ideas { grid-template-columns: minmax(0, 1fr); }
  .building { grid-template-columns: minmax(0, 1fr); }
  .building .story-art { display: none; }
  .about { grid-template-columns: minmax(0, 1fr); }
  .headline-card { grid-template-columns: minmax(0, 1fr); }
}
`;

const KIT_SCRIPT = `
(function () {
  'use strict';
${GADGET_KIT_HELPERS}
  var OPEN = ['pending', 'queued', 'running', 'needs_input'];
  var PENDING_MS = 10 * 60 * 1000;
  var FIND_TEXT = {
    pending: 'Create your gadget session, and it looks.',
    queued: 'Waiting for your gadget session to pick it up…',
    running: 'Reading your recent sessions for data work…',
    needs_input: 'Your gadget session is waiting for you.',
    done: 'Story ideas ready',
    failed: 'Could not find story ideas',
    cancelled: 'Cancelled',
    finished_unreported: 'Your gadget session stopped before it reported. Ask again.',
    interrupted: 'It stopped before it finished. Ask again.',
  };
  var BUILD_TEXT = {
    pending: 'Create your gadget session, and it builds the story.',
    queued: 'Waiting for your gadget session to pick it up…',
    running: 'Building your story…',
    needs_input: 'Your gadget session needs you to go on: open it.',
    done: 'Story built',
    failed: 'Could not build the story',
    cancelled: 'Cancelled',
    finished_unreported: 'Your gadget session stopped before it reported. Build it again.',
    interrupted: 'It stopped before it finished. Build it again.',
  };
  var REFRESH_TEXT = {
    pending: 'Create your gadget session, and it refreshes the data.',
    queued: 'Waiting for your gadget session to pick it up…',
    running: 'Refreshing the data…',
    needs_input: 'Your gadget session needs you to go on: open it.',
    done: 'Data refreshed',
    failed: 'Could not refresh the data',
    cancelled: 'Cancelled',
    finished_unreported: 'Your gadget session stopped before it reported.',
    interrupted: 'It stopped before it finished.',
  };
  var RESTORE_TEXT = { pending: 'Create your gadget session, and it goes back.', running: 'Going back to the previous look…' };

  var client = null;
  // Shown on the control that asked for the gadget session while that session is being created.
  var STARTING_TEXT = 'Starting session…';
  function sessionStarting() {
    return Boolean(client && client.agent && typeof client.agent.starting === 'function' && client.agent.starting());
  }
  var preview = false;
  var canWrite = false;
  var canAct = false;
  var collections = {};
  var ideasDoc = null;
  var storyDoc = null;
  var layoutDoc = null;
  var seriesDocs = dict();
  var loaded = { ideas: false, story: false };
  var actionItems = [];
  var seen = {};
  var pending = dict();
  var openSince = dict();
  var renderQueued = false;
  var viewDirty = true;
  var previewSeeded = false;
  var lastWidth = 0;
  var hiddenSeries = dict();

  // ---- The view, and the fallback to the plain one

  var customView = null;
  var shownView = null;
  var viewFailure = null;
  var started = false;
  var reported = dict();

  // A design that throws while it loads or draws is replaced by the plain view, its styles are
  // switched off, and the page reports it in render_reports for the gadget session.
  function failView(error) {
    if (viewFailure) return;
    viewFailure = { text: message(error || 'unknown error').slice(0, 300) };
    Array.prototype.forEach.call(document.querySelectorAll('style'), function (node) {
      if (node.id !== 'story-kit-style') node.setAttribute('media', 'not all');
    });
    if (started) { report('failed', viewFailure.text); schedule(); }
  }
  window.addEventListener('error', function (event) {
    if (customView || !started) failView((event && (event.error || event.message)) || 'unknown error');
  });
  window.addEventListener('unhandledrejection', function (event) {
    if (customView) failView((event && event.reason) || 'unhandled rejection');
  });

  function report(state, detail) {
    var reports = collections.render_reports;
    if (preview || !canWrite || !reports || !client) return;
    var revision = client.context.revisionId;
    if (!revision || reported[revision] === state) return;
    reported[revision] = state;
    var value = { state: state, at: new Date().toISOString() };
    if (customView && customView.name) value.view = String(customView.name).slice(0, 80);
    if (detail) value.error = String(detail).slice(0, 400);
    reports.get(revision).then(function (doc) {
      if (!doc) return reports.create(value, { id: revision });
      if (doc.value && doc.value.state === state) return null;
      return reports.replace(revision, value, { ifVersion: doc.version });
    }).catch(function () {
      // Not saved: forget it, so the next draw reports it again and the agent still learns.
      if (reported[revision] === state) delete reported[revision];
    });
  }

  function drawView() {
    var root = $('view');
    var value = story();
    if (!value) { root.replaceChildren(); shownView = null; return; }
    var view = customView && !viewFailure ? customView : plainView;
    if (view !== shownView) { root.replaceChildren(); shownView = view; }
    syncKit();
    try {
      view.render(root, viewModel(), kit);
      if (view === customView && !value.example) report('ok', '');
      // A failure whose report didn't save is reported again on the next draw.
      else if (viewFailure && !value.example) report('failed', viewFailure.text);
    } catch (error) {
      if (view !== customView) throw error;
      failView(error);
      root.replaceChildren();
      shownView = plainView;
      plainView.render(root, viewModel(), kit);
    }
  }

  function viewModel() {
    return {
      story: story(),
      sections: sections(),
      series: seriesById(),
      layout: layoutValue(),
      revisionId: client ? client.context.revisionId : '',
      width: Math.round($('view').clientWidth || 0),
      preview: preview,
    };
  }

  // ---- The records

  function ideas() { return ideasDoc && ideasDoc.value && Array.isArray(ideasDoc.value.ideas) ? ideasDoc.value : null; }
  function ideaList() { var value = ideas(); return value ? value.ideas.filter(function (idea) { return idea && idea.id && idea.title; }) : []; }
  function story() { return storyDoc && storyDoc.value && Array.isArray(storyDoc.value.sections) ? storyDoc.value : null; }
  function sections() { var value = story(); return value ? value.sections.filter(function (item) { return item && item.id && item.heading; }) : []; }
  function layoutValue() { return layoutDoc && layoutDoc.value && layoutDoc.value.revision_id ? layoutDoc.value : null; }
  function seriesById() {
    var out = dict();
    Object.keys(seriesDocs).forEach(function (key) {
      var value = seriesDocs[key].value;
      if (value && Array.isArray(value.series)) out[key] = value;
    });
    return out;
  }
  function ref(path) { return path; }

  // ---- Actions and their progress

  function latest(name) {
    var found = null;
    actionItems.forEach(function (item) { if (item.action === name) found = item; });
    return found;
  }
  function isOpen(action) { return Boolean(action) && OPEN.indexOf(action.status) !== -1; }
  // The run to show for an action: one a session is working on, else the latest. A later request
  // the session merged into an open one is already done, but the open one still runs.
  function shownRun(name) {
    var working = null;
    actionItems.forEach(function (item) {
      if (item.action === name && isOpen(item) && item.status !== 'pending') working = item;
    });
    return working || latest(name);
  }

  function onActions(snapshot) {
    if (snapshot.status === 'loading') return;
    var items = snapshot.items || [];
    var key = items.map(function (item) { return [item.id, item.status, item.summary || ''].join(':'); }).join('|');
    if (key === seen.actions) return;
    seen.actions = key;
    actionItems = items;
    schedule();
  }

  function windowDays() { return Number($('window').value) || 7; }
  function textFor(name) {
    return name === 'build_story' ? BUILD_TEXT : name === 'refresh_data' ? REFRESH_TEXT : name === 'restore_look' ? RESTORE_TEXT : FIND_TEXT;
  }

  // Resolves whether the request reached the gadget session (or New session).
  function request(name, params, label) {
    if (!canAct) return Promise.resolve(false);
    var entry = { at: Date.now(), text: 'Sending to your gadget session…', label: label || '' };
    pending[name] = entry;
    schedule();
    return client.agent.request(name, params).then(function (receipt) {
      if (pending[name] !== entry) return true;
      entry.actionId = receipt && receipt.actionId;
      entry.starting = Boolean(receipt && receipt.delivery === 'starting');
      entry.text = entry.starting ? textFor(name).pending : 'Sent to your gadget session.';
      schedule();
      return true;
    }, function (error) {
      if (pending[name] !== entry) return false;
      delete pending[name];
      notice('Could not send that to your gadget session: ' + message(error), 'error');
      schedule();
      return false;
    });
  }

  function findStories() { request('find_stories', { window_days: windowDays() }); }
  function buildIdea(idea) { request('build_story', { idea_id: idea.id, title: idea.title }, idea.title); }
  function buildCustom(textValue) {
    var asked = String(textValue || '').trim().slice(0, 500);
    if (!asked) return Promise.resolve(false);
    return request('build_story', { request: asked }, asked);
  }
  function refreshData() { request('refresh_data', {}); }

  // Where one action stands: open (sent or running), waiting for the session to start, or not.
  function runState(name) {
    var action = shownRun(name);
    var entry = pending[name];
    // The ledger can report the action before request() resolves, so match either way round.
    if (entry && (Date.now() - entry.at > PENDING_MS || (action && entry.actionId === action.id))) { delete pending[name]; entry = null; }
    var open = Boolean(entry && !entry.starting) || (isOpen(action) && action.status !== 'pending');
    var undelivered = Boolean(entry && entry.starting) || (!entry && Boolean(action) && action.status === 'pending');
    var working = open && Boolean(action && action.status === 'running');
    // Count from the request in the ledger, so a page reloaded mid-run (as after a design
    // publishes) keeps counting instead of starting again.
    var requested = action && action.requestedAt ? Date.parse(action.requestedAt) : NaN;
    if (open && !isNaN(requested)) openSince[name] = Math.min(requested, Date.now());
    else if (open && !openSince[name]) openSince[name] = entry ? entry.at : Date.now();
    if (!open) openSince[name] = 0;
    var failedWhy = action && action.summary && (action.status === 'failed' || action.status === 'cancelled') ? ': ' + action.summary : '';
    var words = textFor(name);
    var textValue = entry ? entry.text : action && words[action.status] ? words[action.status] + failedWhy : '';
    var label = (entry && entry.label) || (action && action.params && (action.params.title || action.params.request)) || '';
    return { action: action, open: open, undelivered: undelivered, working: working, requested: requested, text: textValue, label: label, since: openSince[name] };
  }

  // The revision Back to the previous look returns to, when the page knows it.
  function previousRevision() {
    var layout = layoutValue();
    if (!layout || !client) return '';
    return layout.revision_id === client.context.revisionId ? layout.previous_revision_id || '' : '';
  }
  function requestRestore() {
    var revision = previousRevision();
    request('restore_look', revision ? { revision_id: revision } : {});
  }

  function openSession(sessionId) {
    client.navigation.openSession(sessionId, { display: 'panel' }).catch(function (error) {
      notice('Could not open that session: ' + message(error), 'error');
    });
  }

  // ---- Pieces a design reuses

  function format(value, unit) {
    if (typeof value !== 'number' || !isFinite(value)) return String(value);
    var abs = Math.abs(value);
    var out = abs >= 1e9 ? (value / 1e9).toFixed(1) + 'B' : abs >= 1e6 ? (value / 1e6).toFixed(1) + 'M'
      : abs >= 1e4 ? (value / 1e3).toFixed(1) + 'k' : abs >= 100 ? String(Math.round(value)) : String(Math.round(value * 100) / 100);
    out = out.replace(/[.]0([kMB])$/, '$1');
    return out + (unit ? (unit === '%' ? '%' : ' ' + unit) : '');
  }
  // A linear scale from [d0, d1] to [r0, r1].
  function scale(d0, d1, r0, r1) {
    var span = d1 - d0 || 1;
    return function (value) { return r0 + ((value - d0) / span) * (r1 - r0); };
  }
  function color(index) { return 'var(--s' + ((index % 6) + 1) + ')'; }
  var tipBox = null;
  function tip(node, textValue) {
    function show(event) {
      tipBox = tipBox || document.body.appendChild(el('div', { class: 'tip', role: 'tooltip' }));
      tipBox.textContent = textValue;
      var rect = node.getBoundingClientRect();
      var x = event && event.clientX ? event.clientX : rect.left + rect.width / 2;
      var y = event && event.clientY ? event.clientY : rect.top;
      tipBox.style.left = Math.max(4, x + 10) + 'px';
      tipBox.style.top = Math.max(4, y - 30) + 'px';
    }
    function hide() { if (tipBox) { tipBox.remove(); tipBox = null; } }
    node.addEventListener('mouseenter', show);
    node.addEventListener('mousemove', show);
    node.addEventListener('focus', show);
    node.addEventListener('mouseleave', hide);
    node.addEventListener('blur', hide);
    return node;
  }

  // A chart for one series record: line, area, bar or column, with annotations, a legend that
  // hides and shows series, and each point's value on hover or focus.
  function chart(chartId, options) {
    var data = seriesById()[chartId];
    if (!data) return el('p', { class: 'muted', text: 'No data for this chart yet.' });
    var width = Math.max(280, Math.min(900, (options && options.width) || $('view').clientWidth || 640));
    var height = (options && options.height) || Math.round(Math.max(180, Math.min(300, width * 0.42)));
    var pad = { l: 46, r: 16, t: 16, b: 28 };
    var visible = data.series.filter(function (entry) { return !hiddenSeries[chartId + '/' + entry.name]; });
    var xs = [];
    data.series.forEach(function (entry) { entry.points.forEach(function (point) { if (xs.indexOf(point.x) === -1) xs.push(point.x); }); });
    var ys = [];
    visible.forEach(function (entry) { entry.points.forEach(function (point) { ys.push(point.y); }); });
    var bars = data.kind === 'bar' || data.kind === 'column';
    var lo = ys.length ? Math.min.apply(null, ys) : 0;
    var hi = ys.length ? Math.max.apply(null, ys) : 1;
    // Bars grow from zero, so zero is in the range at either end; all-negative bars too.
    if (bars || (lo > 0 && lo < hi * 0.35)) lo = Math.min(0, lo);
    if (bars) hi = Math.max(0, hi);
    if (hi === lo) hi = lo + 1;
    var y = scale(lo, hi, height - pad.b, pad.t);
    var band = (width - pad.l - pad.r) / Math.max(1, xs.length);
    var x = function (key) { return pad.l + band * xs.indexOf(key) + band / 2; };
    var node = svg('svg', { class: 'chart', viewBox: '0 0 ' + width + ' ' + height, role: 'img', 'aria-label': data.title || 'Chart' });
    [0, 0.5, 1].forEach(function (step) {
      var value = lo + (hi - lo) * step;
      node.appendChild(svg('line', { class: 'grid', x1: pad.l, x2: width - pad.r, y1: y(value), y2: y(value) }));
      node.appendChild(svg('text', { class: 'axis-label', x: pad.l - 6, y: y(value) + 4, 'text-anchor': 'end', text: format(value) }));
    });
    var every = Math.max(1, Math.ceil(xs.length / Math.max(2, Math.floor(width / 90))));
    xs.forEach(function (key, index) {
      if (index % every) return;
      node.appendChild(svg('text', { class: 'axis-label', x: x(key), y: height - 8, 'text-anchor': 'middle', text: String(key).slice(0, 12) }));
    });
    var unit = data.unit || '';
    visible.forEach(function (entry) {
      var stroke = color(data.series.indexOf(entry));
      if (bars) {
        var slot = (band * 0.7) / visible.length;
        entry.points.forEach(function (point) {
          var left = x(point.x) - band * 0.35 + slot * visible.indexOf(entry);
          var bar = svg('rect', { class: 'bar', x: left, y: Math.min(y(0), y(point.y)), width: Math.max(1, slot - 2), height: Math.max(1, Math.abs(y(0) - y(point.y))), rx: 2, fill: stroke, tabindex: 0 });
          node.appendChild(tip(bar, entry.name + ' · ' + point.x + ': ' + format(point.y, unit)));
        });
        return;
      }
      var path = entry.points.map(function (point, at) { return (at ? 'L' : 'M') + x(point.x).toFixed(1) + ' ' + y(point.y).toFixed(1); }).join(' ');
      if (data.kind === 'area' && entry.points.length) {
        var first = entry.points[0];
        var last = entry.points[entry.points.length - 1];
        node.appendChild(svg('path', { class: 'area-fill', fill: stroke, d: path + ' L' + x(last.x).toFixed(1) + ' ' + y(lo).toFixed(1) + ' L' + x(first.x).toFixed(1) + ' ' + y(lo).toFixed(1) + ' Z' }));
      }
      node.appendChild(svg('path', { class: 'line' + (calm ? '' : ' draw'), stroke: stroke, d: path, style: '--len:' + Math.ceil(width * 2) }));
      entry.points.forEach(function (point) {
        var dot = svg('circle', { class: 'dot', cx: x(point.x), cy: y(point.y), r: entry.points.length > 60 ? 2 : 3.5, fill: stroke, tabindex: 0 });
        node.appendChild(tip(dot, entry.name + ' · ' + point.x + ': ' + format(point.y, unit)));
      });
    });
    (data.annotations || []).forEach(function (note) {
      if (xs.indexOf(note.x) === -1) return;
      var target = visible.filter(function (entry) { return !note.series || entry.name === note.series; })[0];
      var point = target ? target.points.filter(function (entry) { return entry.x === note.x; })[0] : null;
      var at = point ? y(point.y) : pad.t + 10;
      var left = x(note.x);
      var lines = wrap(plain(note.label), 26, 2);
      var above = at > pad.t + 40;
      var textY = above ? at - 16 - (lines.length - 1) * 13 : at + 22;
      node.appendChild(svg('line', { class: 'note-line', x1: left, x2: left, y1: at, y2: above ? textY + 4 : textY - 12 }));
      node.appendChild(svg('circle', { class: 'note-dot', cx: left, cy: at, r: 4 }));
      var anchor = left > width * 0.75 ? 'end' : left < width * 0.25 ? 'start' : 'middle';
      node.appendChild(svg('text', { class: 'note-text', x: left, y: textY, 'text-anchor': anchor }, lines.map(function (lineText, index) {
        return svg('tspan', { x: left, dy: index ? 13 : 0, text: lineText });
      })));
    });
    var children = [];
    if (data.title && !(options && options.bare)) children.push(el('h3', { text: data.title }));
    children.push(node);
    if (data.series.length > 1) {
      children.push(el('div', { class: 'legend' }, data.series.map(function (entry, index) {
        var key = chartId + '/' + entry.name;
        return el('button', { type: 'button', 'aria-pressed': hiddenSeries[key] ? 'false' : 'true', title: hiddenSeries[key] ? 'Show ' + entry.name : 'Hide ' + entry.name, on: { click: function () {
          if (hiddenSeries[key]) delete hiddenSeries[key]; else if (visible.length > 1) hiddenSeries[key] = true;
          schedule();
        } } }, [el('i', { style: 'background:' + color(index) }), entry.name]);
      })));
    }
    return el('div', { class: 'chart-box', 'data-omnigent-ref': 'series/' + chartId, 'data-omnigent-label': data.title || 'Chart' }, children);
  }

  // The story's headline metric: its value, what it measures, and how it moved.
  function headline(value) {
    var head = value && value.headline;
    if (!head) return null;
    var tone = head.good && head.good !== 'none' && head.direction && head.direction !== 'flat' ? (head.good === head.direction ? 'good' : 'bad') : '';
    var arrow = head.direction === 'up' ? '▲ ' : head.direction === 'down' ? '▼ ' : '';
    return el('div', { class: 'headline-card', 'data-omnigent-ref': 'story/current#headline', 'data-omnigent-label': head.label }, [
      el('div', { class: 'big' }, [head.value, head.unit ? el('small', { text: head.unit }) : null]),
      el('div', {}, [
        el('div', { class: 'label', text: head.label }),
        head.delta ? el('div', { class: 'delta', 'data-tone': tone || null, text: arrow + head.delta }) : null,
        head.context ? el('div', { class: 'context', text: head.context }) : null,
      ]),
    ]);
  }

  // One section: its heading, words, chart and takeaway.
  function section(item) {
    return el('section', { class: 'section', id: 'section-' + item.id, 'data-omnigent-ref': 'story/current#sections/' + item.id, 'data-omnigent-label': item.heading }, [
      el('h2', { text: item.heading }),
      item.body ? el('p', { class: 'body', text: plain(item.body) }) : null,
      item.chart ? chart(item.chart) : null,
      item.takeaway ? el('p', { class: 'takeaway', text: plain(item.takeaway) }) : null,
    ]);
  }

  var kit = {
    el: el, svg: svg, keyed: keyed, ago: ago, hash: hash, wrap: wrap, plain: plain, cite: cite, format: format, scale: scale,
    color: color, tip: tip, chart: chart, headline: headline, section: section, openSession: openSession,
    ref: ref, rerender: function () { schedule(); }, calm: calm, canAct: false, canWrite: false, preview: false,
  };
  function syncKit() { kit.canAct = canAct; kit.canWrite = canWrite; kit.preview = preview; }

  // ---- The plain view: before the first design, and whenever a design fails to draw

  var plainView = {
    name: 'Plain',
    render: function (root, model) {
      var box = root.firstChild;
      if (!box || box.className !== 'story') {
        box = el('div', { class: 'story' }, [el('div', { class: 'story-top' }), el('div', { class: 'story' })]);
        root.replaceChildren(box);
      }
      var top = box.firstChild;
      top.replaceChildren();
      var head = headline(model.story);
      if (head) top.appendChild(head);
      if (model.story.summary) top.appendChild(el('p', { class: 'summary-text', text: plain(model.story.summary) }));
      keyed(box.lastChild, model.sections, function (item) { return item.id; }, section);
    },
  };

  // ---- Rendering

  function schedule(statusOnly) {
    if (!statusOnly) viewDirty = true;
    if (renderQueued) return;
    renderQueued = true;
    (window.requestAnimationFrame || setTimeout)(function () { renderQueued = false; render(); });
  }

  function notice(textValue, kind) {
    var box = $('notice');
    box.hidden = !textValue;
    box.textContent = textValue || '';
    if (kind) box.setAttribute('data-kind', kind); else box.removeAttribute('data-kind');
  }

  function render() {
    var ready = loaded.ideas && loaded.story;
    var value = story();
    var found = ideas();
    var find = runState('find_stories');
    var build = runState('build_story');
    var refresh = runState('refresh_data');
    $('loading').hidden = ready;
    var empty = ready && !value && !found && !(build.open || build.undelivered);
    $('empty').hidden = !empty;
    $('content').hidden = !value;
    $('guide').open = $('guide').open || empty;
    renderHeader(value, refresh);
    renderFindStatus(found, find, empty);
    renderBuilding(build, value);
    renderRun(refresh, build);
    if (!viewDirty) return;
    viewDirty = false;
    renderLook(value);
    renderIdeas(found, value, build);
    drawView();
    renderAbout(value);
  }

  function renderHeader(value, refresh) {
    var parts = [];
    if (value) {
      if (value.example) parts.push('Example');
      if (value.refreshed_at) parts.push('Data refreshed ' + ago(value.refreshed_at));
      else if (value.built_at && !value.example) parts.push('Built ' + ago(value.built_at));
    }
    $('meta').textContent = parts.join(' · ');
    $('meta').hidden = parts.length === 0;
    $('title').textContent = value && value.title ? value.title : client && client.context.app.name ? client.context.app.name : 'Data story';
    $('headline').textContent = (value && value.question) || '';
    $('headline').hidden = !(value && value.question);
    var button = $('refresh');
    button.hidden = !value || !canAct || Boolean(value.example);
    var refreshStarting = refresh.undelivered && sessionStarting();
    button.disabled = (refresh.open && !refresh.undelivered) || refreshStarting;
    button.toggleAttribute('aria-busy', refreshStarting);
    button.textContent = refresh.open ? 'Refreshing…' : refreshStarting ? STARTING_TEXT : refresh.undelivered ? 'Create my gadget session' : 'Refresh data';
  }

  function stages(listId, now) {
    Array.prototype.forEach.call($(listId).children, function (item, index) {
      item.setAttribute('data-now', index === now ? 'true' : 'false');
      item.setAttribute('data-done', now > index ? 'true' : 'false');
    });
  }

  // Finding ideas: under the empty state's button before there are any, on the ideas box after.
  function renderFindStatus(found, find, empty) {
    [$('find-empty'), $('find')].forEach(function (button) {
      var findStarting = find.undelivered && sessionStarting();
      button.disabled = !canAct || (find.open && !find.undelivered) || findStarting;
      button.toggleAttribute('aria-busy', findStarting);
      button.textContent = find.open ? 'Looking…' : findStarting ? STARTING_TEXT : find.undelivered ? 'Create my gadget session' : button.id === 'find' ? 'Suggest others' : 'Find stories in my work';
    });
    // Once a story is built the page is that story: another story is a new gadget.
    var built = Boolean(story() && !story().example);
    $('find').hidden = !canAct || !found || built;
    $('window-label').hidden = !canAct || built;
    var line = find.text ? find.text + (find.working && find.since ? ' ' + elapsed(find.since) + ' · usually 1 to 3 minutes' : '') : '';
    $('empty-run').textContent = empty ? line : '';
    $('progress').hidden = !(empty && find.working);
    if (empty && find.working) stages('progress', 0);
    var box = $('ideas-run');
    box.replaceChildren();
    if (!empty && find.text && (find.open || find.undelivered || (find.action && find.action.status !== 'done'))) {
      if (find.open) box.appendChild(el('span', { class: 'spinner', 'aria-hidden': 'true' }));
      box.appendChild(el('span', { text: line }));
    }
    box.hidden = box.childNodes.length === 0;
  }

  // A story being built: what was picked, the stages, and how long it has run.
  function renderBuilding(build, value) {
    var show = build.open || build.undelivered;
    $('building').hidden = !show;
    if (!show) return;
    $('building-what').textContent = build.label ? '“' + plain(build.label) + '”' : '';
    // The story record is written before the page is designed, so a running build with a story
    // newer than its request is designing.
    var layout = layoutValue();
    var written = Boolean(value) && !value.example && !isNaN(build.requested) && Date.parse(value.built_at) >= build.requested;
    var designed = written && Boolean(layout) && Date.parse(layout.changed_at) >= build.requested;
    stages('build-stages', !build.working ? -1 : designed ? 2 : written ? 1 : 0);
    var line = $('build-run');
    line.replaceChildren();
    if (build.open) line.appendChild(el('span', { class: 'spinner', 'aria-hidden': 'true' }));
    line.appendChild(el('span', { text: written && build.working ? 'Story written. Designing the page; this is the plain view until it is ready.' : build.text }));
    if (build.working && build.since) line.appendChild(el('span', { class: 'elapsed', text: elapsed(build.since) + ' · usually a few minutes' }));
    var session = build.action && build.action.sessionId;
    if (session) line.appendChild(el('button', { type: 'button', class: 'link-button', text: 'Open session', on: { click: function () { openSession(session); } } }));
  }

  // A refresh in progress or one that did not work, or a build that did not work.
  function renderRun(refresh, build) {
    var box = $('run');
    box.replaceChildren();
    var run = refresh.open || refresh.undelivered || (refresh.action && refresh.action.status !== 'done') ? refresh : null;
    if (!run && build.action && !build.open && !build.undelivered && build.action.status !== 'done') run = build;
    if (run && run.text) {
      if (run.open) box.appendChild(el('span', { class: 'spinner', 'aria-hidden': 'true' }));
      box.appendChild(el('span', { text: run.text }));
      if (run.working && run.since) box.appendChild(el('span', { class: 'elapsed', text: elapsed(run.since) }));
    }
    box.hidden = box.childNodes.length === 0;
  }

  function renderLook(value) {
    var box = $('look');
    box.replaceChildren();
    box.removeAttribute('data-kind');
    var layout = layoutValue();
    var restore = runState('restore_look');
    var parts = [];
    if (value && value.example) {
      parts.push(el('span', { text: 'An example. Your gadget session writes your story from your data and designs this page for it.' }));
    } else if (viewFailure) {
      box.setAttribute('data-kind', 'failed');
      parts.push(el('b', { text: 'This look could not be drawn, so this is the plain view.' }));
      parts.push(el('span', { text: viewFailure.text }));
    } else if (value && layout && client && layout.revision_id === client.context.revisionId) {
      var changed = layout.change_note ? ' · ' + clause(layout.change_note) + (layout.changed_at ? ', ' + ago(layout.changed_at) : '') : '';
      parts.push(el('span', {}, ['Look: ', el('b', { text: layout.structure }), changed]));
    }
    if (restore.open) {
      parts.push(el('span', { class: 'spinner', 'aria-hidden': 'true' }));
      parts.push(el('span', { text: 'Going back to the previous look…' }));
    } else if (canAct && value && !value.example && (viewFailure || customView)) {
      parts.push(el('button', { type: 'button', id: 'restore', class: 'quiet', 'data-omnigent-requires': 'action', text: 'Back to the previous look', on: { click: requestRestore } }));
    }
    if (restore.action && !restore.open && restore.action.status === 'failed') parts.push(el('span', { text: 'Could not go back' + (restore.action.summary ? ': ' + restore.action.summary : '') }));
    box.hidden = parts.length === 0;
    parts.forEach(function (part) { box.appendChild(part); });
  }

  function citedLine(list) {
    var cited = (list || []).filter(function (entry) { return entry && entry.id; }).slice(0, 2);
    if (!cited.length) return null;
    var parts = ['From '];
    cited.forEach(function (entry, index) {
      if (index) parts.push(' and ');
      parts.push(cite(entry.title || 'a session', function () { openSession(entry.id); }));
      if (entry.quote) parts.push(el('span', { class: 'quote', text: ': “' + plain(entry.quote) + '”' }));
    });
    return el('div', { class: 'cited' }, parts);
  }

  function ideaCard(idea, picked, disabled) {
    var chips = (idea.sources || []).slice(0, 3).map(function (source) { return el('span', { class: 'chip', title: source.detail || null, text: source.name }); });
    if (idea.effort) chips.unshift(el('span', { class: 'chip', 'data-effort': idea.effort, text: idea.effort === 'quick' ? 'Quick' : idea.effort === 'deep' ? 'Deep dive' : 'A few minutes' }));
    return el('article', { class: 'idea', 'data-picked': picked ? 'true' : null, 'data-omnigent-ref': 'ideas/current#' + idea.id, 'data-omnigent-label': idea.title }, [
      el('h3', { text: idea.title }),
      el('p', { class: 'question', text: plain(idea.question) }),
      idea.why ? el('p', { class: 'why', text: plain(idea.why) }) : null,
      idea.chart_idea ? el('p', { class: 'why', text: 'Chart: ' + plain(idea.chart_idea) }) : null,
      chips.length ? el('div', { class: 'chips' }, chips) : null,
      citedLine(idea.sessions),
      canAct ? el('button', { type: 'button', class: picked ? 'quiet build' : 'primary build', 'data-omnigent-requires': 'action', disabled: disabled, text: picked ? 'Build it again' : 'Build this story', on: { click: function () { buildIdea(idea); } } }) : null,
    ]);
  }

  // The ideas are the page until a story is built; then they go, and another story is a new gadget.
  // Under the preview's example story they stay, folded, so the template shows both steps.
  function renderIdeas(found, value, build) {
    var box = $('ideas');
    var wasHidden = box.hidden;
    box.hidden = !found || Boolean(value && !value.example);
    if (box.hidden) return;
    if (wasHidden) box.open = !value;
    var list = ideaList();
    var busy = (build.open && !build.undelivered) || (build.undelivered && sessionStarting());
    var note = [];
    if (found.example) note.push('Example ideas.');
    else if (found.scanned_at) note.push('From ' + (found.sessions_read || 0) + ' sessions in the last ' + found.window_days + ' days, ' + ago(found.scanned_at) + '.');
    if (found.note) note.push(plain(found.note));
    if (!list.length) note.push('No data work stood out. Try a longer window, or describe the story you want below.');
    $('ideas-note').textContent = note.join(' ');
    var picked = value && value.idea_id;
    keyed($('idea-list'), list, function (idea) { return idea.id; }, function (idea) { return ideaCard(idea, picked === idea.id, busy); });
    $('custom').hidden = !canAct;
    $('custom-go').disabled = busy;
  }

  function renderAbout(value) {
    var box = $('about');
    box.replaceChildren();
    if (!value) { box.hidden = true; return; }
    var parts = [];
    var listed = (value.sources || []).filter(function (source) { return source && source.name; });
    if (listed.length) {
      parts.push(el('div', {}, [el('h3', { text: 'Where the data comes from' }), el('ul', {}, listed.map(function (source) {
        return el('li', {}, [el('b', { text: source.name }), source.detail ? ' · ' + plain(source.detail) : '', source.as_of ? ' · as of ' + source.as_of : '']);
      }))]));
    }
    var cited = (value.sessions || []).filter(function (entry) { return entry && entry.id; });
    if (cited.length) {
      parts.push(el('div', {}, [el('h3', { text: 'From your sessions' }), el('ul', {}, cited.map(function (entry) {
        return el('li', {}, [cite(entry.title || 'A session', function () { openSession(entry.id); }), entry.quote ? el('span', { class: 'quote', text: ': “' + plain(entry.quote) + '”' }) : null]);
      }))]));
    }
    if (value.caveats) parts.push(el('p', { class: 'caveats', text: 'Caveats: ' + plain(value.caveats) }));
    box.hidden = parts.length === 0;
    parts.forEach(function (part) { box.appendChild(part); });
  }

  // ---- The template preview's example, kept in memory

  function seedPreview() {
    previewSeeded = true;
    var now = new Date().toISOString();
    var weeks = ['Sep 1', 'Sep 8', 'Sep 15', 'Sep 22', 'Sep 29', 'Oct 6', 'Oct 13', 'Oct 20'];
    var counts = [410, 432, 520, 610, 640, 655, 690, 702];
    function keep(name, key) {
      return function (doc) { if (name === 'series') seriesDocs[key] = doc; else if (name === 'ideas' && !ideasDoc) ideasDoc = doc; else if (name === 'story' && !storyDoc) storyDoc = doc; schedule(); };
    }
    var ignore = function () { return undefined; };
    collections.series.create({
      title: 'Weekly signups', kind: 'line', x_label: 'Week', y_label: 'Signups',
      series: [{ name: 'Signups', points: weeks.map(function (week, index) { return { x: week, y: counts[index] }; }) }],
      annotations: [{ x: 'Sep 15', label: 'Launch' }],
    }, { id: 'weekly' }).then(keep('series', 'weekly'), ignore);
    collections.series.create({
      title: 'Signups by source, four weeks before and after', kind: 'column',
      series: [{ name: 'Before', points: [{ x: 'Search', y: 720 }, { x: 'Referral', y: 360 }, { x: 'Direct', y: 560 }] },
        { name: 'After', points: [{ x: 'Search', y: 1040 }, { x: 'Referral', y: 840 }, { x: 'Direct', y: 600 }] }],
    }, { id: 'sources' }).then(keep('series', 'sources'), ignore);
    collections.ideas.create({
      scanned_at: now, window_days: 7, sessions_read: 11, example: true,
      ideas: [
        { id: 'signups-after-launch', title: 'Did the launch move signups?', question: 'How did weekly signups change after the September launch?', why: 'Two sessions queried signups by week while checking the launch.', effort: 'quick', chart_idea: 'Weekly signups with the launch marked', sources: [{ name: 'signups', kind: 'table' }], sessions: [{ id: 'example-1', title: 'Launch metrics check', quote: 'signups are up since the 15th' }] },
        { id: 'slow-queries', title: 'Where the slow queries come from', question: 'Which dashboards cause most of the slow queries?', why: 'A latency investigation listed the slowest dashboards.', effort: 'medium', sources: [{ name: 'query history', kind: 'table' }] },
        { id: 'churn-by-plan', title: 'Churn by plan', question: 'Is churn higher on the monthly plan?', effort: 'deep', sources: [{ name: 'subscriptions', kind: 'table' }] },
      ],
    }, { id: 'current' }).then(keep('ideas'), ignore);
    collections.story.create({
      title: 'Did the launch move signups?', question: 'How did weekly signups change after the September launch?', built_at: now, example: true, idea_id: 'signups-after-launch',
      headline: { label: 'Weekly signups', value: '702', delta: '+62% since the week before the launch', direction: 'up', good: 'up', context: 'Week of Oct 20, against the week of Sep 8' },
      summary: 'Signups rose in the launch week and kept climbing for five weeks. Most of the gain came from referrals, which more than doubled.',
      sections: [
        { id: 'trend', heading: 'Signups climbed after the launch', body: 'Weekly signups were flat at about 420 before the launch. They jumped by a fifth in the launch week and kept growing.', chart: 'weekly', takeaway: 'The rise held for five weeks, so it is not a one-week spike.' },
        { id: 'where', heading: 'Referrals drove most of the gain', body: 'Referral signups more than doubled, while direct signups barely moved.', chart: 'sources' },
      ],
      sources: [{ name: 'signups', detail: 'one row per signup', as_of: 'Oct 26' }],
      caveats: 'Example data, made up for this preview.',
    }, { id: 'current' }).then(keep('story'), ignore);
  }

  // ---- Wiring

  function stamp(items) { return (items || []).map(function (doc) { return doc.id + '@' + doc.version; }).join('|'); }
  function current(items) { return (items || []).filter(function (doc) { return doc.id === 'current'; })[0] || null; }
  function onSnapshot(name, snapshot) {
    if (snapshot.status === 'loading') return;
    // A newer revision means the session published a look; Omnigent loads it in a moment.
    if (snapshot.status === 'stale' && snapshot.error && snapshot.error.code === 'REVISION_CHANGED') notice('Loading the new look…');
    else if (snapshot.status === 'stale' && snapshot.error) notice('Could not load the story: ' + message(snapshot.error), 'error');
    var key = stamp(snapshot.items);
    if (loaded[name] && key === seen[name]) return;
    seen[name] = key;
    var items = snapshot.items || [];
    if (name === 'ideas') ideasDoc = current(items) || (preview ? ideasDoc : null);
    else if (name === 'story') storyDoc = current(items) || (preview ? storyDoc : null);
    else if (name === 'layout') layoutDoc = current(items);
    loaded[name] = true;
    if (preview && !previewSeeded && loaded.ideas && loaded.story && !ideasDoc && !storyDoc && snapshot.status === 'live') seedPreview();
    var found = ideas();
    if (found && !$('window').dataset.touched && [3, 7, 14, 30].indexOf(found.window_days) !== -1) $('window').value = String(found.window_days);
    schedule();
  }

  // Charts can fill more than one page (pages end at a byte budget, not only a count), so the
  // series watch follows every page before redrawing.
  function onSeries(items, complete) {
    var key = stamp(items) + (complete ? '' : '+');
    if (loaded.series && key === seen.series) return;
    seen.series = key;
    if (!preview || items.length) seriesDocs = mergeKnown(seriesDocs, items, complete);
    loaded.series = true;
    schedule();
  }

  function automate(name, prompt, rule) {
    client.navigation.newAutomation({ name: name, prompt: prompt, schedule: rule }).catch(function (error) {
      notice('Could not open Automations: ' + message(error), 'error');
    });
  }

  function start() {
    if (started) return;
    started = true;
    $('find-empty').addEventListener('click', findStories);
    $('find').addEventListener('click', findStories);
    $('refresh').addEventListener('click', refreshData);
    $('window').addEventListener('change', function () { $('window').dataset.touched = 'true'; });
    // Not a form: the sandboxed frame may not submit forms, so Chrome would drop the submission.
    // The text stays until the request is sent, so a failed one can be sent again; text typed
    // since then is kept.
    function submitCustom() {
      if ($('custom-go').disabled) return;
      var submitted = $('custom-text').value;
      buildCustom(submitted).then(function (sent) {
        if (sent && $('custom-text').value === submitted) $('custom-text').value = '';
      });
    }
    $('custom-go').addEventListener('click', submitCustom);
    $('custom-text').addEventListener('keydown', function (event) {
      if (event.key === 'Enter' && !event.isComposing) { event.preventDefault(); submitCustom(); }
    });
    $('weekly').addEventListener('click', function () {
      var name = client.context.app.name;
      automate('Refresh ' + name, 'Load the gadget skill and follow it. Without the gadget skill or sys_gadget_* tools, say the host of this session does not support gadgets yet, name what is missing, and stop. Refresh the data of the gadget "' + name + '" (gadget_id ' + client.context.app.id + ') as its guide describes under Refreshing.', 'FREQ=WEEKLY;BYDAY=MO;BYHOUR=8;BYMINUTE=0');
    });
    if (window.ResizeObserver) {
      new ResizeObserver(function () {
        var width = Math.round($('view').clientWidth || 0);
        if (width && Math.abs(width - lastWidth) > 8) { lastWidth = width; schedule(); }
      }).observe($('view'));
    }
    setInterval(function () { schedule(true); }, 1000);
    setInterval(schedule, 60000);

    window.omnigent.connect().then(function (connected) {
      client = connected;
      document.body.setAttribute('data-theme', client.ui.colorScheme);
      preview = client.context.preview === true;
      canWrite = client.hasCapability('data.write') && client.can('write');
      canAct = Boolean(client.agent) && client.can('action') && !preview;
      if (client.agent && typeof client.agent.onStarting === 'function') client.agent.onStarting(function () { schedule(); });
      $('weekly').hidden = !canAct;
      if (!preview && !client.can('action')) notice('You can read this story. Finding, building and refreshing stories run in the owner’s gadget session.');
      ['ideas', 'story', 'series', 'layout', 'render_reports'].forEach(function (name) { collections[name] = client.data.collection(name); });
      if (viewFailure) report('failed', viewFailure.text);
      collections.ideas.watch(function (snapshot) { onSnapshot('ideas', snapshot); }, { intervalMs: 4000, limit: 5 });
      collections.story.watch(function (snapshot) { onSnapshot('story', snapshot); }, { intervalMs: 4000, limit: 5 });
      watchEveryPage(collections.series, onSeries, { intervalMs: 6000, limit: 50 });
      collections.layout.watch(function (snapshot) { onSnapshot('layout', snapshot); }, { intervalMs: 8000, limit: 5 });
      if (canAct && typeof client.agent.watch === 'function') client.agent.watch(onActions, { intervalMs: 3000 });
      schedule();
    }, function (error) {
      $('loading').hidden = true;
      notice('The gadget could not connect to Omnigent: ' + message(error), 'error');
    });
  }

  window.Story = {
    version: 1,
    plainView: plainView,
    view: function (definition) {
      if (!definition || typeof definition.render !== 'function') { failView('Story.view needs an object with a render function'); return; }
      customView = definition === plainView ? null : definition;
      shownView = null;
      schedule();
    },
    start: start,
  };
})();
`;

const STORY_ART = `
<svg class="story-art" viewBox="0 0 320 200" aria-hidden="true">
  <line class="grid" x1="24" y1="40" x2="300" y2="40"/>
  <line class="grid" x1="24" y1="100" x2="300" y2="100"/>
  <line class="grid" x1="24" y1="160" x2="300" y2="160"/>
  <rect class="bar" x="40" y="128" width="28" height="32" rx="4"/>
  <rect class="bar" x="90" y="118" width="28" height="42" rx="4"/>
  <rect class="bar" x="140" y="92" width="28" height="68" rx="4"/>
  <rect class="bar" x="190" y="70" width="28" height="90" rx="4"/>
  <rect class="bar" x="240" y="52" width="28" height="108" rx="4"/>
  <path class="trend" d="M54 124 L104 114 L154 88 L204 66 L254 48"/>
  <circle class="pin" cx="154" cy="88" r="6"/>
</svg>`;

const KIT_BODY = `
<main>
  <div class="top">
    <div>
      <p class="kicker">Data story <span class="badge" title="Built from the owner's own sessions and data. Anyone you share this gadget with sees the story.">Personal</span></p>
      <h1 id="title">Data story</h1>
      <div class="meta" id="meta" hidden></div>
    </div>
    <div class="toolbar">
      <label class="window" id="window-label">Look back
        <select id="window" aria-label="Days of sessions to read">
          <option value="3">3 days</option>
          <option value="7" selected>7 days</option>
          <option value="14">14 days</option>
          <option value="30">30 days</option>
        </select>
      </label>
      <button type="button" id="find" class="quiet" data-omnigent-requires="action" hidden>Suggest others</button>
      <button type="button" id="refresh" class="primary" data-omnigent-requires="action" hidden>Refresh data</button>
    </div>
  </div>
  <p id="headline" class="headline" hidden></p>
  <div id="run" class="status" aria-live="polite" hidden></div>
  <div id="look" class="look" aria-live="polite" hidden></div>
  <p id="notice" class="notice" role="status" hidden></p>
  <p id="loading" class="muted">Loading…</p>

  <section id="empty" class="empty" hidden>
    <div>
      <h2>Find the story in your data work</h2>
      <p>Your gadget session reads the sessions you worked in recently for data work: the tables you queried, the metrics you checked, the analyses you ran. Then it suggests a few stories worth telling.</p>
      <p>Pick one, or describe your own. It gathers the data, writes the story with annotated charts, and designs this page for it, so your story looks like no one else’s.</p>
      <button type="button" id="find-empty" class="primary" data-omnigent-requires="action">Find stories in my work</button>
      <ol id="progress" class="progress" hidden>
        <li>Reading your recent sessions for data work</li>
        <li>Writing story ideas</li>
      </ol>
      <p id="empty-run" class="status"></p>
    </div>
    ${STORY_ART}
  </section>

  <section id="building" class="building" aria-live="polite" hidden>
    <div>
      <h2>Building your story</h2>
      <p id="building-what" class="what"></p>
      <ol id="build-stages" class="progress">
        <li>Gathering the data</li>
        <li>Writing the story</li>
        <li>Designing the page</li>
      </ol>
      <div id="build-run" class="status"></div>
    </div>
    ${STORY_ART}
  </section>

  <div id="content" hidden>
    <div id="view" class="view"></div>
    <section id="about" class="about" hidden></section>
  </div>

  <section>
    <details id="ideas" class="ideas-box" hidden>
      <summary id="ideas-title">Stories you could tell</summary>
      <p id="ideas-note" class="ideas-note"></p>
      <div id="ideas-run" class="status" aria-live="polite" hidden></div>
      <div id="idea-list" class="ideas"></div>
      <div id="custom" class="custom" hidden>
        <label for="custom-text">Something else</label>
        <input type="text" id="custom-text" maxlength="500" placeholder="For example: weekly active users by plan since September">
        <button type="button" id="custom-go" class="quiet" data-omnigent-requires="action">Build it</button>
      </div>
    </details>
  </section>

  <section>
    <details id="guide" class="guide">
      <summary>How this works, and how to make it yours</summary>
      <div class="guide-grid">
        <div>
          <h3>How this works</h3>
          <div class="layers">
            <div class="layer"><svg viewBox="0 0 26 26" aria-hidden="true"><circle cx="13" cy="13" r="9" fill="var(--accent-soft)" stroke="var(--accent)" stroke-width="2"/></svg><div><b>Story ideas</b> come from your gadget session. It reads your recent sessions (their titles, journals and last messages, not whole transcripts) for data work and suggests stories, each citing the sessions it came from.</div></div>
            <div class="layer"><svg viewBox="0 0 26 26" aria-hidden="true"><path d="M4 20 L10 13 L15 16 L22 6" fill="none" stroke="var(--accent)" stroke-width="2.2" stroke-linecap="round"/></svg><div><b>The story</b> is built from real data. Your gadget session gathers it with its own tools, writes the numbers and charts into this gadget’s records and says where they come from. <b>Refresh data</b> gathers them again.</div></div>
            <div class="layer"><svg viewBox="0 0 26 26" aria-hidden="true"><rect x="5" y="5" width="16" height="16" rx="3" fill="none" stroke="var(--accent)" stroke-width="2"/><path d="M5 12 H21 M12 12 V21" stroke="var(--accent)" stroke-width="2"/></svg><div><b>The page</b> is designed for your story. Your gadget session designs it after writing the story and changes it only when the story needs it, saying what changed. <b>Back to the previous look</b> undoes a change.</div></div>
          </div>
          <h3>Personal</h3>
          <ul><li>The story comes from your own sessions and data. Anyone you share this gadget with sees it, so share it only when that is fine.</li></ul>
        </div>
        <div>
          <h3>Make it yours</h3>
          <ul>
            <li>Ask your gadget session, in its chat: <span class="ask">“Add a breakdown by region.”</span></li>
            <li><span class="ask">“Explain the biggest change this week.”</span></li>
            <li><span class="ask">“Make it a one-screen summary with a big headline number.”</span></li>
            <li>Keep the numbers fresh on their own: <button type="button" id="weekly" class="link-button" data-omnigent-requires="action" hidden>refresh the data every Monday</button>.</li>
          </ul>
        </div>
      </div>
    </details>
  </section>
  <footer>Select any text, or Alt+click a chart or section, to reply about it or ask in a side chat.</footer>
</main>
`;

// How a design plugs into the kit. The session reads this in the revision it replaces.
const VIEW_API = `<!--
The view region. A design replaces everything from story:view:start to story:view:end with one
style element and one script element, and keeps the rest of this file exactly as it is.

The script calls Story.view({ name: 'Before and after', render: function (root, model, kit) { ... } }).
render runs after every change to the records and when the page resizes. Update root in place
with kit.keyed and stable IDs so the page animates instead of redrawing. If the script or render
throws, the kit switches the design's styles off, shows its plain view and reports the error in
render_reports.

model: story (the story record: title, question, headline, summary, sections, sources,
sessions, caveats, built_at, refreshed_at), sections (the sections that have an id and a
heading), series (an object of chart data keyed by chart ID, not an array: each
{title, kind, unit, x_label, y_label, series: [{name, points: [{x, y}]}], annotations}),
layout (the layout record or null), width (root's width in pixels), preview.

kit: el(tag, props, children) and svg(tag, props, children) make elements (props: class, text,
on: { click: fn }, and any attribute); keyed(parent, items, keyOf, build) keeps one element per
key and animates moves; chart(chartId, { width, height, bare }) draws a series record as an
annotated chart with a legend and each value on hover; headline(story) is the headline metric
card; section(section) is a section with its words, chart and takeaway; format(number, unit);
scale(d0, d1, r0, r1) is a linear scale; color(index) is a series color; tip(node, text) adds a
hover and focus tooltip; openSession(id); ref(path) for data-omnigent-ref; ago(iso);
wrap(text, maxChars, maxLines) splits text for SVG; plain(text) drops Markdown marks; calm is
true when the viewer prefers reduced motion; canAct, canWrite.

Colors: var(name) with name one of text, muted, line, faint, surface, bg, accent, accent-soft,
s1 to s6 (series), up, down, and done, waiting, blocked with their -soft forms; the kit switches
them for dark mode. The story ideas, build and refresh progress, Refresh data, the look line,
sources and caveats, and the guide are the kit's: do not draw them again.
-->`;

const PLAIN_VIEW = `${DATA_STORY_VIEW_START}
<style id="story-view-style"></style>
<script id="story-view">Story.view(Story.plainView);</script>
${DATA_STORY_VIEW_END}`;

export const DATA_STORY_HTML = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="omnigent-template" content="data-story">
<title>Data story</title>
<style id="story-kit-style">${KIT_STYLE}</style>
</head>
<body>
${KIT_BODY}
<script id="story-kit">${KIT_SCRIPT}</script>
${VIEW_API}
${PLAIN_VIEW}
<script id="story-start">Story.start();</script>
</body>
</html>
`;

export const DATA_STORY_GUIDE = [
  "# Data story",
  "",
  "A story about the viewer's own data work: you find candidate stories in their recent sessions, they pick one, and you build it from real data and design its page. It is personal: it draws on the owner's sessions and data.",
  "",
  "The records are fixed so finding, building, refreshing and their progress always work. The page is yours to design for this story, so every story looks different.",
  "",
  "## Collections",
  "",
  "- `ideas`: one record, ID `current`, the story ideas from the latest search. Each search replaces it in one write (`sys_gadget_records` `get`, then `replace` with its `version`; `create` with `id` `current` when it does not exist).",
  "- `story`: one record, ID `current`, the built story's words and structure. Chart data is not in it.",
  "- `series`: one record per chart, with the chart's ID (a lowercase slug) as the record ID, holding its data. A record holds at most 32 KiB, so keep each chart to the points it needs (at most 400; aggregate before writing).",
  "- `layout`: one record, ID `current`, describing the page you designed: `{structure, why, revision_id, previous_revision_id, changed_at, change_note, history}`. `history` lists earlier looks newest first (`{revision_id, at, note, structure}`, at most 12).",
  "- `render_reports`: the page writes one per revision ID, `{state: ok|failed, error, view, at}`. You only read them.",
  "",
  "## Finding stories",
  "",
  "Find stories in my work, and Suggest others before a story is built, request `find_stories` with `window_days`. Once a story is built the page no longer shows ideas: another story is a new gadget from the Data story template, so do not offer more ideas after a build.",
  "",
  "1. List sessions with `sys_session_list` (visibility `mine`); keep those with `last_activity_at` (Unix seconds) within `window_days`, at most 25, newest first. Skip this gadget session and sessions you started for this gadget.",
  "2. Read only a little of each: its title, its journal with `sys_journal_read` when that tool is available, else `sys_session_get_history` limited to its last few items. Never read whole transcripts, and never ask a session to start a journal.",
  "3. Look for data work: tables or files queried, metrics checked, analyses run, results or charts discussed, questions left open.",
  "4. Write 3 to 6 ideas, each a story worth telling from data the viewer already touched: `{id, title, question, why, chart_idea, effort: quick|medium|deep, sources: [{name, kind, detail}], sessions: [{id, title, quote}]}`. `question` is the one question the story answers; cite each idea's sessions with a short quote. On Suggest others, replace the ideas with different ones.",
  "   Name only sources you saw: a file, table or path an idea cites must be one the session shows or one you checked exists (list the directory), never a guessed name.",
  "5. Replace the `ideas` record with `scanned_at`, `window_days`, `sessions_read` and `ideas`; add `note` when the window had little data work. Report the action `done` with a one-line summary.",
  "",
  "## Building a story",
  "",
  "Build this story requests `build_story` with `idea_id` (and `title`); Something else sends `request`, the viewer's own words, instead. A build takes several minutes; the page shows its stages meanwhile.",
  "",
  "1. Gather the data with your own tools: run the queries the sessions ran, or read the files and tables they used. The page's CSP blocks network requests, so everything it shows comes from records. Every number must come from data you read or a result a session shows; never invent or extrapolate data. When you cannot reach the data, say so in the session and in `caveats`, and stop rather than guess.",
  '2. Write each chart\'s `series` record: `{title, kind: line|area|bar|column, unit, x_label, y_label, series: [{name, points: [{x, y}]}], annotations: [{x, series, label}]}`. `x` is a string (a date like "2026-11-02" or a category), `y` a number. Annotate the moments that matter (a launch, a spike, a change).',
  "3. Replace the `story` record: `idea_id` (when built from an idea), `title`, `question`, `built_at` (now, ISO 8601 UTC), `headline: {label, value, unit, delta, direction: up|down|flat, good: up|down|none, context}`, `summary` (two or three plain sentences: the answer), `sections` (2 to 6: `{id, heading, body, chart, takeaway}`, where `chart` is a series record ID), `sources: [{name, kind, detail, as_of}]` (how to get the data again, such as the query), `sessions` it came from, and `caveats`. Delete `series` records no section uses.",
  "4. Design the page (Designing the page), publish it, and write the `layout` record.",
  '5. On the first story, while the gadget is still named "Data story", rename it to the story\'s title with `sys_gadget_rename`; keep any other name.',
  "6. Report the action `done` with a one-line summary.",
  "",
  "## Designing the page",
  "",
  "Tell the story, not a dashboard: lead with the answer (the headline and summary), then walk through the evidence, one point per section, each with its chart and takeaway. Choose the form the story has: a before and after, a trend with turning points, a ranking, a breakdown, a funnel. These are examples, not a menu.",
  "",
  "- Draw from `model` only; never write data into the HTML, so Refresh data updates the page without a new revision.",
  "- Make it readable in the docked side panel (about 400 px wide) and full screen, use the kit's colors so light and dark both work, respect `kit.calm`, and use inline SVG and CSS only: the frame's CSP blocks external requests. Keep the HTML well under 1 MiB.",
  "- Give it at least one visual that carries the point beyond the kit's chart: an annotated before and after, a highlighted turning point, a reveal as the reader scrolls, an interactive breakdown. `kit.chart`, `kit.headline` and `kit.section` are there to reuse.",
  "- The story ideas, build and refresh progress, Refresh data, the look line, sources and the guide are the kit's: do not draw them again.",
  "- `kit.section` already draws its heading, words, chart and takeaway: place it as it is, not inside another card with a second title. Each chart gets one title.",
  '- Read your text back before publishing: a space between words and numbers ("up 45%", not "up45%"), no repeated titles, and units on every number.',
  "",
  "To publish a design:",
  "",
  "1. Write one `<style>` and one `<script>` calling `Story.view({name, render})`. The comment above the view region documents `model` and `kit`; read it once with `sys_gadget_revision_get`.",
  '2. `sys_gadget_revision_create` with `base_revision_id` the published revision and one edit, `{start: "<!-- story:view:start -->", end: "<!-- story:view:end -->", new: <your style and script>}`. Do not send `html`: the edit changes only the view region, and the rest is carried over unchanged.',
  "3. If it is refused, the error names the edit and why; fix it and send it again. Then `sys_gadget_publish` it with `if_version` from `sys_gadget_get`.",
  "4. Write the `layout` record (`structure`, `why`, `revision_id`, `previous_revision_id`, `changed_at`, `change_note`, `history`).",
  "5. Check that it drew: read `render_reports` for the new revision a few times over about a minute. On `failed`, restore at once (Restoring) and fix the design before you try again.",
  "",
  "## Refreshing",
  "",
  "Refresh data requests `refresh_data`. Run the story's `sources` again, replace the `series` records and the numbers in the `story` record (the headline, and words that quote numbers), set `refreshed_at`, and keep section IDs so the page updates in place. Do not change the design. Say what moved in the action's summary.",
  "",
  "## Restoring",
  "",
  'Back to the previous look requests `restore_look`, with `revision_id` when the page knows it. Publish that revision (or `layout.previous_revision_id`) with `if_version`, then update `layout`: `revision_id` the restored one, `previous_revision_id` the one you left, `change_note` "Back to the previous look", and the left look added to `history`. Do the same on your own when `render_reports` says the published design failed.',
  "",
  "## Customizing",
  "",
  'People ask the gadget session to change the story ("add a breakdown by region"), its look ("make it one screen") or where ideas come from ("look at the last 30 days"). Change records and the page as above; keep the collections, since a published collection\'s schema cannot change.',
].join("\n");

export const DATA_STORY_REVISION: CreateGadgetRevisionRequest = {
  html: DATA_STORY_HTML,
  sdk_version: 1,
  capabilities: ["data.read", "data.write"],
  collections: {
    [DATA_STORY_IDEAS_COLLECTION]: DATA_STORY_IDEAS_SCHEMA,
    [DATA_STORY_COLLECTION]: DATA_STORY_SCHEMA,
    [DATA_STORY_SERIES_COLLECTION]: DATA_STORY_SERIES_SCHEMA,
    [DATA_STORY_LAYOUT_COLLECTION]: GADGET_LAYOUT_SCHEMA,
    [DATA_STORY_REPORTS_COLLECTION]: GADGET_RENDER_REPORT_SCHEMA,
  },
  guide: DATA_STORY_GUIDE,
  constraints: [
    {
      name: "one_ideas",
      kind: "max_records",
      collection: DATA_STORY_IDEAS_COLLECTION,
      max: 1,
      message: "The ideas are one record with ID current: replace it instead of creating another.",
    },
    {
      name: "one_story",
      kind: "max_records",
      collection: DATA_STORY_COLLECTION,
      max: 1,
      message: "The story is one record with ID current: replace it instead of creating another.",
    },
    {
      name: "some_charts",
      kind: "max_records",
      collection: DATA_STORY_SERIES_COLLECTION,
      max: 24,
      message: "A story has at most 24 charts: delete series records no section uses.",
    },
    {
      name: "one_layout",
      kind: "max_records",
      collection: DATA_STORY_LAYOUT_COLLECTION,
      max: 1,
      message: "The layout is one record with ID current: replace it instead of creating another.",
    },
  ],
  actions: [
    {
      name: "find_stories",
      description:
        "Suggest 3 to 6 data stories from the viewer's recent sessions, as the guide's Finding stories section " +
        "says: their sessions active in the last params.window_days days (default 7, at most 25 sessions), " +
        "titles, journals when available and last few messages only, looking for data work. Replace the ideas " +
        "record with ID current in one write, each idea citing its sessions.",
      params: {
        type: "object",
        additionalProperties: false,
        properties: { window_days: { type: "integer", minimum: 1, maximum: 60 } },
      },
    },
    {
      name: "build_story",
      description:
        "Build the story params.idea_id from the ideas record, or the viewer's own params.request, as the guide's " +
        "Building a story section says: gather real data with your own tools, write a series record per chart " +
        "and the story record with ID current, then design and publish its page.",
      params: {
        type: "object",
        additionalProperties: false,
        properties: {
          idea_id: { type: "string", minLength: 1, maxLength: 64 },
          title: { type: "string", maxLength: 90 },
          request: { type: "string", minLength: 1, maxLength: 500 },
        },
      },
    },
    {
      name: "refresh_data",
      description:
        "Gather the story's data again from its sources and update the series records and the story's numbers, " +
        "as the guide's Refreshing section says, without changing the design.",
      params: { type: "object", additionalProperties: false, properties: {} },
    },
    {
      name: "restore_look",
      description:
        "Publish the previous design again, as the guide's Restoring section says: params.revision_id, else the " +
        "layout record's previous_revision_id, then update the layout record.",
      params: {
        type: "object",
        additionalProperties: false,
        properties: { revision_id: { type: "string", minLength: 32, maxLength: 32 } },
      },
    },
  ],
};
