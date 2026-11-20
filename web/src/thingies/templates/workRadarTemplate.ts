// Work radar template: what the viewer's recent sessions add up to, and what needs them next.
// The thingy session writes fixed records (the whole picture as one `radar` record per scan) and
// designs the page itself to fit the viewer's work, so different people see different pages.
//
// The page is a kit plus a view. The kit is everything a design keeps: the scan status, Rescan,
// Next up, Done recently, the look line with Back to the previous look, the guide, a plain view,
// and the fallback to that plain view when a design fails to draw. The view sits between the
// VIEW_START and VIEW_END markers; the session replaces only that region with its own design,
// which calls `Radar.view({ render })`. The template's own view is the plain one.
//
// The scripts are plain ES2018 inside template literals: keep them free of backticks, `${`, and
// backslashes.

import type { CreateThingyRevisionRequest } from "../api/thingyApi";
import {
  THINGY_KIT_HELPERS,
  THINGY_KIT_STYLE,
  THINGY_LAYOUT_SCHEMA,
  THINGY_RENDER_REPORT_SCHEMA,
} from "./templateKit";

export const WORK_RADAR_COLLECTION = "radar";
// The one record in `radar` and in `layout`; each scan or design replaces it.
export const WORK_RADAR_RECORD_ID = "current";
export const WORK_RADAR_LAYOUT_COLLECTION = "layout";
export const WORK_RADAR_REPORTS_COLLECTION = "render_reports";
export const WORK_RADAR_VIEW_START = "<!-- radar:view:start -->";
export const WORK_RADAR_VIEW_END = "<!-- radar:view:end -->";

const AREA_STATES = ["active", "blocked", "waiting", "done"];
const STEP_STATUSES = ["started", "done", "needs_input", "failed"];

const id = (max: number) => ({ type: "string", minLength: 1, maxLength: max });
const text = (max: number) => ({ type: "string", maxLength: max });
const time = { type: "string", minLength: 10, maxLength: 40 };

export const WORK_RADAR_SCHEMA = {
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
                kind: { type: "string", enum: ["act", "monitor"] },
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

export const WORK_RADAR_LAYOUT_SCHEMA = THINGY_LAYOUT_SCHEMA;
export const WORK_RADAR_REPORT_SCHEMA = THINGY_RENDER_REPORT_SCHEMA;

export const WORK_RADAR_STEP_RUN_SCHEMA = {
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

export const WORK_RADAR_DISMISSED_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["at"],
  properties: { at: time, label: text(90) },
} satisfies Record<string, unknown>;

const KIT_STYLE = `
${THINGY_KIT_STYLE}
/* Next up */
.next-list { display: flex; flex-wrap: wrap; gap: 8px; }
.next-chip { display: inline-flex; align-items: center; gap: 8px; max-width: 100%; text-align: left; border: 1px solid var(--line); background: var(--surface); border-radius: 999px; padding: 6px 12px 6px 8px; cursor: pointer; }
.next-chip:hover:not(:disabled) { border-color: var(--accent); }
.next-chip:disabled { cursor: default; opacity: 0.7; }
.next-chip .where { color: var(--muted); font-size: 12px; }

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
${THINGY_KIT_HELPERS}
  var OPEN = ['pending', 'queued', 'running', 'needs_input'];
  var PENDING_MS = 10 * 60 * 1000;
  var STATE_LABELS = { active: 'Active', blocked: 'Blocked', waiting: 'Waiting', done: 'Done' };
  var STATE_ORDER = { blocked: 0, active: 1, waiting: 2, done: 3 };
  var SCAN_TEXT = {
    pending: 'Start your thingy session in New session beside this page, and it scans.',
    queued: 'Waiting for your thingy session to pick it up…',
    running: 'Reading your recent sessions…',
    needs_input: 'Your thingy session is waiting for you.',
    done: 'Scan done',
    failed: 'Scan failed',
    cancelled: 'Scan cancelled',
    finished_unreported: 'Your thingy session stopped before it reported the scan. Click Rescan to ask again.',
    interrupted: 'The scan stopped before it finished. Click Rescan to try again.',
  };
  var STEP_TEXT = {
    pending: 'Start your thingy session in New session beside this page, and it runs this.',
    queued: 'Waiting for your thingy session to pick it up…',
    running: 'Working on it…',
    needs_input: 'Waiting for you in the session',
    done: 'Done',
    failed: 'Did not work',
    cancelled: 'Cancelled',
    finished_unreported: 'Stopped before it reported',
    interrupted: 'Stopped before it finished',
  };
  var LIVE_TEXT = { running: 'Running', needs_input: 'Needs your input', waiting: 'Waiting', failed: 'Failed', idle: 'Idle', done: 'Finished' };

  var client = null;
  var preview = false;
  var canWrite = false;
  var canAct = false;
  var radarCollection = null;
  var layoutCollection = null;
  var stepRuns = null;
  var dismissed = null;
  var reports = null;
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
  // switched off, and the page reports it in render_reports for the thingy session.
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
  function openSteps(area) {
    return (area.next_steps || []).filter(function (step) { return step && step.id && !dismissedDocs[step.id]; });
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
    pendingScan = { at: Date.now(), text: 'Sending to your thingy session…' };
    schedule();
    client.agent.request('scan', { window_days: windowDays() }).then(function (receipt) {
      var starting = Boolean(receipt && receipt.delivery === 'starting');
      pendingScan = { at: Date.now(), actionId: receipt && receipt.actionId, starting: starting, text: starting ? SCAN_TEXT.pending : 'Sent to your thingy session.' };
      schedule();
    }, function (error) {
      pendingScan = null;
      notice('Could not send the scan to your thingy session: ' + message(error), 'error');
      schedule();
    });
  }

  function requestStep(area, step) {
    if (!canAct) return;
    var pending = { at: Date.now(), text: 'Sending to your thingy session…' };
    pendingSteps[step.id] = pending;
    // Running it again starts a new finish to show.
    delete freshDone[step.id];
    schedule();
    client.agent.request('do_next_step', { next_step: step.id, label: step.label, area: area.id }).then(function (receipt) {
      if (pendingSteps[step.id] !== pending) return;
      pending.actionId = receipt && receipt.actionId;
      pending.starting = Boolean(receipt && receipt.delivery === 'starting');
      pending.text = pending.starting ? STEP_TEXT.pending : 'Sent to your thingy session.';
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
    var done = existing
      ? dismissed.delete(step.id, { ifVersion: existing.version }).then(function () { delete dismissedDocs[step.id]; })
      : dismissed.create({ at: new Date().toISOString(), label: step.label }, { id: step.id }).then(function (doc) { dismissedDocs[step.id] = doc; });
    done.then(function () {
      delete dismissing[step.id];
      notice(existing ? '' : 'Hidden “' + step.label + '”. Your thingy session leaves it out of the next scan too.');
      schedule();
    }, function (error) {
      delete dismissing[step.id];
      notice('Could not ' + (existing ? 'restore' : 'hide') + ' “' + step.label + '”: ' + message(error), 'error');
      schedule();
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
        ? 'Live status is off: this thingy was not allowed to see your sessions.'
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
  function stepButton(area, step) {
    var state = stepState(step);
    var busy = isBusy(state);
    return el('button', {
      type: 'button', class: 'next-chip', 'data-omnigent-requires': 'action', 'data-run': state.run, disabled: !canAct || busy,
      title: state.run === 'idle' ? (step.kind === 'monitor' ? 'Ask your thingy session to check on this' : 'Ask your thingy session to do this') : state.text,
      'data-omnigent-ref': ref('steps/' + step.id), 'data-omnigent-label': step.label,
      on: { click: function () { requestStep(area, step); } },
    }, [
      busy ? el('span', { class: 'spinner', 'aria-hidden': 'true' }) : kindIcon(step.kind),
      el('span', { text: step.label }),
      el('span', { class: 'where', text: busy ? state.text : area.title }),
    ]);
  }

  // A next step with its reason, evidence, progress and a way to hide it.
  function stepCard(area, step) {
    var state = stepState(step);
    var busy = isBusy(state);
    var main = el('button', {
      type: 'button', class: 'step-main', 'data-omnigent-requires': 'action', disabled: !canAct || busy,
      title: step.kind === 'monitor' ? 'Ask your thingy session to check on this' : 'Ask your thingy session to do this',
      on: { click: function () { requestStep(area, step); } },
    }, [kindIcon(step.kind), el('span', { class: 'step-label', text: step.label })]);
    var why = [];
    if (step.why) why.push(step.why + ' ');
    var cited = (step.evidence && step.evidence.session_id) || step.session_id;
    if (cited) {
      why.push('From ');
      why.push(el('button', { type: 'button', class: 'cite', text: sessionTitle(cited), on: { click: function () { openSession(cited); } } }));
      if (step.evidence && step.evidence.quote) why.push(el('span', { class: 'quote', text: ': “' + plain(step.evidence.quote) + '”' }));
    }
    var children = [main];
    if (why.length) children.push(el('div', { class: 'step-why' }, why));
    if (state.run !== 'idle') {
      var line = [state.spin ? el('span', { class: 'spinner', 'aria-hidden': 'true' }) : null, el('span', { text: state.text })];
      if (state.session) line.push(el('button', { type: 'button', class: 'link-button', text: 'Open session', on: { click: function () { openSession(state.session); } } }));
      if ((state.again || state.retry) && canAct) line.push(el('button', { type: 'button', class: 'link-button', 'data-omnigent-requires': 'action', text: state.retry ? 'Send again' : 'Do it again', on: { click: function () { requestStep(area, step); } } }));
      children.push(el('div', { class: 'step-run', 'aria-live': 'polite' }, line));
    }
    if (canWrite) {
      children.push(el('button', { type: 'button', class: 'step-x', 'data-omnigent-requires': 'write', 'aria-label': 'Hide this next step', title: 'Hide this next step', disabled: Boolean(dismissing[step.id]), text: '×', on: { click: function () { dismiss(step); } } }));
    }
    return el('div', { class: 'step', 'data-kind': step.kind, 'data-run': state.run, 'data-omnigent-ref': ref('steps/' + step.id), 'data-omnigent-label': step.label }, children);
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
    el: el, svg: svg, keyed: keyed, ago: ago, hash: hash, wrap: wrap, plain: plain,
    stateColor: stateColor, stateSoft: stateSoft, stateLabel: stateLabel, stateOrder: STATE_ORDER,
    openSteps: openSteps, ordered: ordered, stepState: stepState, requestStep: requestStep,
    stepButton: stepButton, stepCard: stepCard, sessionCard: sessionCard, sessionTitle: sessionTitle,
    openSession: openSession, liveState: liveState, ref: ref, rerender: function () { schedule(); },
    calm: calm, canAct: false, canWrite: false, preview: false,
  };
  function syncKit() { kit.canAct = canAct; kit.canWrite = canWrite; kit.preview = preview; }

  // ---- The plain view: before the first design, and whenever a design fails to draw

  function areaCard(area, model) {
    var steps = openSteps(area);
    var hidden = (area.next_steps || []).length - steps.length;
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
      hidden > 0 ? el('div', { class: 'dismissed-note' }, [
        hidden + (hidden === 1 ? ' next step hidden. ' : ' next steps hidden. '),
        canWrite ? el('button', { type: 'button', class: 'link-button', 'data-omnigent-requires': 'write', text: 'Show again', on: { click: function () {
          (area.next_steps || []).forEach(function (step) { if (dismissedDocs[step.id]) dismiss(step); });
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

  function notice(textValue, kind) {
    var box = $('notice');
    box.hidden = !textValue;
    box.textContent = textValue || '';
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
    renderNextUp(areas());
    drawView();
    renderDone();
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
      button.textContent = open ? 'Scanning…' : undelivered ? 'Start my thingy session' : button.id === 'scan' ? 'Rescan' : 'Scan my recent work';
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
      parts.push(el('span', { text: 'After its first scan, your thingy session designs this page around your work.' }));
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

  // The most useful next steps first: blocked work, then things to do, then things to watch.
  function renderNextUp(list) {
    var picks = [];
    list.forEach(function (area) {
      openSteps(area).forEach(function (step, position) {
        var state = stepState(step);
        // One chip per step: a step that just finished shows only as its finished chip.
        if (state.run === 'done' || isFresh(step.id)) return;
        picks.push({ area: area, step: step, score: STATE_ORDER[stateKey(area.state)] * 10 + (step.kind === 'act' ? 0 : 5) + position });
      });
    });
    picks.sort(function (a, b) { return a.score - b.score; });
    picks = picks.slice(0, 4);
    var finished = Object.keys(freshDone).filter(isFresh).map(function (stepId) {
      var done = freshDone[stepId];
      return el('div', { class: 'next-chip finished', 'data-state': done.state, role: 'status' }, [
        el('span', { class: 'mark', 'aria-hidden': 'true', text: done.state === 'done' ? '✓' : '!' }),
        el('span', { text: done.label || 'Next step' }),
        el('span', { class: 'where', text: done.state === 'done' ? 'Done' : 'Did not work' }),
      ]);
    });
    $('next-section').hidden = picks.length === 0 && finished.length === 0;
    $('next-list').replaceChildren.apply($('next-list'), finished.concat(picks.map(function (pick) { return stepButton(pick.area, pick.step); })));
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
          next_steps: [{ id: 'ramp-10', label: 'Ramp the flag to 10%', kind: 'act', why: 'Errors stayed flat for two days at 5%.', evidence: { session_id: 'example-1', quote: 'no change in 5xx after the ramp' } },
            { id: 'watch-p95', label: 'Check p95 latency after the ramp', kind: 'monitor', why: 'The last ramp moved p95 by 8 ms.' }] },
        { id: 'latency', title: 'Search latency investigation', state: 'waiting', kind: 'investigation', related: ['checkout-rollout'],
          summary: 'Slow requests trace to a cold cache after deploys; a fix is in review.',
          items: [{ id: 'trace', title: 'Traced the slow path', state: 'done' }, { id: 'fix', title: 'Cache warm-up fix', state: 'waiting' }],
          next_steps: [{ id: 'latency-verify', label: 'Verify p90 once the fix ships', kind: 'monitor' }] },
        { id: 'reviews', title: 'Pull requests to review', state: 'blocked', kind: 'review',
          summary: 'Two pull requests are waiting on your review; one blocks the cache fix.',
          items: [{ id: 'pr-cache', title: 'Cache warm-up fix', state: 'blocked' }, { id: 'pr-docs', title: 'Docs for the new flag', state: 'waiting' }],
          next_steps: [{ id: 'review-cache', label: 'Review the cache warm-up fix', kind: 'act', why: 'It blocks the latency fix.' }] },
      ],
      sessions: [
        { id: 'example-1', title: 'Ramp checkout flag', area: 'checkout-rollout', why: 'Watch errors after the next ramp', status: 'idle', outcome: 'Ramped to 5%; error rate unchanged.', steps: [{ step: 'ramp', status: 'done' }, { step: 'verify', status: 'done' }, { step: 'next-ramp', status: 'started' }] },
      ],
    }, { id: 'current' }).then(function (doc) {
      // Show it now rather than at the next poll.
      if (!radarDoc) { radarDoc = doc; schedule(); }
    }, function () { return undefined; });
  }

  // ---- Wiring

  function stamp(items) {
    return (items || []).map(function (doc) { return doc.id + '@' + doc.version; }).join('|');
  }
  function byIdOf(items) {
    var out = {};
    (items || []).forEach(function (doc) { out[doc.id] = doc; });
    return out;
  }
  function onRadar(snapshot) {
    if (snapshot.status === 'loading') return;
    // A newer revision means the session published a look; Omnigent loads it in a moment.
    if (snapshot.status === 'stale' && snapshot.error && snapshot.error.code === 'REVISION_CHANGED') notice('Loading the new look…');
    else if (snapshot.status === 'stale' && snapshot.error) notice('Could not load the radar: ' + message(snapshot.error), 'error');
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
  function onRuns(snapshot) {
    if (snapshot.status === 'loading') return;
    var key = stamp(snapshot.items);
    if (key === seen.runs) return;
    seen.runs = key;
    var next = byIdOf(snapshot.items);
    Object.keys(next).forEach(function (stepId) {
      var before = runDocs[stepId] && runDocs[stepId].value;
      var now = next[stepId].value;
      if (before && before.state === 'running' && now && (now.state === 'done' || now.state === 'failed')) markFresh(stepId, now.state, now.label);
    });
    runDocs = next;
    schedule();
  }
  function onDismissed(snapshot) {
    if (snapshot.status === 'loading') return;
    var key = stamp(snapshot.items);
    if (key === seen.dismissed) return;
    seen.dismissed = key;
    dismissedDocs = byIdOf(snapshot.items);
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
      automate('Refresh ' + name, 'Load the thingy skill and follow it. Refresh the thingy "' + name + '" (thingy_id ' + client.context.app.id + '): scan my sessions from the last ' + windowDays() + ' days as its guide describes under Scan.', 'FREQ=DAILY;BYHOUR=8;BYMINUTE=0');
    });
    $('automate').addEventListener('click', function () {
      var name = client.context.app.name;
      automate('Next step from ' + name, 'Load the thingy skill and follow it. In the thingy "' + name + '" (thingy_id ' + client.context.app.id + '), do this next step as its guide describes under Next steps: (say which step, for example: check the error rate after the latest ramp).', 'FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9;BYMINUTE=0');
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
      if (!preview && !client.can('action')) notice('You can look at this page. Scanning and next steps run in the owner’s thingy session.');
      radarCollection = client.data.collection('radar');
      layoutCollection = client.data.collection('layout');
      stepRuns = client.data.collection('step_runs');
      dismissed = client.data.collection('dismissed');
      reports = client.data.collection('render_reports');
      if (viewFailure) report('failed', viewFailure.text);
      radarCollection.watch(onRadar, { intervalMs: 4000, limit: 5 });
      layoutCollection.watch(onLayout, { intervalMs: 8000, limit: 5 });
      stepRuns.watch(onRuns, { intervalMs: 4000, limit: 100 });
      dismissed.watch(onDismissed, { intervalMs: 8000, limit: 100 });
      if (canAct && typeof client.agent.watch === 'function') client.agent.watch(onActions, { intervalMs: 3000 });
      schedule();
    }, function (error) {
      $('loading').hidden = true;
      notice('The thingy could not connect to Omnigent: ' + message(error), 'error');
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
      <p class="kicker">Work radar <span class="badge" title="Built from the owner's own sessions. Anyone you share this thingy with sees these summaries.">Personal</span></p>
      <h1 id="title">Work radar</h1>
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
      <p>Your thingy session reads the sessions you worked in recently and works out what they add up to: the efforts you are driving, what is blocked or waiting on you, and the obvious next steps, such as ramping a flag, checking a metric or following up on a bug.</p>
      <p>Then it designs this page around your work, a rollout as stages or reviews as lanes, and keeps it up to date as the work moves. Click a next step and your thingy session does it or checks on it.</p>
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
      <div id="next-list" class="next-list"></div>
    </section>
    <div id="view" class="view"></div>
    <section id="done-section" hidden>
      <h2>Done recently</h2>
      <ul id="done-list" class="done-list"></ul>
    </section>
  </div>

  <section>
    <details id="guide" class="guide">
      <summary>How this works, and how to make it yours</summary>
      <div class="guide-grid">
        <div>
          <h3>How this works</h3>
          <div class="layers">
            <div class="layer"><svg viewBox="0 0 26 26" aria-hidden="true"><circle cx="13" cy="13" r="9" fill="var(--active-soft)" stroke="var(--active)" stroke-width="2"/></svg><div><b>The radar</b> comes from your thingy session. On Scan it reads your recent sessions (their titles, journals and last messages, not whole transcripts), works out what they add up to and what needs you, and writes it down.</div></div>
            <div class="layer"><svg viewBox="0 0 26 26" aria-hidden="true"><rect x="5" y="5" width="16" height="16" rx="3" fill="none" stroke="var(--accent)" stroke-width="2"/><path d="M5 12 H21 M12 12 V21" stroke="var(--accent)" stroke-width="2"/></svg><div><b>The page</b> is designed for your work. After the first scan your thingy session designs it to fit what you do; later scans update it in place. It changes the design only when your work no longer fits, and says what changed. <b>Back to the previous look</b> undoes a change.</div></div>
            <div class="layer"><svg viewBox="0 0 26 26" aria-hidden="true"><rect x="8" y="8" width="10" height="10" rx="2" transform="rotate(45 13 13)" fill="var(--accent)"/></svg><div><b>Next steps</b> go to your thingy session, which does them, hands them to the session they came from, or starts a new one. Each step shows its progress here. Hide the ones you do not need with ×.</div></div>
            <div class="layer"><svg viewBox="0 0 26 26" aria-hidden="true"><circle cx="13" cy="13" r="5" fill="var(--active)"/><circle cx="13" cy="13" r="10" fill="none" stroke="var(--active)" stroke-width="1.5" stroke-dasharray="3 3"/></svg><div><b>Live status</b> is optional. Turn it on and this page shows, without asking the agent, which linked sessions are running or need your input.</div></div>
          </div>
          <h3>Personal</h3>
          <ul><li>This page summarizes your own sessions. Anyone you share this thingy with sees those summaries, so share it only when that is fine.</li></ul>
        </div>
        <div>
          <h3>Make it yours</h3>
          <ul>
            <li>Ask your thingy session, in its chat: <span class="ask">“Show my pull request reviews as lanes.”</span></li>
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
record or null), width (root's width in pixels), preview.

kit: el(tag, props, children) and svg(tag, props, children) make elements (props: class, text,
on: { click: fn }, and any attribute); keyed(parent, items, keyOf, build) keeps one element per
key and animates moves; stepButton(area, step) is a next step that runs and shows its progress;
stepCard(area, step) adds its reason, evidence and a way to hide it; sessionCard(session) is a
session link with its live status and journal outcome; openSession(id); openSteps(area) are the
steps the viewer has not hidden; ordered(areas) puts related areas together, blocked first;
stateColor(state), stateSoft(state), stateLabel(state); ref(path) for data-omnigent-ref, as in
ref('areas/' + area.id); ago(iso); wrap(text, maxChars, maxLines) splits text into lines for
SVG; plain(text) drops Markdown marks; calm is true when the viewer prefers reduced motion;
canAct, canWrite.

Colors: var(name) with name one of text, muted, line, faint, surface, bg, accent, active,
blocked, waiting, done, and the -soft form of the last four; the kit switches them for dark mode.
Next up, Done recently, the scan status, Rescan, the look line and the guide are the kit's: do
not draw them again.
-->`;

const PLAIN_VIEW = `${WORK_RADAR_VIEW_START}
<style id="radar-view-style"></style>
<script id="radar-view">Radar.view(Radar.plainView);</script>
${WORK_RADAR_VIEW_END}`;

export const WORK_RADAR_HTML = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="omnigent-template" content="work-radar">
<title>Work radar</title>
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

export const WORK_RADAR_GUIDE = [
  "# Work radar",
  "",
  "What the viewer's recent sessions add up to, and what needs them next: built from their own sessions and organized by goal, not by session. It is personal: it summarizes the owner's sessions.",
  "",
  "The records are fixed so scans, next steps and their progress always work. The page is yours to design for this viewer's work, so different people see different pages.",
  "",
  "## Collections",
  "",
  "- `radar`: exactly one record, ID `current`, holding the whole picture. Each scan replaces it in one write (`sys_thingy_records` `get` it, then `replace` with its `version`; `create` with `id` `current` when it does not exist yet).",
  '- `layout`: one record, ID `current`, describing the page you designed: `{structure, why, revision_id, previous_revision_id, changed_at, change_note, history}`. `structure` is a short phrase ("Rollout stages and review lanes"), `change_note` says what changed in the viewer\'s words ("Added a lane for reviews"), `history` lists earlier looks newest first (`{revision_id, at, note, structure}`, at most 12).',
  "- `step_runs`: one record per next step you worked on, with the next step's ID as the record ID: `{label, area, state: running|done|failed, outcome, session_id, updated}`.",
  "- `dismissed`: next steps the viewer hid, by next step ID. The page writes these; you only read them.",
  "- `render_reports`: the page writes one per revision ID, `{state: ok|failed, error, view, at}`, when a design draws or fails. You only read them.",
  "",
  "## The radar record",
  "",
  "- `scanned_at` (ISO 8601 UTC), `window_days`, `sessions_read`, and `headline`: one plain sentence on where the work stands.",
  "- `areas` (up to 12): `{id, title, summary, state, kind, related, items, next_steps}`. `id` is a stable lowercase slug (`[a-z0-9][a-z0-9_-]*`, at most 64). `state` is `active`, `blocked`, `waiting` (on someone or something else) or `done`. `kind` is a word such as rollout, investigation, review, bug, incident or project. `related` lists IDs of areas that depend on or inform this one.",
  "- `items` (up to 8 per area): the threads of work in it, `{id, title, summary, state, session_ids}`.",
  "- `next_steps` (up to 5 per area): `{id, label, why, kind, session_id, evidence: {session_id, quote}}`. `kind` is `act` or `monitor`. `id` is a stable slug, unique across the radar, which also names its `step_runs` and `dismissed` records.",
  "- `sessions` (up to 30): `{id, title, area, why, status, outcome, steps}` for each session the radar refers to. Set `why` only on sessions the viewer should monitor or act in. `outcome` is one sentence from its journal or last messages; `steps` the latest plan step statuses from its journal, only when it has one.",
  "",
  "## Scan",
  "",
  "The Scan button requests the `scan` action with `window_days`. A chat request to scan, or an automation's prompt, works the same way.",
  "",
  "1. Read the `layout` record, and the `render_reports` record whose ID is the published revision from `sys_thingy_get`. If it says `failed`, restore the previous look first (Restoring). Do not read the page's HTML on a scan unless you are about to change the page.",
  "2. List sessions with `sys_session_list` (visibility `mine`). Each row has `last_activity_at` in Unix seconds: keep the sessions active within `window_days` of now, at most 25, most recently active first. Rows are ordered by creation, so page on with `cursor` until rows were created before the window. Skip this thingy session, sessions you started for this thingy's next steps, and trivial ones.",
  "3. For each kept session read only a little: its title and status, then its journal with `sys_journal_read` and its `conversation_id` when that tool is available, else `sys_session_get_history` limited to its last few items. Journal tools are optional; never ask a session to start a journal, and never read whole transcripts. On a rescan, read again only the sessions active since the radar's `scanned_at` and keep what the radar already says about the others.",
  "4. Group by goal: several sessions about one rollout are one area; one session can feed several areas. Name each area by what the viewer is trying to get done.",
  "5. Write next steps only when the sessions point to something specific (a flag to ramp after a quiet bake, a metric to check after a change, a review someone waits for, a failed run to retry), citing the session and a short quote in `evidence`. Write none when nothing specific stands out.",
  "6. Leave out next steps in `dismissed`; drop steps whose `step_runs` say done unless the work needs a follow-up, which gets a new step.",
  "7. On a rescan keep the IDs of areas, items and next steps that are still the same thing, so the page moves them instead of redrawing, and mark finished work `done` rather than removing it at once.",
  "8. Replace the radar record in one write; there is no need to read it back.",
  "9. With no `layout` record yet, design the first page now (Designing the page). Otherwise keep the page unless the work no longer fits it (Changing the page).",
  '10. Report the action `done` with a one-line summary ("4 areas, 6 next steps from 14 sessions").',
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
  "- Next up, Done recently, the scan status, Rescan, the look line and the guide are the kit's: do not draw them again, nor a list of every next step. Show where steps sit instead (a marker on a stage), with `kit.stepButton` or `kit.stepCard`, and use `kit.sessionCard` for sessions, so their actions and progress keep working. `kit.plain(text)` drops Markdown marks from text the sessions wrote.",
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
  "1. Write one `<style>` and one `<script>` calling `Radar.view({name, render})`. The comment just above the view region in the page documents `model` and `kit`; read it once with `sys_thingy_revision_get` before your first design.",
  '2. `sys_thingy_revision_create` with `base_revision_id` the published revision and one edit, `{start: "<!-- radar:view:start -->", end: "<!-- radar:view:end -->", new: <your style and script>}`. Do not send `html`: the edit changes only the view region, and the rest (kit, capabilities, collections, guide, rules, actions) is carried over unchanged.',
  "3. If it is refused, the error names the edit and why; fix that and send it again.",
  "4. `sys_thingy_publish` it with `if_version` from `sys_thingy_get`.",
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
  "A click on a next step requests `do_next_step` with `next_step` (its ID), `label` and `area`. Read the step from the radar, then:",
  "",
  "1. Create or replace its `step_runs` record with `state` `running`.",
  "2. Carry it out. When the step names a session (`session_id` or `evidence.session_id`) and the work belongs there, message that session with `sys_session_message`; otherwise do it yourself or start a session with `sys_session_create`. Record the session you used in `session_id`.",
  "3. For a `monitor` step, check and report what you found rather than changing anything.",
  "4. Set the run `done` or `failed` with a one-sentence `outcome`, update the matching area or item in the radar when the step changed it, and report the action.",
  "",
  "Ask in the session before anything with a wide effect, such as ramping a flag in production or posting to other people.",
  "",
  "## Customizing",
  "",
  'People ask the thingy session to change the page ("show my reviews as lanes"), the scan ("scan 7 days by default", "leave out the docs sessions") or how work is grouped. Change the page as Changing the page says and the scan rules in this guide; keep the collections, since a published collection\'s schema cannot change.',
].join("\n");

export const WORK_RADAR_REVISION: CreateThingyRevisionRequest = {
  html: WORK_RADAR_HTML,
  sdk_version: 1,
  capabilities: ["data.read", "data.write", "sessions.read"],
  collections: {
    [WORK_RADAR_COLLECTION]: WORK_RADAR_SCHEMA,
    [WORK_RADAR_LAYOUT_COLLECTION]: WORK_RADAR_LAYOUT_SCHEMA,
    step_runs: WORK_RADAR_STEP_RUN_SCHEMA,
    dismissed: WORK_RADAR_DISMISSED_SCHEMA,
    [WORK_RADAR_REPORTS_COLLECTION]: WORK_RADAR_REPORT_SCHEMA,
  },
  guide: WORK_RADAR_GUIDE,
  constraints: [
    {
      name: "one_radar",
      kind: "max_records",
      collection: WORK_RADAR_COLLECTION,
      max: 1,
      message: "The radar is one record with ID current: replace it instead of creating another.",
    },
    {
      name: "one_layout",
      kind: "max_records",
      collection: WORK_RADAR_LAYOUT_COLLECTION,
      max: 1,
      message: "The layout is one record with ID current: replace it instead of creating another.",
    },
  ],
  actions: [
    {
      name: "scan",
      description:
        "Rebuild the radar from the viewer's recent sessions as the guide's Scan section says: check the published " +
        "design's render report first, list their sessions active in the last params.window_days days (default 3, " +
        "at most 25; last_activity_at in the list), read each one's journal when journal tools are available and " +
        "only its last few messages, group them by goal into areas with threads and specific next steps that cite " +
        "their session, and replace the radar record with ID current in one write. With no layout record yet, also " +
        "design and publish the first page as the guide's Designing the page section says.",
      params: {
        type: "object",
        additionalProperties: false,
        properties: { window_days: { type: "integer", minimum: 1, maximum: 30 } },
      },
    },
    {
      name: "do_next_step",
      description:
        "Do or check the next step params.next_step from the radar, as the guide's Next steps section says: track " +
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
