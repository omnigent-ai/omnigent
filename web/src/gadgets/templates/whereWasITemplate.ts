// Where was I? template. Keep the kit outside the editable view region (VIEW_START to VIEW_END),
// which is the only part a session's design replaces.
//
// The scripts are plain ES2018 inside template literals: keep them free of backticks, `${`, and
// backslashes.

import type { CreateGadgetRevisionRequest } from "../api/gadgetApi";
import {
  GADGET_KIT_HELPERS,
  GADGET_KIT_STYLE,
  GADGET_LAYOUT_SCHEMA,
  GADGET_RENDER_REPORT_SCHEMA,
} from "./templateKit";

export const WHERE_WAS_I_COLLECTION = "radar";
// The one record in `radar` and in `layout`; each scan or design replaces it.
export const WHERE_WAS_I_RECORD_ID = "current";
export const WHERE_WAS_I_LAYOUT_COLLECTION = "layout";
export const WHERE_WAS_I_REPORTS_COLLECTION = "render_reports";
// What the session learned about how the viewer works, and how its next steps did.
export const WHERE_WAS_I_PATTERNS_COLLECTION = "patterns";
// Next up shows a step only when the session's estimate that the viewer clicks it reaches this,
// or its pattern's own calibrated bar.
export const WHERE_WAS_I_DEFAULT_BAR = 0.7;
export const WHERE_WAS_I_VIEW_START = "<!-- radar:view:start -->";
export const WHERE_WAS_I_VIEW_END = "<!-- radar:view:end -->";

const AREA_STATES = ["active", "blocked", "waiting", "done"];
const STEP_STATUSES = ["started", "done", "needs_input", "failed"];
// Why a step is worth the viewer's attention now: one they usually skip, one past its usual
// time, or one simply due.
const NUDGES = ["forgotten", "overdue", "due"];
const DISMISS_REASONS = ["not_relevant", "already_done"];
const OUTCOMES = ["open", "clicked", "dismissed", "ignored"];

const id = (max: number) => ({ type: "string", minLength: 1, maxLength: max });
const text = (max: number) => ({ type: "string", maxLength: max });
const time = { type: "string", minLength: 10, maxLength: 40 };
const share = { type: "number", minimum: 0, maximum: 1 };
const count = { type: "integer", minimum: 0, maximum: 10000 };

export const WHERE_WAS_I_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["scanned_at", "window_days", "areas"],
  properties: {
    scanned_at: time,
    window_days: { type: "integer", minimum: 1, maximum: 30 },
    sessions_read: { type: "integer", minimum: 0, maximum: 500 },
    headline: text(240),
    example: { type: "boolean" },
    areas: {
      type: "array",
      maxItems: 12,
      items: {
        type: "object",
        additionalProperties: false,
        required: ["id", "title", "state"],
        properties: {
          id: id(64),
          title: id(80),
          summary: text(500),
          state: { type: "string", enum: AREA_STATES },
          kind: text(30),
          related: { type: "array", maxItems: 6, items: id(64) },
          items: {
            type: "array",
            maxItems: 8,
            items: {
              type: "object",
              additionalProperties: false,
              required: ["id", "title", "state"],
              properties: {
                id: id(64),
                title: id(120),
                summary: text(300),
                state: { type: "string", enum: AREA_STATES },
                session_ids: { type: "array", maxItems: 4, items: id(128) },
              },
            },
          },
          next_steps: {
            type: "array",
            maxItems: 5,
            items: {
              type: "object",
              additionalProperties: false,
              required: ["id", "label", "kind"],
              properties: {
                id: id(64),
                label: id(90),
                why: text(300),
                why_now: text(300),
                kind: { type: "string", enum: ["act", "monitor"] },
                confidence: share,
                pattern: text(64),
                pattern_step: text(64),
                nudge: { type: "string", enum: NUDGES },
                session_id: text(128),
                evidence: {
                  type: "object",
                  additionalProperties: false,
                  properties: { session_id: text(128), quote: text(240) },
                },
              },
            },
          },
        },
      },
    },
    sessions: {
      type: "array",
      maxItems: 30,
      items: {
        type: "object",
        additionalProperties: false,
        required: ["id", "title"],
        properties: {
          id: id(128),
          title: text(160),
          area: text(64),
          why: text(240),
          status: text(20),
          outcome: text(500),
          steps: {
            type: "array",
            maxItems: 10,
            items: {
              type: "object",
              additionalProperties: false,
              required: ["step", "status"],
              properties: { step: id(64), status: { type: "string", enum: STEP_STATUSES } },
            },
          },
        },
      },
    },
  },
} satisfies Record<string, unknown>;

export const WHERE_WAS_I_LAYOUT_SCHEMA = GADGET_LAYOUT_SCHEMA;
export const WHERE_WAS_I_REPORT_SCHEMA = GADGET_RENDER_REPORT_SCHEMA;

export const WHERE_WAS_I_STEP_RUN_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["label", "state"],
  properties: {
    label: id(90),
    area: text(64),
    state: { type: "string", enum: ["running", "done", "failed"] },
    outcome: text(600),
    session_id: text(128),
    updated: text(40),
  },
} satisfies Record<string, unknown>;

export const WHERE_WAS_I_DISMISSED_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["at"],
  properties: {
    at: time,
    label: text(90),
    pattern: text(64),
    reason: { type: "string", enum: DISMISS_REASONS },
  },
} satisfies Record<string, unknown>;

export const WHERE_WAS_I_PATTERNS_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["updated_at", "patterns"],
  properties: {
    updated_at: time,
    history_days: { type: "integer", minimum: 1, maximum: 30 },
    sessions_seen: { type: "integer", minimum: 0, maximum: 2000 },
    patterns: {
      type: "array",
      maxItems: 10,
      items: {
        type: "object",
        additionalProperties: false,
        required: ["id", "name", "steps"],
        properties: {
          id: id(64),
          name: id(80),
          summary: text(240),
          steps: {
            type: "array",
            minItems: 1,
            maxItems: 8,
            items: {
              type: "object",
              additionalProperties: false,
              required: ["id", "label"],
              properties: {
                id: id(64),
                label: id(90),
                after_days: { type: "number", minimum: 0, maximum: 90 },
                done_rate: share,
                forgotten: { type: "boolean" },
              },
            },
          },
          evidence: {
            type: "array",
            maxItems: 6,
            items: {
              type: "object",
              additionalProperties: false,
              required: ["session_id"],
              properties: { session_id: id(128), quote: text(200) },
            },
          },
          seen: { type: "integer", minimum: 0, maximum: 1000 },
          bar: { type: "number", minimum: 0.5, maximum: 0.95 },
          hits: {
            type: "object",
            additionalProperties: false,
            properties: { shown: count, clicked: count, dismissed: count, ignored: count },
          },
        },
      },
    },
    ledger: {
      type: "array",
      maxItems: 40,
      items: {
        type: "object",
        additionalProperties: false,
        required: ["step", "shown_at", "outcome"],
        properties: {
          step: id(64),
          pattern: text(64),
          label: text(90),
          shown_at: time,
          outcome: { type: "string", enum: OUTCOMES },
          reason: { type: "string", enum: DISMISS_REASONS },
          resolved_at: time,
        },
      },
    },
  },
} satisfies Record<string, unknown>;

const KIT_STYLE = `
${GADGET_KIT_STYLE}
/* Next up */
.next-list { display: flex; flex-wrap: wrap; gap: 8px; }
.next-chip { display: inline-flex; align-items: center; gap: 8px; max-width: 100%; text-align: left; border: 1px solid var(--line); background: var(--surface); border-radius: 999px; padding: 6px 12px 6px 8px; cursor: pointer; }
.next-chip:hover:not(:disabled) { border-color: var(--accent); }
.next-chip:disabled { cursor: default; opacity: 0.7; }
.next-chip .where { color: var(--muted); font-size: 12px; }
.next-chip[data-run='failed'] { border-color: var(--blocked); }
.next-chip[data-run='failed'] .where, .next-chip[data-run='failed'] .mark { color: var(--blocked); }
.next-run { margin: 0 0 0 4px; font-size: 12px; color: var(--blocked); }
.next-items { display: grid; gap: 10px; }
.next-item { position: relative; display: grid; gap: 4px; padding: 10px 34px 10px 12px; border: 1px solid var(--line); border-left: 3px solid var(--accent); border-radius: 12px; background: var(--surface); }
.next-item[data-nudge='forgotten'] { border-left-color: var(--waiting); }
.next-item[data-nudge='overdue'] { border-left-color: var(--blocked); }
.next-item .next-chip { justify-self: start; }
.next-why { margin: 0 0 0 4px; font-size: 13px; color: var(--text); }
.next-why .nudge { font-weight: 700; color: var(--accent); }
.next-item[data-nudge='forgotten'] .next-why .nudge { color: var(--waiting); }
.next-item[data-nudge='overdue'] .next-why .nudge { color: var(--blocked); }
.next-why .quote, .next-from { color: var(--muted); }
.next-from { margin: 0 0 0 4px; font-size: 12px; }
.next-empty { margin: 0; color: var(--muted); }
.reasons { display: inline-flex; gap: 6px; margin-left: 8px; }
.reasons button { border: 1px solid var(--line); background: var(--surface); border-radius: 999px; padding: 1px 10px; font-size: 12px; cursor: pointer; color: var(--text); }
.reasons button:hover { border-color: var(--accent); }
.hit { margin: 18px 0 0; font-size: 12px; color: var(--muted); }
.why-hidden { margin: 8px 0 0; font-size: 12px; color: var(--muted); }
.why-hidden .reasons { margin-left: 4px; }

/* What it learned */
.learned { margin-top: 18px; }
.learned summary { cursor: pointer; color: var(--muted); font-size: 13px; }
.learned-list { list-style: none; margin: 10px 0 0; padding: 0; display: grid; gap: 10px; }
.learned-list li { padding: 10px 12px; border-radius: 10px; border: 1px solid var(--line); background: var(--surface); }
.learned-steps { display: flex; flex-wrap: wrap; align-items: center; gap: 4px; margin-top: 6px; font-size: 12px; }
.learned-steps span { padding: 1px 8px; border-radius: 999px; background: var(--faint); }
.learned-steps span[data-forgotten='true'] { background: var(--waiting-soft); color: var(--waiting); font-weight: 600; }
.learned-steps i { color: var(--muted); font-style: normal; }

/* Area cards */
.areas { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 14px; }
.area { background: var(--surface); border: 1px solid var(--line); border-left: 4px solid var(--state, var(--active)); border-radius: 12px; padding: 14px 14px 12px; transition: box-shadow 300ms ease, border-color 300ms ease; }
.area[data-focused='true'] { box-shadow: 0 0 0 2px var(--accent); }
.area[data-state='active'] { --state: var(--active); --state-soft: var(--active-soft); }
.area[data-state='blocked'] { --state: var(--blocked); --state-soft: var(--blocked-soft); }
.area[data-state='waiting'] { --state: var(--waiting); --state-soft: var(--waiting-soft); }
.area[data-state='done'] { --state: var(--done); --state-soft: var(--done-soft); }
.area-head { display: flex; align-items: center; gap: 8px; }
.area-head h3 { flex: 1; min-width: 0; }
.pill { font-size: 11px; font-weight: 600; padding: 1px 8px; border-radius: 999px; background: var(--state-soft, var(--faint)); color: var(--state, var(--muted)); white-space: nowrap; }
.area .summary { margin: 6px 0 0; color: var(--muted); }
.block-label { margin: 12px 0 6px; font-size: 11px; font-weight: 700; letter-spacing: 0.05em; text-transform: uppercase; color: var(--muted); }
.steps { display: grid; gap: 8px; }
.step { position: relative; border: 1px solid var(--line); border-radius: 10px; padding: 8px 30px 8px 10px; background: var(--bg); }
.step[data-run='running'], .step[data-run='sending'] { border-color: var(--accent); }
.step[data-run='done'] { border-color: var(--done); }
.step[data-run='failed'] { border-color: var(--blocked); }
.step-main { display: flex; align-items: flex-start; gap: 8px; width: 100%; text-align: left; border: none; background: none; padding: 0; cursor: pointer; font-weight: 600; }
.step-main:disabled { cursor: default; }
.step-main:hover:not(:disabled) .step-label { color: var(--accent); }
/* Doubled class names keep the kit's pieces as they are inside a design's broader rules. */
.kind-icon.kind-icon { flex: none; width: 18px; height: 18px; margin-top: 1px; display: inline-block; }
.kind-icon.kind-icon circle, .kind-icon.kind-icon path { fill: none; stroke: var(--accent); stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }
.kind-icon .kind-fill.kind-fill { fill: var(--accent); stroke: none; }
.step-why { margin: 3px 0 0 26px; font-size: 12.5px; color: var(--muted); }
.step-run { display: flex; align-items: center; gap: 6px; margin: 6px 0 0 26px; font-size: 12.5px; }
.step[data-run='done'] .step-run { color: var(--done); }
.step[data-run='failed'] .step-run { color: var(--blocked); }
.step-x { position: absolute; top: 6px; right: 6px; width: 22px; height: 22px; border: none; background: none; border-radius: 6px; color: var(--muted); cursor: pointer; font-size: 16px; line-height: 1; }
.step-x:hover:not(:disabled) { background: var(--faint); color: var(--text); }
.items { list-style: none; margin: 0; padding: 0; display: grid; gap: 4px; }
.items li { display: grid; grid-template-columns: 10px minmax(0, 1fr); gap: 8px; align-items: baseline; }
.items .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.items .item-sum { color: var(--muted); font-size: 12.5px; }
.watch { display: grid; gap: 6px; }
.session { display: grid; gap: 2px; padding: 6px 8px; border-radius: 8px; background: var(--bg); }
.session-link { display: inline-flex; align-items: center; gap: 8px; border: none; background: none; padding: 0; cursor: pointer; text-align: left; font-weight: 600; color: var(--accent); }
.session-link:hover { text-decoration: underline; }
.session-meta { font-size: 12.5px; color: var(--muted); }
.live-dot { flex: none; width: 8px; height: 8px; border-radius: 50%; background: var(--muted); }
.live-dot[data-live='running'] { background: var(--active); animation: blink 1.4s ease-in-out infinite; }
.live-dot[data-live='needs_input'] { background: var(--waiting); animation: blink 1s ease-in-out infinite; }
.live-dot[data-live='waiting'] { background: var(--waiting); }
.live-dot[data-live='failed'] { background: var(--blocked); }
.live-dot[data-live='done'], .live-dot[data-live='idle'] { background: var(--done); }
.steps-bar { display: flex; gap: 3px; margin-top: 3px; }
.steps-bar i { width: 14px; height: 5px; border-radius: 3px; background: var(--faint); }
.steps-bar i[data-s='done'] { background: var(--done); }
.steps-bar i[data-s='started'] { background: var(--active); }
.steps-bar i[data-s='needs_input'] { background: var(--waiting); }
.steps-bar i[data-s='failed'] { background: var(--blocked); }
.dismissed-note { margin-top: 8px; font-size: 12px; color: var(--muted); }

/* Done recently and the guide */
.done-list { list-style: none; margin: 0; padding: 0; display: grid; gap: 6px; }
.done-list li { display: grid; grid-template-columns: 18px minmax(0, 1fr); gap: 8px; padding: 8px 10px; border-radius: 10px; background: var(--surface); border: 1px solid var(--line); }
.done-list .mark { color: var(--done); font-weight: 700; }
.done-list li[data-state='failed'] .mark { color: var(--blocked); }
.done-list li[data-fresh='true'] { border-color: var(--done); background: var(--done-soft); animation: done-in 700ms ease-out; }
.done-list li[data-fresh='true'][data-state='failed'] { border-color: var(--blocked); background: var(--blocked-soft); }
.done-list li[data-fresh='true'] .mark, .next-chip.finished .mark { animation: pop 600ms cubic-bezier(0.2, 0.8, 0.2, 1.4); }
.next-chip.finished { border-color: var(--done); background: var(--done-soft); cursor: default; opacity: 1; animation: done-in 500ms ease-out; }
.next-chip.finished[data-state='failed'] { border-color: var(--blocked); background: var(--blocked-soft); }
.next-chip .mark { color: var(--done); font-weight: 700; width: 16px; text-align: center; }
.next-chip.finished[data-state='failed'] .mark { color: var(--blocked); }
@keyframes done-in { from { transform: translateY(4px); opacity: 0.4; } to { transform: none; opacity: 1; } }
@keyframes pop { 0% { transform: scale(0.4); } 60% { transform: scale(1.3); } 100% { transform: scale(1); } }
@media (max-width: 640px) {
  .areas { grid-template-columns: minmax(0, 1fr); }
}


/* The empty state's radar */
.radar-art { width: 100%; height: auto; display: block; }
.radar-art .ring { fill: none; stroke: var(--line); stroke-width: 1.2; }
.radar-art .axis { stroke: var(--line); stroke-width: 1; stroke-dasharray: 3 5; }
.radar-art .sweep { fill: var(--accent); opacity: 0.16; transform-box: view-box; transform-origin: 160px 110px; animation: sweep 4.5s linear infinite; }
.radar-art .blip { opacity: 0.2; animation: blip 4.5s ease-out infinite; }
.radar-art .b1 { fill: var(--active); animation-delay: -3.9s; }
.radar-art .b2 { fill: var(--waiting); animation-delay: -2.6s; }
.radar-art .b3 { fill: var(--blocked); animation-delay: -1.4s; }
.radar-art .b4 { fill: var(--done); animation-delay: -0.4s; }
@keyframes sweep { to { transform: rotate(360deg); } }
@keyframes blip { 0% { opacity: 1; } 70% { opacity: 0.25; } 100% { opacity: 0.2; } }
`;

const KIT_SCRIPT = `
(function () {
  'use strict';
${GADGET_KIT_HELPERS}
  var OPEN = ['pending', 'queued', 'running', 'needs_input'];
  var PENDING_MS = 10 * 60 * 1000;
  var STATE_LABELS = { active: 'Active', blocked: 'Blocked', waiting: 'Waiting', done: 'Done' };
  var STATE_ORDER = { blocked: 0, active: 1, waiting: 2, done: 3 };
  var SCAN_TEXT = {
    pending: 'Create your gadget session, and it scans.',
    queued: 'Waiting for your gadget session to pick it up…',
    running: 'Reading your recent sessions…',
    needs_input: 'Your gadget session is waiting for you.',
    done: 'Scan done',
    failed: 'Scan failed',
    cancelled: 'Scan cancelled',
    finished_unreported: 'Your gadget session stopped before it reported the scan. Click Rescan to ask again.',
    interrupted: 'The scan stopped before it finished. Click Rescan to try again.',
  };
  var STEP_TEXT = {
    pending: 'Create your gadget session, and it runs this.',
    queued: 'Waiting for your gadget session to pick it up…',
    running: 'Working on it…',
    needs_input: 'Waiting for you in the session',
    done: 'Done',
    failed: 'Did not work',
    cancelled: 'Cancelled',
    finished_unreported: 'Stopped before it reported',
    interrupted: 'Stopped before it finished',
  };
  var LIVE_TEXT = { running: 'Running', needs_input: 'Needs your input', waiting: 'Waiting', failed: 'Failed', idle: 'Idle', done: 'Finished' };
  // Next up shows a step only when the session's estimate that the viewer clicks it reaches its
  // pattern's bar. A shown step nobody clicked or hid within IGNORED_MS counts as ignored.
  var DEFAULT_BAR = 0.7;
  var NEXT_UP_MAX = 5;
  var IGNORED_MS = 7 * 24 * 3600 * 1000;
  var HIT_WINDOW_MS = 30 * 24 * 3600 * 1000;
  var NUDGE_TEXT = { forgotten: 'Easy to forget.', overdue: 'Overdue.', due: 'Due now.' };
  var REASON_TEXT = { not_relevant: 'Not relevant', already_done: 'Already done' };

  var client = null;
  var preview = false;
  var canWrite = false;
  var canAct = false;
  var radarCollection = null;
  var layoutCollection = null;
  var stepRuns = null;
  var dismissed = null;
  var reports = null;
  var patternsCollection = null;
  var patternsDoc = null;
  var radarDoc = null;
  var radarLoaded = false;
  var layoutDoc = null;
  var runDocs = {};
  var dismissedDocs = {};
  var actionItems = [];
  var seen = {};
  // Next steps that finished while this page watched, by step ID: they show as done for a moment
  // in Next up, then stay under Done recently.
  var FRESH_MS = 8000;
  var freshDone = {};
  var openStepActions = {};
  var pendingScan = null;
  var pendingSteps = {};
  var pendingRestore = null;
  var dismissing = {};
  var scanOpenSince = 0;
  var live = { on: false, timer: null, byId: {}, error: '' };
  var renderQueued = false;
  var viewDirty = true;
  var previewSeeded = false;
  var lastWidth = 0;

  // ---- The view, and the fallback to the plain one

  var customView = null;
  var shownView = null;
  var viewFailure = null;
  var started = false;
  var reported = {};


  // A design that throws while it loads or draws is replaced by the plain view, its styles are
  // switched off, and the page reports it in render_reports for the gadget session.
  function failView(error) {
    if (viewFailure) return;
    viewFailure = { text: message(error || 'unknown error').slice(0, 300) };
    Array.prototype.forEach.call(document.querySelectorAll('style'), function (node) {
      if (node.id !== 'radar-kit-style') node.setAttribute('media', 'not all');
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
    }).catch(function () { return undefined; });
  }

  function drawView() {
    var root = $('view');
    var value = radar();
    if (!value) { root.replaceChildren(); shownView = null; return; }
    var view = customView && !viewFailure ? customView : plainView;
    if (view !== shownView) { root.replaceChildren(); shownView = view; }
    syncKit();
    try {
      view.render(root, viewModel(), kit);
      if (view === customView && !value.example) report('ok', '');
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
      radar: radar(),
      areas: areas(),
      sessions: sessionsById(),
      layout: layoutValue(),
      patterns: patterns(),
      revisionId: client ? client.context.revisionId : '',
      width: Math.round($('view').clientWidth || 0),
      preview: preview,
    };
  }

  // ---- States

  function stateKey(state) { return STATE_LABELS[state] ? state : 'active'; }
  function stateColor(state) { return 'var(--' + stateKey(state) + ')'; }
  function stateSoft(state) { return 'var(--' + stateKey(state) + '-soft)'; }
  function stateLabel(state) { return STATE_LABELS[stateKey(state)]; }
  function ref(path) { return 'radar/current#' + path; }

  // ---- The records

  function radar() { return radarDoc && radarDoc.value && Array.isArray(radarDoc.value.areas) ? radarDoc.value : null; }
  function layoutValue() { return layoutDoc && layoutDoc.value && layoutDoc.value.revision_id ? layoutDoc.value : null; }
  function areas() { var value = radar(); return value ? value.areas.filter(function (area) { return area && area.id && area.title; }) : []; }
  function sessionsById() {
    var out = {};
    var value = radar();
    ((value && value.sessions) || []).forEach(function (session) { if (session && session.id) out[session.id] = session; });
    return out;
  }
  function patternsValue() { return patternsDoc && patternsDoc.value && Array.isArray(patternsDoc.value.patterns) ? patternsDoc.value : null; }
  function patterns() { var value = patternsValue(); return value ? value.patterns.filter(function (pattern) { return pattern && pattern.id && pattern.name; }) : []; }
  function patternOf(step) {
    if (!step || !step.pattern) return null;
    return patterns().filter(function (pattern) { return pattern.id === step.pattern; })[0] || null;
  }
  function barOf(step) {
    var pattern = patternOf(step);
    return pattern && typeof pattern.bar === 'number' ? pattern.bar : DEFAULT_BAR;
  }
  // Whether the session is sure enough the viewer wants this step to show it at all. A step
  // without an estimate is not shown: a wrong step costs the viewer more than a missing one.
  function sure(step) { return Boolean(step) && typeof step.confidence === 'number' && step.confidence >= barOf(step); }
  function openSteps(area) {
    return (area.next_steps || []).filter(function (step) { return step && step.id && !dismissedDocs[step.id] && sure(step); });
  }
  // The steps Next up shows now, by ID: a view draws the others, so no step shows twice.
  var nextUpIds = {};
  function laterSteps(area) { return openSteps(area).filter(function (step) { return !nextUpIds[step.id]; }); }
  function hiddenSteps(area) {
    return (area.next_steps || []).filter(function (step) { return step && step.id && dismissedDocs[step.id]; });
  }
  // What became of each step Next up showed, from the session's ledger, brought up to date with
  // what this page already knows: a click, a hide, or no answer for a week.
  function outcomeOf(entry) {
    if (entry.outcome && entry.outcome !== 'open') return entry.outcome;
    if (dismissedDocs[entry.step]) return 'dismissed';
    if (runDocs[entry.step] || pendingSteps[entry.step] || latestAction('do_next_step', entry.step)) return 'clicked';
    var shown = Date.parse(entry.shown_at);
    return !isNaN(shown) && Date.now() - shown > IGNORED_MS ? 'ignored' : 'open';
  }
  function hitRatio(patternId) {
    var value = patternsValue();
    var clicked = 0;
    var resolved = 0;
    ((value && value.ledger) || []).forEach(function (entry) {
      if (!entry || !entry.step || (patternId && entry.pattern !== patternId)) return;
      var shown = Date.parse(entry.shown_at);
      if (isNaN(shown) || Date.now() - shown > HIT_WINDOW_MS) return;
      var outcome = outcomeOf(entry);
      if (outcome === 'open') return;
      resolved += 1;
      if (outcome === 'clicked') clicked += 1;
    });
    return { clicked: clicked, resolved: resolved };
  }
  // Related areas next to each other, blocked work first.
  function ordered(list) {
    var byId = {};
    list.forEach(function (area) { byId[area.id] = area; });
    var visited = {};
    var out = [];
    function visit(area) {
      if (visited[area.id]) return;
      visited[area.id] = true;
      out.push(area);
      (area.related || []).forEach(function (other) { if (byId[other]) visit(byId[other]); });
    }
    list.slice().sort(function (a, b) { return STATE_ORDER[stateKey(a.state)] - STATE_ORDER[stateKey(b.state)]; }).forEach(visit);
    return out;
  }

  // ---- Actions and their progress

  function latestAction(name, stepId) {
    var found = null;
    actionItems.forEach(function (item) {
      if (item.action !== name) return;
      if (stepId && !(item.params && item.params.next_step === stepId)) return;
      found = item;
    });
    return found;
  }
  function isOpen(action) { return Boolean(action) && OPEN.indexOf(action.status) !== -1; }
  // The scan to show: one a session is working on, else the latest. A later request the session
  // merged into an open one is already done, but the open one still runs.
  function shownScan() {
    var working = null;
    actionItems.forEach(function (item) {
      if (item.action === 'scan' && isOpen(item) && item.status !== 'pending') working = item;
    });
    return working || latestAction('scan');
  }

  function onActions(snapshot) {
    if (snapshot.status === 'loading') return;
    var items = snapshot.items || [];
    var key = items.map(function (item) { return [item.id, item.status, item.summary || ''].join(':'); }).join('|');
    if (key === seen.actions) return;
    seen.actions = key;
    actionItems = items;
    actionItems.forEach(function (item) {
      if (item.action !== 'do_next_step' || !item.params || typeof item.params.next_step !== 'string') return;
      if (isOpen(item)) { openStepActions[item.id] = true; return; }
      if (!openStepActions[item.id]) return;
      delete openStepActions[item.id];
      markFresh(item.params.next_step, item.status === 'done' ? 'done' : 'failed', item.params.label);
    });
    schedule();
  }

  function markFresh(stepId, state, label) {
    if (freshDone[stepId]) return;
    freshDone[stepId] = { at: Date.now(), state: state, label: label || '' };
    setTimeout(schedule, FRESH_MS + 100);
  }
  function isFresh(stepId) { return Boolean(freshDone[stepId]) && Date.now() - freshDone[stepId].at < FRESH_MS; }

  function windowDays() { return Number($('window').value) || 3; }

  function requestScan() {
    if (!canAct) return;
    pendingScan = { at: Date.now(), text: 'Sending to your gadget session…' };
    schedule();
    client.agent.request('scan', { window_days: windowDays() }).then(function (receipt) {
      var starting = Boolean(receipt && receipt.delivery === 'starting');
      pendingScan = { at: Date.now(), actionId: receipt && receipt.actionId, starting: starting, text: starting ? SCAN_TEXT.pending : 'Sent to your gadget session.' };
      schedule();
    }, function (error) {
      pendingScan = null;
      notice('Could not send the scan to your gadget session: ' + message(error), 'error');
      schedule();
    });
  }

  function requestStep(area, step) {
    if (!canAct) return;
    var pending = { at: Date.now(), text: 'Sending to your gadget session…' };
    pendingSteps[step.id] = pending;
    // Running it again starts a new finish to show.
    delete freshDone[step.id];
    schedule();
    var params = { next_step: step.id, label: step.label, area: area.id };
    if (step.pattern) params.pattern = step.pattern;
    client.agent.request('do_next_step', params).then(function (receipt) {
      if (pendingSteps[step.id] !== pending) return;
      pending.actionId = receipt && receipt.actionId;
      pending.starting = Boolean(receipt && receipt.delivery === 'starting');
      pending.text = pending.starting ? STEP_TEXT.pending : 'Sent to your gadget session.';
      schedule();
    }, function (error) {
      if (pendingSteps[step.id] !== pending) return;
      pending.error = 'Could not send it: ' + message(error);
      schedule();
    });
  }

  // The revision Back to the previous look returns to, when the page knows it.
  function previousRevision() {
    var layout = layoutValue();
    if (!layout || !client) return '';
    return layout.revision_id === client.context.revisionId ? layout.previous_revision_id || '' : '';
  }
  function requestRestore() {
    if (!canAct) return;
    var revision = previousRevision();
    pendingRestore = { at: Date.now() };
    schedule();
    client.agent.request('restore_look', revision ? { revision_id: revision } : {}).then(function (receipt) {
      if (pendingRestore) pendingRestore.actionId = receipt && receipt.actionId;
      schedule();
    }, function (error) {
      pendingRestore = null;
      notice('Could not ask for the previous look: ' + message(error), 'error');
      schedule();
    });
  }

  // One state for a next step: sending, running, done, failed or idle, with its text.
  function stepState(step) {
    var pending = pendingSteps[step.id];
    var action = latestAction('do_next_step', step.id);
    // The ledger can report the action before request() resolves, so match either way round.
    if (pending && (Date.now() - pending.at > PENDING_MS || (action && pending.actionId === action.id))) { delete pendingSteps[step.id]; pending = null; }
    if (pending) return pending.error ? { run: 'failed', text: pending.error, retry: true } : { run: 'sending', text: pending.text, spin: !pending.starting, retry: pending.starting };
    var record = runDocs[step.id] ? runDocs[step.id].value : null;
    if (isOpen(action)) {
      if (action.status === 'pending') return { run: 'sending', text: STEP_TEXT.pending, retry: true };
      return { run: 'running', text: STEP_TEXT[action.status], spin: action.status !== 'needs_input', session: action.sessionId };
    }
    // A step this page saw finish stays finished, even while its step_runs record still says
    // running and the ledger no longer lists its action.
    var finish = freshDone[step.id];
    if (finish && !(record && (record.state === 'done' || record.state === 'failed'))) {
      return { run: finish.state, text: finish.state === 'done' ? 'Done' : 'Did not work', session: action ? action.sessionId : record && record.session_id, again: true };
    }
    if (record && record.state === 'running' && !action) return { run: 'running', text: 'Working on it…', spin: true, session: record.session_id };
    if (record && (record.state === 'done' || record.state === 'failed')) {
      return { run: record.state, text: (record.state === 'done' ? 'Done' : 'Did not work') + (record.outcome ? ': ' + record.outcome : ''), session: record.session_id, again: true };
    }
    if (action) {
      var ok = action.status === 'done';
      return { run: ok ? 'done' : 'failed', text: (STEP_TEXT[action.status] || 'Ended') + (action.summary ? ': ' + action.summary : ''), session: action.sessionId, again: true };
    }
    return { run: 'idle' };
  }

  // ---- Dismissing a next step

  function dismiss(step) {
    if (!canWrite || dismissing[step.id]) return;
    dismissing[step.id] = true;
    schedule();
    var existing = dismissedDocs[step.id];
    var value = { at: new Date().toISOString(), label: step.label };
    if (step.pattern) value.pattern = step.pattern;
    var done = existing
      ? dismissed.delete(step.id, { ifVersion: existing.version }).then(function () { delete dismissedDocs[step.id]; })
      : dismissed.create(value, { id: step.id }).then(function (doc) { dismissedDocs[step.id] = doc; });
    done.then(function () {
      delete dismissing[step.id];
      if (existing) notice('');
      else {
        askedNow[step.id] = true;
        notice('Hidden “' + step.label + '”. Why?', '', [reasonButtons(step)]);
      }
      schedule();
    }, function (error) {
      delete dismissing[step.id];
      notice('Could not ' + (existing ? 'restore' : 'hide') + ' “' + step.label + '”: ' + message(error), 'error');
      schedule();
    });
  }
  function reasonButtons(step) {
    return el('span', { class: 'reasons' }, Object.keys(REASON_TEXT).map(function (reason) {
      return el('button', { type: 'button', 'data-omnigent-requires': 'write', 'data-reason': reason, text: REASON_TEXT[reason], on: { click: function () { giveReason(step, reason); } } });
    }));
  }
  // Steps hidden in the last week without a reason, so the viewer can still say why once the
  // notice is gone (after a reload): newest first, a few at most. A step hidden on this page asks
  // in its notice instead.
  var askedNow = {};
  function renderWhyHidden() {
    var box = $('why-hidden');
    var unanswered = canWrite ? Object.keys(dismissedDocs).filter(function (stepId) {
      var value = dismissedDocs[stepId].value || {};
      var at = Date.parse(value.at);
      return !askedNow[stepId] && !value.reason && !isNaN(at) && Date.now() - at < IGNORED_MS;
    }).sort(function (a, b) { return Date.parse(dismissedDocs[b].value.at) - Date.parse(dismissedDocs[a].value.at); }).slice(0, 3) : [];
    box.hidden = unanswered.length === 0;
    box.replaceChildren.apply(box, unanswered.map(function (stepId) {
      var label = dismissedDocs[stepId].value.label;
      var step = { id: stepId, label: label || 'that next step' };
      return el('div', { 'data-step': stepId }, ['You hid ' + (label ? '“' + label + '”' : 'a next step') + '. Why?', reasonButtons(step)]);
    }));
  }
  // The one-tap answer to why a step was hidden: the session uses it to tune that pattern.
  function giveReason(step, reason) {
    var doc = dismissedDocs[step.id];
    if (!canWrite || !doc) return;
    var value = {};
    Object.keys(doc.value || {}).forEach(function (key) { value[key] = doc.value[key]; });
    value.reason = reason;
    dismissed.replace(step.id, value, { ifVersion: doc.version }).then(function (saved) {
      dismissedDocs[step.id] = saved;
      notice('Thanks. Next up learns from that.');
      schedule();
    }, function (error) {
      notice('Could not save that: ' + message(error), 'error');
    });
  }

  // ---- Live session status

  function setLive(on) {
    live.on = on;
    if (live.timer) { clearInterval(live.timer); live.timer = null; }
    if (on) {
      pollLive();
      live.timer = setInterval(pollLive, 20000);
    } else {
      live.byId = {};
    }
    schedule();
  }
  function pollLive() {
    client.sessions.list({ limit: 100 }).then(function (page) {
      var byId = {};
      ((page && page.items) || []).forEach(function (session) { byId[session.id] = session; });
      live.byId = byId;
      live.error = '';
      schedule();
    }, function (error) {
      live.error = error && error.code === 'PERMISSION_DENIED'
        ? 'Live status is off: this gadget was not allowed to see your sessions.'
        : 'Could not read live status: ' + message(error);
      setLive(false);
      notice(live.error, 'error');
    });
  }
  function liveState(sessionId, captured) {
    var session = live.on ? live.byId[sessionId] : null;
    if (session) {
      if (session.needsInput) return 'needs_input';
      if (session.status === 'running' || session.backgroundWork) return 'running';
      return session.status;
    }
    return captured || '';
  }

  // ---- Pieces a design reuses

  function kindIcon(kind) {
    if (kind === 'monitor') {
      return svg('svg', { class: 'kind-icon', viewBox: '0 0 18 18', 'aria-hidden': 'true' }, [
        svg('path', { d: 'M1.5 9 C4 4.5 14 4.5 16.5 9 C14 13.5 4 13.5 1.5 9 Z' }),
        svg('circle', { class: 'kind-fill', cx: 9, cy: 9, r: 2.4 }),
      ]);
    }
    return svg('svg', { class: 'kind-icon', viewBox: '0 0 18 18', 'aria-hidden': 'true' }, [
      svg('circle', { cx: 9, cy: 9, r: 7.5 }),
      svg('path', { class: 'kind-fill', d: 'M7.2 5.6 L12.4 9 L7.2 12.4 Z' }),
    ]);
  }

  function openSession(sessionId) {
    client.navigation.openSession(sessionId, { display: 'panel' }).catch(function (error) {
      notice('Could not open that session: ' + message(error), 'error');
    });
  }

  function sessionTitle(sessionId) {
    var session = sessionsById()[sessionId];
    return session && session.title ? session.title : 'a session';
  }

  function isBusy(state) { return state.run === 'running' || (state.run === 'sending' && !state.retry); }

  // A next step as a compact button that runs it and shows its progress.
  // A step that did not work reads as a retry, not as a new step.
  function stepButton(area, step) {
    var state = stepState(step);
    var busy = isBusy(state);
    var failed = state.run === 'failed';
    return el('button', {
      type: 'button', class: 'next-chip', 'data-omnigent-requires': 'action', 'data-run': state.run, disabled: !canAct || busy,
      title: state.run === 'idle' ? (step.kind === 'monitor' ? 'Ask your gadget session to check on this' : 'Ask your gadget session to do this') : state.text,
      'data-omnigent-ref': ref('steps/' + step.id), 'data-omnigent-label': step.label,
      on: { click: function () { requestStep(area, step); } },
    }, [
      busy ? el('span', { class: 'spinner', 'aria-hidden': 'true' }) : failed ? el('span', { class: 'mark', 'aria-hidden': 'true', text: '!' }) : kindIcon(step.kind),
      el('span', { text: step.label }),
      el('span', { class: 'where', text: busy ? state.text : failed ? (state.retry ? 'Send again' : 'Try again') : area.title }),
    ]);
  }

  // A next step with its reason, evidence, progress and a way to hide it.
  function stepCard(area, step) {
    var state = stepState(step);
    var busy = isBusy(state);
    var main = el('button', {
      type: 'button', class: 'step-main', 'data-omnigent-requires': 'action', disabled: !canAct || busy || state.run === 'failed',
      title: step.kind === 'monitor' ? 'Ask your gadget session to check on this' : 'Ask your gadget session to do this',
      on: { click: function () { requestStep(area, step); } },
    }, [kindIcon(step.kind), el('span', { class: 'step-label', text: step.label })]);
    var why = [];
    if (step.why_now || step.why) why.push(plain(step.why_now || step.why) + ' ');
    var cited = (step.evidence && step.evidence.session_id) || step.session_id;
    if (cited) {
      why.push('From ');
      why.push(cite(sessionTitle(cited), function () { openSession(cited); }));
      if (step.evidence && step.evidence.quote) why.push(el('span', { class: 'quote', text: ': “' + plain(step.evidence.quote) + '”' }));
    }
    var children = [main];
    if (why.length) children.push(el('div', { class: 'step-why' }, why));
    if (state.run !== 'idle') {
      var line = [state.spin ? el('span', { class: 'spinner', 'aria-hidden': 'true' }) : null, el('span', { text: state.text })];
      if (state.session) line.push(el('button', { type: 'button', class: 'link-button', text: 'Open session', on: { click: function () { openSession(state.session); } } }));
      if ((state.again || state.retry) && canAct) line.push(el('button', { type: 'button', class: 'link-button', 'data-omnigent-requires': 'action', text: state.retry ? 'Send again' : state.run === 'failed' ? 'Try again' : 'Do it again', on: { click: function () { requestStep(area, step); } } }));
      children.push(el('div', { class: 'step-run', 'aria-live': 'polite' }, line));
    }
    if (canWrite) {
      children.push(el('button', { type: 'button', class: 'step-x', 'data-omnigent-requires': 'write', 'aria-label': 'Hide this next step', title: 'Hide this next step', disabled: Boolean(dismissing[step.id]), text: '×', on: { click: function () { dismiss(step); } } }));
    }
    return el('div', { class: 'step', 'data-kind': step.kind, 'data-run': state.run, 'data-omnigent-ref': ref('steps/' + step.id), 'data-omnigent-label': step.label }, children);
  }

  // A step in Next up: the button, why it is due now with its evidence, the pattern it comes
  // from, and a way to hide it.
  function nextItem(area, step) {
    var why = [];
    if (step.nudge && NUDGE_TEXT[step.nudge]) why.push(el('span', { class: 'nudge', text: NUDGE_TEXT[step.nudge] + ' ' }));
    if (step.why_now || step.why) why.push(plain(step.why_now || step.why) + ' ');
    var cited = (step.evidence && step.evidence.session_id) || step.session_id;
    var from = [];
    if (cited) {
      from.push('From ');
      from.push(cite(sessionTitle(cited), function () { openSession(cited); }));
      if (step.evidence && step.evidence.quote) from.push(el('span', { class: 'quote', text: ': “' + plain(step.evidence.quote) + '”' }));
    }
    var pattern = patternOf(step);
    if (pattern) from.push((from.length ? ' · ' : '') + 'Your pattern: ' + pattern.name);
    var children = [stepButton(area, step)];
    var state = stepState(step);
    if (state.run === 'failed') {
      var run = [el('span', { text: state.text })];
      if (state.session) run.push(' ', el('button', { type: 'button', class: 'link-button', text: 'Open session', on: { click: function () { openSession(state.session); } } }));
      children.push(el('p', { class: 'next-run', 'aria-live': 'polite' }, run));
    }
    if (why.length) children.push(el('p', { class: 'next-why' }, why));
    if (from.length) children.push(el('p', { class: 'next-from' }, from));
    if (canWrite) {
      children.push(el('button', { type: 'button', class: 'step-x', 'data-omnigent-requires': 'write', 'aria-label': 'Hide this next step', title: 'Hide this next step', disabled: Boolean(dismissing[step.id]), text: '×', on: { click: function () { dismiss(step); } } }));
    }
    return el('div', { class: 'next-item', id: 'next-' + step.id, 'data-nudge': step.nudge || null, 'data-omnigent-ref': ref('steps/' + step.id), 'data-omnigent-label': step.label }, children);
  }

  // A session worth watching: a link with its live status, its reason and its journal outcome.
  function sessionCard(session) {
    var state = liveState(session.id, session.status);
    var steps = session.steps || [];
    var meta = [];
    if (state) meta.push(LIVE_TEXT[state] || state);
    if (live.on && live.byId[session.id] && live.byId[session.id].unread) meta.push('new messages');
    if (session.why) meta.push(session.why);
    var finished = steps.filter(function (step) { return step.status === 'done'; }).length;
    if (steps.length) meta.push(finished + ' of ' + steps.length + ' steps done');
    return el('div', { class: 'session', 'data-omnigent-ref': ref('sessions/' + session.id), 'data-omnigent-label': session.title || 'Session' }, [
      el('button', { type: 'button', class: 'session-link', on: { click: function () { openSession(session.id); } } }, [
        el('span', { class: 'live-dot', 'data-live': state || 'unknown', 'aria-hidden': 'true' }),
        el('span', { text: session.title || 'Untitled session' }),
      ]),
      meta.length ? el('div', { class: 'session-meta', text: meta.join(' · ') }) : null,
      session.outcome ? el('div', { class: 'session-meta', text: session.outcome }) : null,
      steps.length ? el('div', { class: 'steps-bar', 'aria-hidden': 'true' }, steps.map(function (step) { return el('i', { 'data-s': step.status, title: step.step + ': ' + step.status }); })) : null,
    ]);
  }

  var kit = {
    el: el, svg: svg, keyed: keyed, ago: ago, hash: hash, wrap: wrap, plain: plain, cite: cite,
    stateColor: stateColor, stateSoft: stateSoft, stateLabel: stateLabel, stateOrder: STATE_ORDER,
    openSteps: openSteps, laterSteps: laterSteps, sure: sure, patternOf: patternOf, hitRatio: hitRatio,
    ordered: ordered, stepState: stepState, requestStep: requestStep,
    stepButton: stepButton, stepCard: stepCard, sessionCard: sessionCard, sessionTitle: sessionTitle,
    openSession: openSession, liveState: liveState, ref: ref, rerender: function () { schedule(); },
    calm: calm, canAct: false, canWrite: false, preview: false,
  };
  function syncKit() { kit.canAct = canAct; kit.canWrite = canWrite; kit.preview = preview; }

  // ---- The plain view: before the first design, and whenever a design fails to draw

  function areaCard(area, model) {
    var steps = laterSteps(area);
    var hidden = hiddenSteps(area);
    var watched = ((model.radar && model.radar.sessions) || []).filter(function (session) { return session.area === area.id && session.why; });
    return el('article', { class: 'area', id: 'card-' + area.id, 'data-state': stateKey(area.state), 'data-omnigent-ref': ref('areas/' + area.id), 'data-omnigent-label': area.title }, [
      el('div', { class: 'area-head' }, [el('h3', { text: area.title }), el('span', { class: 'pill', text: stateLabel(area.state) })]),
      area.summary ? el('p', { class: 'summary', text: area.summary }) : null,
      steps.length ? el('div', { class: 'block-label', text: 'Next steps' }) : null,
      steps.length ? el('div', { class: 'steps' }, steps.map(function (step) { return stepCard(area, step); })) : null,
      (area.items || []).length ? el('div', { class: 'block-label', text: 'Threads' }) : null,
      (area.items || []).length ? el('ul', { class: 'items' }, area.items.map(function (item) {
        return el('li', { 'data-omnigent-ref': ref('items/' + item.id), 'data-omnigent-label': item.title }, [
          el('span', { class: 'dot', style: 'background:' + stateColor(item.state), title: stateLabel(item.state) }),
          el('div', {}, [el('div', { text: item.title }), item.summary ? el('div', { class: 'item-sum', text: item.summary }) : null]),
        ]);
      })) : null,
      watched.length ? el('div', { class: 'block-label', text: 'Keep an eye on' }) : null,
      watched.length ? el('div', { class: 'watch' }, watched.map(sessionCard)) : null,
      hidden.length ? el('div', { class: 'dismissed-note' }, [
        hidden.length === 1 ? 'You hid a next step here. ' : 'You hid some next steps here. ',
        canWrite ? el('button', { type: 'button', class: 'link-button', 'data-omnigent-requires': 'write', text: 'Show again', on: { click: function () {
          hidden.forEach(dismiss);
        } } }) : null,
      ]) : null,
    ]);
  }

  var plainView = {
    name: 'Plain',
    render: function (root, model) {
      var grid = root.firstChild;
      if (!grid || grid.className !== 'areas') { grid = el('div', { class: 'areas' }); root.replaceChildren(grid); }
      keyed(grid, ordered(model.areas), function (area) { return area.id; }, function (area) { return areaCard(area, model); });
    },
  };

  // ---- Rendering

  function schedule(statusOnly) {
    if (!statusOnly) viewDirty = true;
    if (renderQueued) return;
    renderQueued = true;
    (window.requestAnimationFrame || setTimeout)(function () { renderQueued = false; render(); });
  }

  function notice(textValue, kind, extra) {
    var box = $('notice');
    box.hidden = !textValue;
    box.textContent = textValue || '';
    (extra || []).forEach(function (node) { box.appendChild(node); });
    if (kind) box.setAttribute('data-kind', kind); else box.removeAttribute('data-kind');
  }

  function render() {
    var value = radar();
    $('loading').hidden = radarLoaded;
    renderHeader(value);
    renderScanStatus(value);
    var empty = radarLoaded && !value;
    $('empty').hidden = !empty;
    $('content').hidden = !value;
    $('guide').open = $('guide').open || empty;
    if (!viewDirty) return;
    viewDirty = false;
    renderLook(value);
    if (!value) { drawView(); return; }
    $('nothing').hidden = areas().length > 0;
    renderNextUp(areas(), value);
    drawView();
    renderDone();
    renderLearned();
  }

  function renderHeader(value) {
    var parts = [];
    if (value) {
      if (value.example) parts.push('Example');
      else if (value.scanned_at) parts.push('Scanned ' + ago(value.scanned_at));
      parts.push('last ' + value.window_days + (value.window_days === 1 ? ' day' : ' days'));
      if (typeof value.sessions_read === 'number') parts.push(value.sessions_read + (value.sessions_read === 1 ? ' session read' : ' sessions read'));
    }
    $('meta').textContent = parts.join(' · ');
    $('headline').textContent = (value && value.headline) || '';
    $('headline').hidden = !(value && value.headline);
    $('scan').hidden = !value;
    var liveButton = $('live');
    liveButton.hidden = !value || preview || !client || !client.sessions || !client.sessions.canList;
    liveButton.setAttribute('aria-pressed', live.on ? 'true' : 'false');
    liveButton.textContent = live.on ? 'Live status on' : 'Show live status';
  }


  function renderScanStatus(value) {
    var box = $('run');
    box.replaceChildren();
    var action = shownScan();
    if (pendingScan && (Date.now() - pendingScan.at > PENDING_MS || (action && pendingScan.actionId === action.id))) pendingScan = null;
    var open = Boolean(pendingScan && !pendingScan.starting) || (isOpen(action) && action.status !== 'pending');
    var undelivered = Boolean(pendingScan && pendingScan.starting) || (!pendingScan && action && action.status === 'pending');
    var working = open && Boolean(action && action.status === 'running');
    // Count from the request in the ledger, so a page reloaded mid-scan (as after the first
    // design publishes) keeps counting instead of starting again.
    var requested = action && action.requestedAt ? Date.parse(action.requestedAt) : NaN;
    if (open && !isNaN(requested)) scanOpenSince = Math.min(requested, Date.now());
    else if (open && !scanOpenSince) scanOpenSince = pendingScan ? pendingScan.at : Date.now();
    if (!open) scanOpenSince = 0;
    // Records this scan already wrote: the radar is newer than the request.
    var written = working && value && !value.example && !isNaN(requested) && Date.parse(value.scanned_at) >= requested;
    [$('scan'), $('scan-empty')].forEach(function (button) {
      button.disabled = !canAct || (open && !undelivered);
      button.textContent = open ? 'Scanning…' : undelivered ? 'Create my gadget session' : button.id === 'scan' ? 'Rescan' : 'Scan my recent work';
    });
    // The first scan also designs the page: once its records are written and until a layout
    // exists, a running scan is designing.
    var designing = working && value && !value.example && !layoutValue() && (isNaN(requested) || written);
    var why = action && action.summary && (action.status === 'failed' || action.status === 'cancelled') ? ': ' + action.summary : '';
    var textValue = pendingScan ? pendingScan.text : action && SCAN_TEXT[action.status] ? SCAN_TEXT[action.status] + why : '';
    if (designing) textValue = 'Designing a page for your work. This is the plain view until it is ready.';
    else if (written) textValue = 'Map updated. Finishing the scan…';
    if (open && value) box.appendChild(el('span', { class: 'spinner', 'aria-hidden': 'true' }));
    if (textValue && value) box.appendChild(el('span', { text: textValue }));
    if (working && scanOpenSince && value) box.appendChild(el('span', { class: 'elapsed', text: elapsed(scanOpenSince) + (layoutValue() ? '' : ' · usually 1 to 3 minutes') }));
    box.hidden = box.childNodes.length === 0;
    // Before the first scan the status sits under the empty state's button, with its stages.
    $('empty-run').textContent = !value && textValue ? textValue + (working && scanOpenSince ? ' ' + elapsed(scanOpenSince) + ' · usually 1 to 3 minutes' : '') : '';
    var stages = $('progress');
    stages.hidden = !(working && !value);
    Array.prototype.forEach.call(stages.children, function (item, index) {
      item.setAttribute('data-now', index === 0 && working && !value ? 'true' : 'false');
    });
  }

  function renderLook(value) {
    var box = $('look');
    box.replaceChildren();
    box.removeAttribute('data-kind');
    var layout = layoutValue();
    var restore = latestAction('restore_look');
    if (pendingRestore && (Date.now() - pendingRestore.at > PENDING_MS || (restore && pendingRestore.actionId === restore.id))) pendingRestore = null;
    var restoring = Boolean(pendingRestore) || isOpen(restore);
    var parts = [];
    if (value && value.example) {
      parts.push(el('span', { text: 'After its first scan, your gadget session designs this page around your work.' }));
    } else if (viewFailure) {
      box.setAttribute('data-kind', 'failed');
      parts.push(el('b', { text: 'This look could not be drawn, so this is the plain view.' }));
      parts.push(el('span', { text: viewFailure.text }));
    } else if (layout && client && layout.revision_id === client.context.revisionId) {
      var changed = layout.change_note ? ' · ' + clause(layout.change_note) + (layout.changed_at ? ', ' + ago(layout.changed_at) : '') : '';
      parts.push(el('span', {}, ['Look: ', el('b', { text: layout.structure }), changed]));
    }
    if (restoring) {
      parts.push(el('span', { class: 'spinner', 'aria-hidden': 'true' }));
      parts.push(el('span', { text: 'Going back to the previous look…' }));
    } else if (canAct && value && !value.example && (viewFailure || customView)) {
      parts.push(el('button', { type: 'button', id: 'restore', class: 'quiet', 'data-omnigent-requires': 'action', text: 'Back to the previous look', on: { click: requestRestore } }));
    }
    if (restore && !restoring && restore.status === 'failed') parts.push(el('span', { text: 'Could not go back' + (restore.summary ? ': ' + restore.summary : '') }));
    box.hidden = parts.length === 0;
    parts.forEach(function (part) { box.appendChild(part); });
  }

  // Only the steps the session is sure the viewer wants, surest first; a step they tend to forget
  // goes ahead of an equally sure one. None at all is a fine answer, and says so.
  function renderNextUp(list, value) {
    var picks = [];
    list.forEach(function (area) {
      openSteps(area).forEach(function (step, position) {
        var state = stepState(step);
        // One chip per step: a step that just finished shows only as its finished chip.
        if (state.run === 'done' || isFresh(step.id)) return;
        var lift = step.nudge === 'forgotten' || step.nudge === 'overdue' ? 0.05 : 0;
        picks.push({ area: area, step: step, score: -(step.confidence + lift) * 100 + STATE_ORDER[stateKey(area.state)] + position / 10 });
      });
    });
    picks.sort(function (a, b) { return a.score - b.score; });
    picks = picks.slice(0, NEXT_UP_MAX);
    nextUpIds = {};
    picks.forEach(function (pick) { nextUpIds[pick.step.id] = true; });
    Object.keys(freshDone).filter(isFresh).forEach(function (stepId) { nextUpIds[stepId] = true; });
    var finished = Object.keys(freshDone).filter(isFresh).map(function (stepId) {
      var done = freshDone[stepId];
      return el('div', { class: 'next-chip finished', 'data-state': done.state, role: 'status' }, [
        el('span', { class: 'mark', 'aria-hidden': 'true', text: done.state === 'done' ? '✓' : '!' }),
        el('span', { text: done.label || 'Next step' }),
        el('span', { class: 'where', text: done.state === 'done' ? 'Done' : 'Did not work' }),
      ]);
    });
    var quiet = picks.length === 0 && finished.length === 0;
    $('next-section').hidden = list.length === 0;
    $('next-empty').hidden = !quiet;
    $('next-list').hidden = finished.length === 0;
    $('next-list').replaceChildren.apply($('next-list'), finished);
    keyed($('next-items'), picks, function (pick) { return pick.step.id; }, function (pick) { return nextItem(pick.area, pick.step); });
    var hits = hitRatio();
    renderWhyHidden();
    $('hit').hidden = !hits.resolved;
    $('hit').textContent = hits.resolved ? 'Next up hit ratio, last 30 days: ' + hits.clicked + ' of ' + hits.resolved + ' clicked (' + Math.round(100 * hits.clicked / hits.resolved) + '%). It shows fewer steps when you skip them.' : '';
  }

  // What the session learned about how the viewer works, kept out of the way.
  function renderLearned() {
    var list = patterns();
    $('learned').hidden = list.length === 0;
    $('learned-list').replaceChildren.apply($('learned-list'), list.map(function (pattern) {
      var steps = [];
      (pattern.steps || []).forEach(function (step, index) {
        if (index) steps.push(el('i', { text: '→', 'aria-hidden': 'true' }));
        steps.push(el('span', { 'data-forgotten': step.forgotten ? 'true' : null, title: step.forgotten ? 'You often skip this step or do it late' : '', text: step.label + (step.forgotten ? ' (often forgotten)' : '') }));
      });
      var hits = hitRatio(pattern.id);
      var meta = [];
      if (pattern.summary) meta.push(plain(pattern.summary));
      if (hits.resolved) meta.push('Next up hits: ' + hits.clicked + ' of ' + hits.resolved);
      return el('li', { 'data-omnigent-ref': 'patterns/current#' + pattern.id, 'data-omnigent-label': pattern.name }, [
        el('b', { text: pattern.name }),
        meta.length ? el('div', { class: 'session-meta', text: meta.join(' · ') }) : null,
        el('div', { class: 'learned-steps' }, steps),
      ]);
    }));
  }

  // Finished next steps: the session's step_runs records, and finished do_next_step actions it
  // reported without one. Steps that finished while this page watched come first, highlighted.
  function renderDone() {
    var rows = {};
    Object.keys(runDocs).forEach(function (key) {
      var doc = runDocs[key];
      var value = doc.value;
      if (!value || (value.state !== 'done' && value.state !== 'failed')) return;
      rows[key] = { key: key, label: value.label, state: value.state, outcome: value.outcome, session: value.session_id, at: value.updated || doc.updatedAt, ref: 'step_runs/' + key };
    });
    actionItems.forEach(function (item, index) {
      var stepId = item.action === 'do_next_step' && item.params ? item.params.next_step : null;
      if (typeof stepId !== 'string' || rows[stepId] || (item.status !== 'done' && item.status !== 'failed')) return;
      rows[stepId] = { key: stepId, label: item.params.label || 'Next step', state: item.status, outcome: item.summary, session: item.sessionId, order: index };
    });
    var list = Object.keys(rows).map(function (key) { return rows[key]; }).sort(function (a, b) {
      var fresh = (isFresh(b.key) ? 1 : 0) - (isFresh(a.key) ? 1 : 0);
      if (fresh) return fresh;
      if (a.at && b.at) return a.at < b.at ? 1 : -1;
      if (a.at || b.at) return a.at ? -1 : 1;
      return (b.order || 0) - (a.order || 0);
    }).slice(0, 6);
    $('done-section').hidden = list.length === 0;
    $('done-list').replaceChildren.apply($('done-list'), list.map(function (row) {
      var when = isFresh(row.key) ? 'just now' : ago(row.at);
      return el('li', { 'data-state': row.state, 'data-fresh': isFresh(row.key) ? 'true' : null, 'data-omnigent-ref': row.ref || null, 'data-omnigent-label': row.label }, [
        el('span', { class: 'mark', 'aria-hidden': 'true', text: row.state === 'done' ? '✓' : '!' }),
        el('div', {}, [
          el('div', { text: row.label }),
          el('div', { class: 'session-meta' }, [
            (row.state === 'done' ? 'Done' : 'Did not work') + (when ? ' ' + when : '') + (row.outcome ? ': ' + row.outcome : ''),
            row.session ? ' ' : null,
            row.session ? el('button', { type: 'button', class: 'link-button', text: 'Open session', on: { click: function () { openSession(row.session); } } }) : null,
          ]),
        ]),
      ]);
    }));
  }

  // ---- The template preview's example, kept in memory

  function seedPreview() {
    previewSeeded = true;
    var now = new Date().toISOString();
    radarCollection.create({
      scanned_at: now, window_days: 3, sessions_read: 14, example: true,
      headline: 'An example: three efforts are moving, one is blocked on a review, and two follow-ups are waiting on you.',
      areas: [
        { id: 'checkout-rollout', title: 'New checkout flag rollout', state: 'active', kind: 'rollout', related: ['latency'],
          summary: 'The new checkout is at 5% with error rates flat since Tuesday.',
          items: [{ id: 'ramp', title: 'Ramp from 1% to 5%', state: 'done' }, { id: 'dash', title: 'Error-rate dashboard', state: 'active' }],
          next_steps: [{ id: 'ramp-10', label: 'Ramp the flag to 10%', kind: 'act', confidence: 0.86, pattern: 'flag-rollout', pattern_step: 'ramp', nudge: 'due',
              why_now: 'Two quiet days at 5%; you usually ramp after two days.', evidence: { session_id: 'example-1', quote: 'no change in 5xx after the ramp' } },
            { id: 'checkout-telemetry', label: 'Add a checkout funnel dashboard', kind: 'act', confidence: 0.78, pattern: 'ship-feature', pattern_step: 'telemetry', nudge: 'forgotten',
              why_now: 'You added a dashboard only after someone asked on your last two launches; this one has none yet.' },
            { id: 'watch-p95', label: 'Check p95 latency after the ramp', kind: 'monitor', confidence: 0.55, pattern: 'flag-rollout', why_now: 'The last ramp moved p95 by 8 ms.' }] },
        { id: 'latency', title: 'Search latency investigation', state: 'waiting', kind: 'investigation', related: ['checkout-rollout'],
          summary: 'Slow requests trace to a cold cache after deploys; a fix is in review.',
          items: [{ id: 'trace', title: 'Traced the slow path', state: 'done' }, { id: 'fix', title: 'Cache warm-up fix', state: 'waiting' }],
          next_steps: [{ id: 'latency-verify', label: 'Verify p90 once the fix ships', kind: 'monitor', confidence: 0.4, why_now: 'The fix has not shipped yet.' }] },
        { id: 'reviews', title: 'Pull requests to review', state: 'blocked', kind: 'review',
          summary: 'Two pull requests are waiting on your review; one blocks the cache fix.',
          items: [{ id: 'pr-cache', title: 'Cache warm-up fix', state: 'blocked' }, { id: 'pr-docs', title: 'Docs for the new flag', state: 'waiting' }],
          next_steps: [{ id: 'review-cache', label: 'Review the cache warm-up fix', kind: 'act', confidence: 0.74, nudge: 'overdue', why_now: 'It has waited three days and blocks the latency fix.' }] },
      ],
      sessions: [
        { id: 'example-1', title: 'Ramp checkout flag', area: 'checkout-rollout', why: 'Watch errors after the next ramp', status: 'idle', outcome: 'Ramped to 5%; error rate unchanged.', steps: [{ step: 'ramp', status: 'done' }, { step: 'verify', status: 'done' }, { step: 'next-ramp', status: 'started' }] },
      ],
    }, { id: 'current' }).then(function (doc) {
      // Show it now rather than at the next poll.
      if (!radarDoc) { radarDoc = doc; schedule(); }
    }, function () { return undefined; });
    var day = 24 * 3600 * 1000;
    function daysAgo(days) { return new Date(Date.now() - days * day).toISOString(); }
    patternsCollection.create({
      updated_at: now, history_days: 30, sessions_seen: 48,
      patterns: [
        { id: 'flag-rollout', name: 'Flag rollout', summary: 'Merge behind a flag, ramp in steps, check metrics after each ramp, then remove the flag.', seen: 4, bar: 0.7,
          steps: [{ id: 'merge', label: 'Merge behind a flag', done_rate: 1 }, { id: 'ramp', label: 'Ramp', after_days: 2, done_rate: 1 },
            { id: 'metric', label: 'Check metrics', after_days: 1, done_rate: 0.75 }, { id: 'cleanup', label: 'Remove the flag', after_days: 14, done_rate: 0.25, forgotten: true }],
          evidence: [{ session_id: 'example-1', quote: 'ramped to 5%, watching 5xx' }], hits: { shown: 5, clicked: 4, dismissed: 1, ignored: 0 } },
        { id: 'ship-feature', name: 'Shipping a feature', summary: 'Telemetry and a dashboard come late, usually after someone asks.', seen: 3, bar: 0.75,
          steps: [{ id: 'ship', label: 'Ship', done_rate: 1 }, { id: 'telemetry', label: 'Add telemetry', after_days: 0, done_rate: 0.33, forgotten: true }, { id: 'announce', label: 'Announce', after_days: 1, done_rate: 0.66 }],
          hits: { shown: 3, clicked: 2, dismissed: 0, ignored: 1 } },
      ],
      ledger: [
        { step: 'ramp-5', pattern: 'flag-rollout', label: 'Ramp the flag to 5%', shown_at: daysAgo(2), outcome: 'clicked' },
        { step: 'cleanup-search', pattern: 'flag-rollout', label: 'Remove the search flag', shown_at: daysAgo(5), outcome: 'clicked' },
        { step: 'metric-1', pattern: 'flag-rollout', label: 'Check errors after the 1% ramp', shown_at: daysAgo(6), outcome: 'clicked' },
        { step: 'ramp-1', pattern: 'flag-rollout', label: 'Ramp the flag to 1%', shown_at: daysAgo(8), outcome: 'clicked' },
        { step: 'metric-old', pattern: 'flag-rollout', label: 'Check p95 after the docs flag', shown_at: daysAgo(9), outcome: 'dismissed', reason: 'already_done' },
        { step: 'tele-search', pattern: 'ship-feature', label: 'Add search telemetry', shown_at: daysAgo(10), outcome: 'clicked' },
        { step: 'dash-docs', pattern: 'ship-feature', label: 'Add a docs dashboard', shown_at: daysAgo(12), outcome: 'clicked' },
        { step: 'announce-docs', pattern: 'ship-feature', label: 'Announce the docs launch', shown_at: daysAgo(13), outcome: 'ignored' },
      ],
    }, { id: 'current' }).then(function (doc) {
      if (!patternsDoc) { patternsDoc = doc; schedule(); }
    }, function () { return undefined; });
  }

  // ---- Wiring

  function stamp(items) {
    return (items || []).map(function (doc) { return doc.id + '@' + doc.version; }).join('|');
  }
  function onRadar(snapshot) {
    if (snapshot.status === 'loading') return;
    // A newer revision means the session published a look; Omnigent loads it in a moment.
    if (snapshot.status === 'stale' && snapshot.error && snapshot.error.code === 'REVISION_CHANGED') notice('Loading the new look…');
    else if (snapshot.status === 'stale' && snapshot.error) notice('Could not load your work: ' + message(snapshot.error), 'error');
    var current = (snapshot.items || []).filter(function (doc) { return doc.id === 'current'; })[0] || null;
    var key = stamp(current ? [current] : []);
    if (radarLoaded && key === seen.radar) return;
    seen.radar = key;
    radarDoc = current || (preview ? radarDoc : null);
    radarLoaded = true;
    if (preview && !radarDoc && !previewSeeded && snapshot.status === 'live') seedPreview();
    var value = radar();
    if (value && !$('window').dataset.touched) $('window').value = String([1, 3, 7, 14].indexOf(value.window_days) !== -1 ? value.window_days : 3);
    schedule();
  }
  function onLayout(snapshot) {
    if (snapshot.status === 'loading') return;
    var key = stamp(snapshot.items);
    if (key === seen.layout) return;
    seen.layout = key;
    layoutDoc = (snapshot.items || []).filter(function (doc) { return doc.id === 'current'; })[0] || null;
    schedule();
  }
  function onRuns(items, complete) {
    var key = stamp(items) + (complete ? '' : '+');
    if (key === seen.runs) return;
    seen.runs = key;
    var next = mergeKnown(runDocs, items, complete);
    Object.keys(next).forEach(function (stepId) {
      var before = runDocs[stepId] && runDocs[stepId].value;
      var now = next[stepId].value;
      if (before && before.state === 'running' && now && (now.state === 'done' || now.state === 'failed')) markFresh(stepId, now.state, now.label);
    });
    runDocs = next;
    schedule();
  }
  function onDismissed(items, complete) {
    var key = stamp(items) + (complete ? '' : '+');
    if (key === seen.dismissed) return;
    seen.dismissed = key;
    dismissedDocs = mergeKnown(dismissedDocs, items, complete);
    schedule();
  }

  function onPatterns(snapshot) {
    if (snapshot.status === 'loading') return;
    var current = (snapshot.items || []).filter(function (doc) { return doc.id === 'current'; })[0] || null;
    var key = stamp(current ? [current] : []);
    if (key === seen.patterns) return;
    seen.patterns = key;
    patternsDoc = current || (preview ? patternsDoc : null);
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
    $('scan').addEventListener('click', requestScan);
    $('scan-empty').addEventListener('click', requestScan);
    $('window').addEventListener('change', function () { $('window').dataset.touched = 'true'; });
    $('live').addEventListener('click', function () { setLive(!live.on); });
    $('daily').addEventListener('click', function () {
      var name = client.context.app.name;
      automate('Refresh ' + name, 'Load the gadget skill and follow it. Without the gadget skill or sys_gadget_* tools, say the host of this session does not support gadgets yet, name what is missing, and stop. Refresh the gadget "' + name + '" (gadget_id ' + client.context.app.id + '): scan my sessions from the last ' + windowDays() + ' days as its guide describes under Scan.', 'FREQ=DAILY;BYHOUR=8;BYMINUTE=0');
    });
    $('automate').addEventListener('click', function () {
      var name = client.context.app.name;
      automate('Next step from ' + name, 'Load the gadget skill and follow it. Without the gadget skill or sys_gadget_* tools, say the host of this session does not support gadgets yet, name what is missing, and stop. In the gadget "' + name + '" (gadget_id ' + client.context.app.id + '), do this next step as its guide describes under Next steps: (say which step, for example: check the error rate after the latest ramp).', 'FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9;BYMINUTE=0');
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
      if (client.context.app.name) $('title').textContent = client.context.app.name;
      preview = client.context.preview === true;
      canWrite = client.hasCapability('data.write') && client.can('write');
      canAct = Boolean(client.agent) && client.can('action') && !preview;
      $('daily').hidden = !canAct;
      $('automate').hidden = !canAct;
      if (!preview && !client.can('action')) notice('You can look at this page. Scanning and next steps run in the owner’s gadget session.');
      radarCollection = client.data.collection('radar');
      layoutCollection = client.data.collection('layout');
      stepRuns = client.data.collection('step_runs');
      dismissed = client.data.collection('dismissed');
      reports = client.data.collection('render_reports');
      patternsCollection = client.data.collection('patterns');
      if (viewFailure) report('failed', viewFailure.text);
      radarCollection.watch(onRadar, { intervalMs: 4000, limit: 5 });
      layoutCollection.watch(onLayout, { intervalMs: 8000, limit: 5 });
      watchEveryPage(stepRuns, onRuns, { intervalMs: 4000, limit: 100 });
      watchEveryPage(dismissed, onDismissed, { intervalMs: 8000, limit: 100 });
      patternsCollection.watch(onPatterns, { intervalMs: 8000, limit: 5 });
      if (canAct && typeof client.agent.watch === 'function') client.agent.watch(onActions, { intervalMs: 3000 });
      schedule();
    }, function (error) {
      $('loading').hidden = true;
      notice('The gadget could not connect to Omnigent: ' + message(error), 'error');
    });
  }

  window.Radar = {
    version: 1,
    plainView: plainView,
    view: function (definition) {
      if (!definition || typeof definition.render !== 'function') { failView('Radar.view needs an object with a render function'); return; }
      customView = definition === plainView ? null : definition;
      shownView = null;
      schedule();
    },
    start: start,
  };
})();
`;

const RADAR_ART = `
<svg class="radar-art" viewBox="0 0 320 220" aria-hidden="true">
  <circle class="ring" cx="160" cy="110" r="96"/>
  <circle class="ring" cx="160" cy="110" r="64"/>
  <circle class="ring" cx="160" cy="110" r="32"/>
  <line class="axis" x1="60" y1="110" x2="260" y2="110"/>
  <line class="axis" x1="160" y1="10" x2="160" y2="210"/>
  <path class="sweep" d="M160 110 L160 14 A96 96 0 0 1 228 42 Z"/>
  <circle class="blip b1" cx="205" cy="70" r="7"/>
  <circle class="blip b2" cx="112" cy="62" r="6"/>
  <circle class="blip b3" cx="118" cy="160" r="6"/>
  <circle class="blip b4" cx="214" cy="150" r="5"/>
</svg>`;

const KIT_BODY = `
<main>
  <div class="top">
    <div>
      <p class="kicker">Where was I? <span class="badge" title="Built from the owner's own sessions. Anyone you share this gadget with sees these summaries.">Personal</span></p>
      <h1 id="title">Where was I?</h1>
      <div class="meta" id="meta"></div>
    </div>
    <div class="toolbar">
      <label class="window">Last
        <select id="window" aria-label="Days to scan">
          <option value="1">1 day</option>
          <option value="3" selected>3 days</option>
          <option value="7">7 days</option>
          <option value="14">14 days</option>
        </select>
      </label>
      <button type="button" id="scan" class="primary" data-omnigent-requires="action" hidden>Rescan</button>
      <button type="button" id="live" class="quiet" aria-pressed="false" hidden>Show live status</button>
    </div>
  </div>
  <p id="headline" class="headline" hidden></p>
  <div id="run" class="status" aria-live="polite"></div>
  <div id="look" class="look" aria-live="polite" hidden></div>
  <p id="notice" class="notice" role="status" hidden></p>
  <p id="loading" class="muted">Loading…</p>

  <section id="empty" class="empty" hidden>
    <div>
      <h2>See what your work adds up to</h2>
      <p>Your gadget session reads the sessions you worked in recently and works out what they add up to: the efforts you are driving, what is blocked or waiting on you, and the workflows you repeat, so it can tell you the step that is due, such as checking a metric after a ramp or the telemetry you meant to add.</p>
      <p>Then it designs this page around your work, a rollout as stages or reviews as lanes, and keeps it up to date as the work moves. Click a next step and your gadget session does it or checks on it.</p>
      <button type="button" id="scan-empty" class="primary" data-omnigent-requires="action">Scan my recent work</button>
      <ol id="progress" class="progress" hidden>
        <li>Reading your recent sessions and what they add up to</li>
        <li>Designing a page for your work</li>
      </ol>
      <p id="empty-run" class="status"></p>
    </div>
    ${RADAR_ART}
  </section>

  <div id="content" hidden>
    <p id="nothing" class="notice" hidden>Nothing to show for this window. Try a longer one, then Rescan.</p>
    <section id="next-section" hidden>
      <h2>Next up</h2>
      <div id="next-list" class="next-list" hidden></div>
      <div id="next-items" class="next-items"></div>
      <div id="why-hidden" class="why-hidden" hidden></div>
      <p id="next-empty" class="next-empty" hidden>Nothing needs you right now. Next up shows a step only when your past work says it is due and you will want it.</p>
    </section>
    <div id="view" class="view"></div>
    <section id="done-section" hidden>
      <h2>Done recently</h2>
      <ul id="done-list" class="done-list"></ul>
    </section>
    <details id="learned" class="learned" hidden>
      <summary>How you usually work, as your gadget session learned it</summary>
      <ul id="learned-list" class="learned-list"></ul>
    </details>
    <p id="hit" class="hit" hidden></p>
  </div>

  <section>
    <details id="guide" class="guide">
      <summary>How this works, and how to make it yours</summary>
      <div class="guide-grid">
        <div>
          <h3>How this works</h3>
          <div class="layers">
            <div class="layer"><svg viewBox="0 0 26 26" aria-hidden="true"><circle cx="13" cy="13" r="9" fill="var(--active-soft)" stroke="var(--active)" stroke-width="2"/></svg><div><b>This page</b> comes from your gadget session. On Scan it reads your recent sessions (their titles, journals and last messages, not whole transcripts), works out what they add up to and what needs you, and writes it down.</div></div>
            <div class="layer"><svg viewBox="0 0 26 26" aria-hidden="true"><rect x="5" y="5" width="16" height="16" rx="3" fill="none" stroke="var(--accent)" stroke-width="2"/><path d="M5 12 H21 M12 12 V21" stroke="var(--accent)" stroke-width="2"/></svg><div><b>The page</b> is designed for your work. After the first scan your gadget session designs it to fit what you do; later scans update it in place. It changes the design only when your work no longer fits, and says what changed. <b>Back to the previous look</b> undoes a change.</div></div>
            <div class="layer"><svg viewBox="0 0 26 26" aria-hidden="true"><rect x="8" y="8" width="10" height="10" rx="2" transform="rotate(45 13 13)" fill="var(--accent)"/></svg><div><b>Next up learns from what you click.</b> Your gadget session learns the workflows you repeat, such as a flag ramp, then a metric check, then removing the flag, and shows a step only when it is due and you are likely to want it, so Next up is often empty. Click one and your gadget session does it, hands it to the session it came from, or starts a new one. Hide one with × and say why in one tap; skipped steps make it show fewer like them.</div></div>
            <div class="layer"><svg viewBox="0 0 26 26" aria-hidden="true"><circle cx="13" cy="13" r="5" fill="var(--active)"/><circle cx="13" cy="13" r="10" fill="none" stroke="var(--active)" stroke-width="1.5" stroke-dasharray="3 3"/></svg><div><b>Live status</b> is optional. Turn it on and this page shows, without asking the agent, which linked sessions are running or need your input.</div></div>
          </div>
          <h3>Personal</h3>
          <ul><li>This page summarizes your own sessions. Anyone you share this gadget with sees those summaries, so share it only when that is fine.</li></ul>
        </div>
        <div>
          <h3>Make it yours</h3>
          <ul>
            <li>Ask your gadget session, in its chat: <span class="ask">“Show my pull request reviews as lanes.”</span></li>
            <li><span class="ask">“Put blocked work first and make the page more compact.”</span></li>
            <li><span class="ask">“Scan 7 days by default and leave out sessions about the docs site.”</span></li>
            <li>Run a next step often? <button type="button" id="automate" class="link-button" data-omnigent-requires="action" hidden>Make it an automation</button>.</li>
            <li>Rescan whenever you like, or keep it fresh on its own: <button type="button" id="daily" class="link-button" data-omnigent-requires="action" hidden>refresh it every morning</button>.</li>
          </ul>
        </div>
      </div>
    </details>
  </section>
  <footer>Select any text, or Alt+click an area or next step, to reply about it or ask in a side chat.</footer>
</main>
`;

// How a design plugs into the kit. The session reads this in the revision it replaces.
const VIEW_API = `<!--
The view region. A design replaces everything from radar:view:start to radar:view:end with one
style element and one script element, and keeps the rest of this file exactly as it is.

The script calls Radar.view({ name: 'Rollout stages', render: function (root, model, kit) { ... } }).
render runs after every change to the records and when the page resizes. Update root in place
with kit.keyed and stable IDs so the page animates instead of redrawing. If the script or render
throws, the kit switches the design's styles off, shows its plain view and reports the error in
render_reports.

model: radar (the radar record: headline, areas, sessions, scanned_at, window_days), areas (the
areas that have an id and a title), sessions (an object of the radar's sessions keyed by ID, not
an array: use Object.values(model.sessions) to list them), layout (the layout
record or null), patterns (the workflows the session learned, from the patterns record), width
(root's width in pixels), preview.

kit: el(tag, props, children) and svg(tag, props, children) make elements (props: class, text,
on: { click: fn }, and any attribute); keyed(parent, items, keyOf, build) keeps one element per
key and animates moves; stepButton(area, step) is a next step that runs and shows its progress;
stepCard(area, step) adds its reason, evidence and a way to hide it; sessionCard(session) is a
session link with its live status and journal outcome; openSession(id); openSteps(area) are the
steps sure enough to show (confidence at or above their pattern's bar) that the viewer has not
hidden; laterSteps(area) are those Next up does not show already: draw only these as buttons; sure(step); patternOf(step) is its pattern or null; ordered(areas) puts related areas together, blocked first;
stateColor(state), stateSoft(state), stateLabel(state); ref(path) for data-omnigent-ref, as in
ref('areas/' + area.id); ago(iso); wrap(text, maxChars, maxLines) splits text into lines for
SVG; plain(text) drops Markdown marks; calm is true when the viewer prefers reduced motion;
canAct, canWrite.

Colors: var(name) with name one of text, muted, line, faint, surface, bg, accent, active,
blocked, waiting, done, and the -soft form of the last four; the kit switches them for dark mode.
Next up, Done recently, the scan status, Rescan, the look line and the guide are the kit's: do
not draw them again.
-->`;

const PLAIN_VIEW = `${WHERE_WAS_I_VIEW_START}
<style id="radar-view-style"></style>
<script id="radar-view">Radar.view(Radar.plainView);</script>
${WHERE_WAS_I_VIEW_END}`;

export const WHERE_WAS_I_HTML = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="omnigent-template" content="where-was-i">
<title>Where was I?</title>
<style id="radar-kit-style">${KIT_STYLE}</style>
</head>
<body>
${KIT_BODY}
<script id="radar-kit">${KIT_SCRIPT}</script>
${VIEW_API}
${PLAIN_VIEW}
<script id="radar-start">Radar.start();</script>
</body>
</html>
`;

export const WHERE_WAS_I_GUIDE = [
  "# Where was I?",
  "",
  "What the viewer's recent sessions add up to, and the step that is due next: built from their own sessions, organized by goal, and timed by the workflows they repeat. It is personal: it summarizes the owner's sessions.",
  "",
  "The records are fixed so scans, next steps and their progress always work. The page is yours to design for this viewer's work, so different people see different pages.",
  "",
  "## Collections",
  "",
  "- `radar`: exactly one record, ID `current`, holding the whole picture. Each scan replaces it in one write (`sys_gadget_records` `get` it, then `replace` with its `version`; `create` with `id` `current` when it does not exist yet).",
  "- `patterns`: one record, ID `current`: the workflows you learned from the viewer's history, and how the steps you showed did (Patterns). Written the same way.",
  '- `layout`: one record, ID `current`, describing the page you designed: `{structure, why, revision_id, previous_revision_id, changed_at, change_note, history}`. `structure` is a short phrase ("Rollout stages and review lanes"), `change_note` says what changed in the viewer\'s words ("Added a lane for reviews"), `history` lists earlier looks newest first (`{revision_id, at, note, structure}`, at most 12).',
  "- `step_runs`: one record per next step you worked on, with the next step's ID as the record ID: `{label, area, state: running|done|failed, outcome, session_id, updated}`.",
  "- `dismissed`: next steps the viewer hid, by next step ID: `{at, label, pattern, reason}`, with `reason` `not_relevant` or `already_done` when they said why. The page writes these; you only read them.",
  "- `render_reports`: the page writes one per revision ID, `{state: ok|failed, error, view, at}`, when a design draws or fails. You only read them.",
  "",
  "## The radar record",
  "",
  "- `scanned_at` (ISO 8601 UTC), `window_days`, `sessions_read`, and `headline`: one plain sentence on where the work stands.",
  "- `areas` (up to 12): `{id, title, summary, state, kind, related, items, next_steps}`. `id` is a stable lowercase slug (`[a-z0-9][a-z0-9_-]*`, at most 64). `state` is `active`, `blocked`, `waiting` (on someone or something else) or `done`. `kind` is a word such as rollout, investigation, review, bug, incident or project. `related` lists IDs of areas that depend on or inform this one.",
  "- `items` (up to 8 per area): the threads of work in it, `{id, title, summary, state, session_ids}`.",
  "- `next_steps` (up to 5 per area): `{id, label, kind, why_now, confidence, pattern, pattern_step, nudge, session_id, evidence: {session_id, quote}}`. `kind` is `act` or `monitor`. `id` is a stable slug, unique across the radar, which also names its `step_runs` and `dismissed` records. `confidence` (0 to 1) is your estimate that the viewer clicks it: the page shows a step only at or above its pattern's `bar` (0.7 without one) and never one without `confidence`. `pattern` and `pattern_step` are IDs from the patterns record. `nudge` is `forgotten` (a step they usually skip or do late), `overdue` (past its usual time) or `due`.",
  "- `sessions` (up to 30): `{id, title, area, why, status, outcome, steps}` for each session the radar refers to. Set `why` only on sessions the viewer should monitor or act in. `outcome` is one sentence from its journal or last messages; `steps` the latest plan step statuses from its journal, only when it has one.",
  "",
  "## Patterns",
  "",
  "The `patterns` record is `{updated_at, history_days, sessions_seen, patterns, ledger}`.",
  "",
  "- `patterns` (up to 10): `{id, name, summary, steps, evidence, seen, bar, hits}`. `steps` (up to 8, in order) are `{id, label, after_days, done_rate, forgotten}`: `after_days` is the usual delay after the previous step, `done_rate` how often the viewer did the step when the workflow ran (0 to 1), and `forgotten` is true when they usually skipped it or did it only after a reminder. `evidence` (up to 6) is `{session_id, quote}` from runs of the workflow, and `seen` counts the runs. `hits` is `{shown, clicked, dismissed, ignored}` for its steps; `bar` (0.5 to 0.95) is its calibrated bar.",
  "- `ledger` (up to 40, newest first): one entry per step Next up showed, `{step, pattern, label, shown_at, outcome, reason, resolved_at}`, with `outcome` `open`, `clicked`, `dismissed` or `ignored`. The page shows the hit ratio from it.",
  "",
  "On each scan, before choosing next steps:",
  "",
  "1. Read history lightly: sessions active in the last `history_days` (default 30), at most 60. Read titles and statuses for all, and journals or the last few items only for sessions that look like a step of a workflow. On a rescan read only sessions active since `updated_at`.",
  "2. Find the workflows the viewer repeats: the same ordered steps across two or more efforts, with the gaps between them. Shapes worth looking for, kept only when the history shows them: a change merged behind a feature flag, then ramps, a metric check after each ramp, then removing the flag; a feature shipped, then telemetry or a dashboard, often only after someone asked; a bug fixed, then backported or verified in production; an incident, then its follow-ups. Never add a pattern from general knowledge alone.",
  "3. Update incrementally: add evidence, adjust `after_days`, `done_rate` and `forgotten`, keep IDs stable, and drop patterns not seen within `history_days`. Keep the record under 24 KiB by trimming evidence and old ledger entries first.",
  "",
  "## Next up",
  "",
  "Next up is judged by its hit ratio, the share of shown steps the viewer clicks, not by how many it shows. A wrong step costs more than a missing one, and the best step is one the viewer would otherwise miss.",
  "",
  "1. Place each live effort in a pattern: which steps it has done (check later sessions too, so you never propose something done or under way) and which comes next.",
  "2. Propose a step only when it is due now: the previous step is done, its `after_days` has passed, and nothing shows it done or started. Prefer steps the viewer tends to forget and due follow-ups they have not started. A specific, evidenced step outside any pattern, such as a review someone waits on, can be shown too, rated the same way.",
  "3. Estimate `confidence`: how sure you are of the effort's place and that the step is due, times the pattern's hit ratio (`clicked / (clicked + dismissed + ignored)`; about 0.8 with fewer than five resolved). Lower it after a `not_relevant` dismissal of a similar step.",
  "4. Write only steps at or above the bar. No step is the right answer when nothing is due; never fill the list.",
  '5. `why_now` is one sentence on why now, from the evidence ("Two quiet days at 5%; you ramp after two."). Cite the session and a short quote in `evidence`.',
  "6. Keep the ledger: mark each `open` entry `clicked` when a `do_next_step` action or `step_runs` record exists for its step, `dismissed` with its `reason` when it is in `dismissed`, and `ignored` once it is seven days old or you dropped it unclicked, with `resolved_at`. Add an `open` entry for each new step you show, and update each pattern's `hits`.",
  "7. Calibrate: once a pattern has five resolved steps, raise its `bar` by 0.05 (to at most 0.9) while its hit ratio is under 0.6, and lower it by 0.05 (to at least 0.6) while it is over 0.85. `already_done` means you missed evidence: read the effort's later sessions before proposing its steps again.",
  "",
  "## Scan",
  "",
  "The Scan button requests the `scan` action with `window_days`. A chat request to scan, or an automation's prompt, works the same way.",
  "",
  "1. Read the `layout` record, and the `render_reports` record whose ID is the published revision from `sys_gadget_get`. If it says `failed`, restore the previous look first (Restoring). Do not read the page's HTML on a scan unless you are about to change the page.",
  "2. List sessions with `sys_session_list` (visibility `mine`). Each row has `last_activity_at` in Unix seconds: keep the sessions active within `window_days` of now, at most 25, most recently active first. Rows are ordered by creation, so page on with `cursor` until rows were created before the window. Skip this gadget session, sessions you started for this gadget's next steps, and trivial ones.",
  "3. For each kept session read only a little: its title and status, then its journal with `sys_journal_read` and its `conversation_id` when that tool is available, else `sys_session_get_history` limited to its last few items. Journal tools are optional; never ask a session to start a journal, and never read whole transcripts. On a rescan, read again only the sessions active since the radar's `scanned_at` and keep what the radar already says about the others.",
  "4. Group by goal: several sessions about one rollout are one area; one session can feed several areas. Name each area by what the viewer is trying to get done.",
  "5. Update the patterns record and choose next steps (Patterns, Next up). Leave out steps in `dismissed`; drop steps whose `step_runs` say done, and give the pattern's following step a new ID when it becomes due.",
  "6. On a rescan keep the IDs of areas, items and next steps that are still the same thing, so the page moves them instead of redrawing, and mark finished work `done` rather than removing it at once.",
  "7. Replace the radar and patterns records, each in one write; there is no need to read them back.",
  "8. With no `layout` record yet, design the first page now (Designing the page). Otherwise keep the page unless the work no longer fits it (Changing the page).",
  '9. Report the action `done` with one line on what is due, without counts ("Checkout is ready for 10%; nothing else is due").',
  "",
  "The first scan takes one to three minutes; the page shows its progress meanwhile.",
  "",
  "## Designing the page",
  "",
  "Show what the work adds up to and what needs the viewer, not a list of sessions. Choose the structure the work has: a rollout as stages, an investigation as a dependency graph, a review queue as lanes, many small fixes as a timeline. These are examples, not a menu; mix structures when areas differ.",
  "",
  "- Draw from `model` only; never write data into the HTML, so a scan updates the page without a new revision.",
  "- Make it glanceable both in the docked side panel (about 400 px wide) and full screen. Use the kit's colors so light and dark both work, respect `kit.calm` (the kit also stops CSS animation for reduced motion), and use inline SVG and CSS only: the frame blocks the network, so no external fonts, images or scripts. Keep the HTML well under 1 MiB.",
  "- Include at least one visual element that carries meaning: an SVG of the rollout's stages with where it stands, a graph of what blocks what, lanes by state, a timeline of fixes. Cards and text alone are the plain view's job.",
  "- Animate changes in place with `kit.keyed` and stable IDs, and scope your CSS to your own classes.",
  "- Next up, Done recently, what the session learned, the hit ratio, the scan status, Rescan, the look line and the guide are the kit's: do not draw them again. Next up owns its steps, so never repeat them: show where the other steps sit instead (a marker on a stage), only steps from `kit.laterSteps(area)`, with `kit.stepButton` or `kit.stepCard`, and use `kit.sessionCard` for sessions, so their actions and progress keep working. `kit.plain(text)` drops Markdown marks from text the sessions wrote.",
  "",
  "A small example of a stage track that moves its marker when the radar changes:",
  "",
  "```js",
  "Radar.view({ name: 'Rollout stages', render: function (root, model, kit) {",
  "  var stages = ['1%', '5%', '10%', '50%', '100%'];",
  "  var at = 2; // where the rollout stands, read from its area",
  "  var track = root.querySelector('svg.stages') || root.appendChild(kit.svg('svg', { class: 'stages', viewBox: '0 0 400 40', role: 'img' }));",
  "  track.setAttribute('aria-label', 'Rollout at ' + stages[at]);",
  "  // keyed keeps one element per key: the 'now' ring slides to its new stage on each change.",
  "  kit.keyed(track, stages.concat(['now']), function (key) { return key; }, function (key) {",
  "    if (key === 'now') return kit.svg('circle', { r: 10, cy: 20, cx: 20 + at * 90, fill: 'none', stroke: 'var(--accent)', 'stroke-width': 2 });",
  "    var index = stages.indexOf(key);",
  "    return kit.svg('circle', { r: 6, cy: 20, cx: 20 + index * 90, fill: index <= at ? 'var(--active)' : 'var(--line)' });",
  "  });",
  "} });",
  "```",
  "",
  "To publish a design:",
  "",
  "1. Write one `<style>` and one `<script>` calling `Radar.view({name, render})`. The comment just above the view region in the page documents `model` and `kit`; read it once with `sys_gadget_revision_get` before your first design.",
  '2. `sys_gadget_revision_create` with `base_revision_id` the published revision and one edit, `{start: "<!-- radar:view:start -->", end: "<!-- radar:view:end -->", new: <your style and script>}`. Do not send `html`: the edit changes only the view region, and the rest (kit, capabilities, collections, guide, rules, actions) is carried over unchanged.',
  "3. If it is refused, the error names the edit and why; fix that and send it again.",
  "4. `sys_gadget_publish` it with `if_version` from `sys_gadget_get`.",
  "5. Write the `layout` record: `structure`, `why`, `revision_id` (the new one), `previous_revision_id` (the one it replaced), `changed_at`, `change_note`, and `history` with the replaced look added.",
  "6. Check that it drew: read `render_reports` for the new revision ID a few times over about a minute. On `failed`, restore at once (Restoring) and fix the design before you try again. No report means nobody has the page open; the next scan checks.",
  "",
  "## Changing the page",
  "",
  "Publish a new design only when the work no longer fits the current structure (a new kind of work, an area that keeps overflowing) or the viewer asks. Change gradually: keep what works, change one part, keep IDs and positions stable, and say what changed in `change_note`. Never redesign from scratch on a routine rescan.",
  "",
  "## Restoring",
  "",
  'Back to the previous look requests `restore_look`, with `revision_id` when the page knows it. Publish that revision (or `layout.previous_revision_id`, or the newest `history` entry that drew) with `if_version`, then update `layout`: `revision_id` the restored one, `previous_revision_id` the one you left, `change_note` "Back to the previous look", and the left look added to `history`. Do the same on your own when `render_reports` says the published design failed, then report what happened in the session.',
  "",
  "## Next steps",
  "",
  "A click on a next step requests `do_next_step` with `next_step` (its ID), `label`, `area` and, when it has one, `pattern`. Read the step from the radar, then:",
  "",
  "1. Create or replace its `step_runs` record with `state` `running`.",
  "2. Carry it out. When the step names a session (`session_id` or `evidence.session_id`) and the work belongs there, message that session with `sys_session_message`; otherwise do it yourself or start a session with `sys_session_create`. Record the session you used in `session_id`.",
  "3. For a `monitor` step, check and report what you found rather than changing anything.",
  "4. Set the run `done` or `failed` with a one-sentence `outcome`, update the matching area or item in the radar when the step changed it, and report the action. The next scan counts the click in the ledger and the step as done in its pattern.",
  "",
  "Ask in the session before anything with a wide effect, such as ramping a flag in production or posting to other people.",
  "",
  "## Customizing",
  "",
  'People ask the gadget session to change the page ("show my reviews as lanes"), the scan ("scan 7 days by default", "leave out the docs sessions"), how work is grouped, or how sure Next up must be ("only show what is really due"). Change the page as Changing the page says and the scan rules or bars in this guide; keep the collections, since a published collection\'s schema cannot change.',
].join("\n");

export const WHERE_WAS_I_REVISION: CreateGadgetRevisionRequest = {
  html: WHERE_WAS_I_HTML,
  sdk_version: 1,
  capabilities: ["data.read", "data.write", "sessions.read"],
  collections: {
    [WHERE_WAS_I_COLLECTION]: WHERE_WAS_I_SCHEMA,
    [WHERE_WAS_I_LAYOUT_COLLECTION]: WHERE_WAS_I_LAYOUT_SCHEMA,
    step_runs: WHERE_WAS_I_STEP_RUN_SCHEMA,
    dismissed: WHERE_WAS_I_DISMISSED_SCHEMA,
    [WHERE_WAS_I_REPORTS_COLLECTION]: WHERE_WAS_I_REPORT_SCHEMA,
    [WHERE_WAS_I_PATTERNS_COLLECTION]: WHERE_WAS_I_PATTERNS_SCHEMA,
  },
  guide: WHERE_WAS_I_GUIDE,
  constraints: [
    {
      name: "one_radar",
      kind: "max_records",
      collection: WHERE_WAS_I_COLLECTION,
      max: 1,
      message: "The radar is one record with ID current: replace it instead of creating another.",
    },
    {
      name: "one_layout",
      kind: "max_records",
      collection: WHERE_WAS_I_LAYOUT_COLLECTION,
      max: 1,
      message: "The layout is one record with ID current: replace it instead of creating another.",
    },
    {
      name: "one_patterns",
      kind: "max_records",
      collection: WHERE_WAS_I_PATTERNS_COLLECTION,
      max: 1,
      message:
        "The patterns are one record with ID current: replace it instead of creating another.",
    },
  ],
  actions: [
    {
      name: "scan",
      description:
        "Rebuild the radar from the viewer's recent sessions as the guide's Scan section says: check the published " +
        "design's render report first, list their sessions active in the last params.window_days days (default 3, " +
        "at most 25; last_activity_at in the list), read each one's journal when journal tools are available and " +
        "only its last few messages, and group them by goal into areas. Update the patterns record from a light read " +
        "of up to 30 days of history (the guide's Patterns), propose only next steps that are due in a learned " +
        "pattern and likely to be clicked (Next up), update the ledger of shown steps, and replace the radar record " +
        "with ID current in one write. With no layout record yet, also design and publish the first page as the " +
        "guide's Designing the page section says.",
      params: {
        type: "object",
        additionalProperties: false,
        properties: { window_days: { type: "integer", minimum: 1, maximum: 30 } },
      },
    },
    {
      name: "do_next_step",
      description:
        "Do or check the next step params.next_step from the radar (params.pattern is its pattern), as the guide's Next steps section says: track " +
        "it in its step_runs record (running, then done or failed with an outcome), work in the session it came " +
        "from when that is where it belongs, and update the radar's area when the step changed it.",
      params: {
        type: "object",
        additionalProperties: false,
        required: ["next_step"],
        properties: {
          next_step: { type: "string", minLength: 1, maxLength: 64 },
          label: { type: "string", maxLength: 90 },
          area: { type: "string", maxLength: 64 },
          pattern: { type: "string", maxLength: 64 },
        },
      },
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
