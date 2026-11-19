// Work map template: a semantic, animated map of what the viewer has been working on, built by
// their thingy session from their recent sessions. The session writes the whole map as one
// `maps` record per scan, so the page never shows half a scan and a scan is one write. Progress
// on a next step lives in `step_runs`, and next steps the viewer hides live in `dismissed`.
//
// A new thingy starts empty: the map comes only from the viewer's own sessions. Only the
// template preview, whose data is in memory, shows an example map.
//
// The script is plain ES2018 inside a template literal: keep it free of backticks, `${`, and
// backslashes.

import type { CreateThingyRevisionRequest } from "../api/thingyApi";

export const WORK_MAP_COLLECTION = "maps";
// The one record in `maps`; each scan replaces it.
export const WORK_MAP_RECORD_ID = "current";

const AREA_STATES = ["active", "blocked", "waiting", "done"];
const STEP_STATUSES = ["started", "done", "needs_input", "failed"];

const id = (max: number) => ({ type: "string", minLength: 1, maxLength: max });
const text = (max: number) => ({ type: "string", maxLength: max });

export const WORK_MAP_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["scanned_at", "window_days", "areas"],
  properties: {
    scanned_at: { type: "string", minLength: 10, maxLength: 40 },
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

export const WORK_MAP_STEP_RUN_SCHEMA = {
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

export const WORK_MAP_DISMISSED_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["at"],
  properties: { at: { type: "string", minLength: 10, maxLength: 40 }, label: text(90) },
} satisfies Record<string, unknown>;

const STYLE = `
:root {
  color-scheme: light;
  --bg: #f7f8fa; --surface: #ffffff; --text: #17191c; --muted: #646a73; --faint: #eceef2; --line: #d8dce3;
  --accent: #4f46e5; --accent-soft: #eceafd; --focus: #2563eb;
  --active: #4f46e5; --active-soft: #e8e7fc; --blocked: #dc2626; --blocked-soft: #fde4e4;
  --waiting: #d97706; --waiting-soft: #fdf0dc; --done: #16a34a; --done-soft: #def5e5;
  --sans: system-ui, -apple-system, 'Segoe UI', sans-serif;
}
body[data-theme='dark'] {
  color-scheme: dark;
  --bg: #121317; --surface: #1b1d22; --text: #eceef2; --muted: #9aa1ac; --faint: #24272e; --line: #343842;
  --accent: #8b84ff; --accent-soft: #2a2850; --focus: #60a5fa;
  --active: #8b84ff; --active-soft: #27264a; --blocked: #f87171; --blocked-soft: #3d2023;
  --waiting: #fbbf24; --waiting-soft: #3a2f17; --done: #4ade80; --done-soft: #1b3324;
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
body { margin: 0; background: var(--bg); color: var(--text); font: 14px/1.5 var(--sans); }
main { max-width: 1040px; margin: 0 auto; padding: 20px 18px 56px; }
h1 { font-size: 24px; line-height: 1.2; margin: 0; letter-spacing: -0.01em; }
h2 { font-size: 15px; margin: 0 0 10px; }
h3 { font-size: 15px; margin: 0; }
button { font: inherit; color: inherit; }
button:focus-visible, select:focus-visible, [tabindex]:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
.top { display: flex; flex-wrap: wrap; gap: 12px 20px; align-items: flex-end; justify-content: space-between; }
.kicker { display: flex; align-items: center; gap: 8px; margin: 0 0 4px; font-size: 12px; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; color: var(--accent); }
.badge { font-size: 11px; font-weight: 600; letter-spacing: 0; text-transform: none; padding: 1px 8px; border-radius: 999px; border: 1px solid var(--line); color: var(--muted); background: var(--surface); }
.meta { margin-top: 4px; font-size: 13px; color: var(--muted); }
.headline { margin: 10px 0 0; font-size: 15px; max-width: 760px; }
.toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.primary { border: 1px solid var(--accent); background: var(--accent); color: #fff; border-radius: 8px; padding: 6px 14px; font-weight: 600; cursor: pointer; }
.primary:disabled { opacity: 0.55; cursor: default; }
.quiet { border: 1px solid var(--line); background: var(--surface); border-radius: 8px; padding: 5px 10px; cursor: pointer; }
.quiet[aria-pressed='true'] { border-color: var(--accent); background: var(--accent-soft); }
.quiet:disabled { opacity: 0.55; cursor: default; }
select { font: inherit; color: inherit; background: var(--surface); border: 1px solid var(--line); border-radius: 8px; padding: 4px 6px; }
.window { display: inline-flex; align-items: center; gap: 6px; color: var(--muted); font-size: 13px; }
.status { display: flex; align-items: center; gap: 8px; min-height: 22px; margin-top: 10px; font-size: 13px; color: var(--muted); }
.spinner { width: 12px; height: 12px; border: 2px solid var(--accent); border-right-color: transparent; border-radius: 50%; animation: spin 0.8s linear infinite; flex: none; }
@keyframes spin { to { transform: rotate(360deg); } }
.notice { margin: 10px 0 0; padding: 9px 12px; border-radius: 8px; background: var(--faint); font-size: 13px; }
.notice[data-kind='error'] { background: var(--blocked-soft); color: var(--blocked); }
section { margin-top: 22px; }

/* Empty state */
.empty { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 24px; align-items: center; padding: 26px; background: var(--surface); border: 1px solid var(--line); border-radius: 16px; }
.empty h2 { font-size: 20px; margin-bottom: 6px; }
.empty p { margin: 0 0 12px; color: var(--muted); }
.forming { width: 100%; height: auto; display: block; }
.forming .f-dot { fill: var(--accent); opacity: 0.85; animation: drift 6s ease-in-out infinite; transform-box: fill-box; transform-origin: center; }
.forming .f-dot.d2 { animation-delay: -1.5s; fill: var(--waiting); }
.forming .f-dot.d3 { animation-delay: -3s; fill: var(--done); }
.forming .f-dot.d4 { animation-delay: -4.5s; fill: var(--blocked); }
.forming .f-line { fill: none; stroke: var(--line); stroke-width: 1.5; stroke-dasharray: 6 6; animation: dash 4s linear infinite; }
.forming .f-ring { fill: none; stroke: var(--accent); stroke-width: 1.5; opacity: 0; animation: ring 3s ease-out infinite; transform-box: fill-box; transform-origin: center; }
@keyframes drift { 0%, 100% { transform: translate(0, 0); } 33% { transform: translate(6px, -5px); } 66% { transform: translate(-5px, 4px); } }
@keyframes dash { to { stroke-dashoffset: -48; } }
@keyframes ring { 0% { opacity: 0.6; transform: scale(0.6); } 100% { opacity: 0; transform: scale(1.6); } }

/* The map */
.map-card { background: var(--surface); border: 1px solid var(--line); border-radius: 16px; padding: 6px 4px 2px; position: relative; overflow: hidden; }
#map svg { display: block; width: 100%; height: auto; }
.legend { display: flex; flex-wrap: wrap; gap: 6px 14px; padding: 6px 12px 10px; font-size: 12px; color: var(--muted); }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.legend i { width: 9px; height: 9px; border-radius: 50%; display: inline-block; }
.node { cursor: pointer; transition: transform 700ms cubic-bezier(0.2, 0.8, 0.2, 1), opacity 500ms ease; }
.node[data-new='true'] { opacity: 0; }
.node:focus { outline: none; }
.node:focus-visible .core { stroke-width: 3; stroke: var(--focus); }
.node .core { stroke-width: 2; transition: r 500ms ease, fill 300ms ease; }
.node .glow { fill: none; stroke-width: 2; opacity: 0; }
.node[data-state='active'] .glow, .node[data-state='blocked'] .glow { animation: ring 2.8s ease-out infinite; transform-box: fill-box; transform-origin: center; }
.node .count { font-size: 18px; font-weight: 700; text-anchor: middle; dominant-baseline: central; pointer-events: none; }
.node .count-label { font-size: 10px; text-anchor: middle; fill: var(--muted); pointer-events: none; }
.node .title { font-size: 13px; font-weight: 600; text-anchor: middle; fill: var(--text); pointer-events: none; }
.node .sub { font-size: 11px; text-anchor: middle; fill: var(--muted); pointer-events: none; }
.sat { stroke: var(--surface); stroke-width: 1.5; }
.sat-step { fill: var(--accent); stroke: var(--surface); stroke-width: 1.5; }
.sat-step[data-open='true'] { animation: blink 2.2s ease-in-out infinite; }
@keyframes blink { 50% { opacity: 0.35; } }
.edge { fill: none; stroke: var(--line); stroke-width: 1.6; transition: stroke 200ms ease, opacity 300ms ease; }
.map-svg[data-focus] .node:not([data-focused='true']) { opacity: 0.45; }
.map-svg[data-focus] .edge { opacity: 0.25; }
.map-svg[data-focus] .edge[data-focused='true'] { opacity: 1; stroke: var(--accent); }
.sweep { pointer-events: none; opacity: 0; transition: opacity 400ms ease; }
.sweep[data-on='true'] { opacity: 1; }
.sweep-arm path { fill: var(--accent); opacity: 0.12; }

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
.kind-icon { flex: none; width: 18px; height: 18px; margin-top: 1px; }
.kind-icon circle, .kind-icon path { fill: none; stroke: var(--accent); stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }
.kind-icon .fill { fill: var(--accent); stroke: none; }
.step-why { margin: 3px 0 0 26px; font-size: 12.5px; color: var(--muted); }
.quote { font-style: italic; }
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
.cite { border: none; background: none; padding: 0; color: var(--accent); cursor: pointer; font-size: inherit; }
.cite:hover { text-decoration: underline; }
.dismissed-note { margin-top: 8px; font-size: 12px; color: var(--muted); }
.link-button { border: none; background: none; padding: 0; color: var(--accent); cursor: pointer; font-size: inherit; }

/* Done recently and the guide */
.done-list { list-style: none; margin: 0; padding: 0; display: grid; gap: 6px; }
.done-list li { display: grid; grid-template-columns: 18px minmax(0, 1fr); gap: 8px; padding: 8px 10px; border-radius: 10px; background: var(--surface); border: 1px solid var(--line); }
.done-list .mark { color: var(--done); font-weight: 700; }
.done-list li[data-state='failed'] .mark { color: var(--blocked); }
.guide { background: var(--surface); border: 1px solid var(--line); border-radius: 14px; padding: 4px 16px; }
.guide summary { cursor: pointer; font-weight: 600; padding: 10px 0; }
.guide-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 8px 28px; padding-bottom: 14px; }
.guide h3 { font-size: 13px; margin: 8px 0 6px; }
.guide ul { margin: 0; padding-left: 18px; color: var(--muted); }
.guide li { margin: 3px 0; }
.guide .ask { color: var(--text); }
.layers { display: grid; gap: 8px; }
.layer { display: grid; grid-template-columns: 26px minmax(0, 1fr); gap: 8px; align-items: start; color: var(--muted); }
.layer b { color: var(--text); }
.layer svg { width: 26px; height: 26px; }
footer { margin-top: 26px; font-size: 12px; color: var(--muted); }
@media (max-width: 640px) {
  main { padding: 14px 12px 40px; }
  .empty { grid-template-columns: minmax(0, 1fr); padding: 18px; }
  .guide-grid { grid-template-columns: minmax(0, 1fr); }
  .areas { grid-template-columns: minmax(0, 1fr); }
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation: none !important; transition: none !important; }
}
`;

const SCRIPT = `
(function () {
  'use strict';
  var SVG_NS = 'http://www.w3.org/2000/svg';
  var OPEN = ['pending', 'queued', 'running', 'needs_input'];
  var PENDING_MS = 10 * 60 * 1000;
  var STATE_LABELS = { active: 'Active', blocked: 'Blocked', waiting: 'Waiting', done: 'Done' };
  var STATE_ORDER = { blocked: 0, active: 1, waiting: 2, done: 3 };
  var SCAN_TEXT = {
    pending: 'Start your thingy session beside this page to scan.',
    queued: 'Waiting for your thingy session…',
    running: 'Reading your recent sessions…',
    needs_input: 'Your thingy session is waiting for you.',
    done: 'Scan done',
    failed: 'Scan failed',
    cancelled: 'Scan cancelled',
    finished_unreported: 'The scan ended without a report. Click Rescan to try again.',
    interrupted: 'The scan stopped before it finished. Click Rescan to try again.',
  };
  var STEP_TEXT = {
    pending: 'Start your thingy session beside this page to run it.',
    queued: 'Waiting for your thingy session…',
    running: 'Working on it…',
    needs_input: 'Waiting for you in the session',
    done: 'Done',
    failed: 'Did not work',
    cancelled: 'Cancelled',
    finished_unreported: 'Ended without a report',
    interrupted: 'Stopped before it finished',
  };
  var LIVE_TEXT = { running: 'Running', needs_input: 'Needs your input', waiting: 'Waiting', failed: 'Failed', idle: 'Idle', done: 'Finished' };

  var client = null;
  var preview = false;
  var canWrite = false;
  var canAct = false;
  var maps = null;
  var stepRuns = null;
  var dismissed = null;
  var mapDoc = null;
  var mapLoaded = false;
  var runDocs = {};
  var dismissedDocs = {};
  var actionItems = [];
  var pendingScan = null;
  var pendingSteps = {};
  var dismissing = {};
  var live = { on: false, timer: null, byId: {}, error: '' };
  var focus = null;
  var nodes = {};
  var mapSvg = null;
  var lastWidth = 0;
  var renderQueued = false;
  var previewSeeded = false;
  var calm = Boolean(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);

  // ---- DOM helpers

  function setProps(node, props) {
    Object.keys(props || {}).forEach(function (key) {
      var value = props[key];
      if (value === null || value === undefined || value === false) return;
      if (key === 'text') node.textContent = value;
      else if (key === 'on') Object.keys(value).forEach(function (type) { node.addEventListener(type, value[type]); });
      else node.setAttribute(key, value === true ? '' : String(value));
    });
  }
  function el(tag, props, children) {
    var node = document.createElement(tag);
    setProps(node, props);
    (children || []).forEach(function (child) {
      if (child !== null && child !== undefined && child !== false) node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
    });
    return node;
  }
  function svg(tag, props, children) {
    var node = document.createElementNS(SVG_NS, tag);
    setProps(node, props);
    (children || []).forEach(function (child) { if (child) node.appendChild(child); });
    return node;
  }
  function $(id) { return document.getElementById(id); }
  // Rotates a group about its own origin, which CSS transforms on SVG do not do reliably.
  function spin(seconds) {
    return calm ? null : svg('animateTransform', { attributeName: 'transform', type: 'rotate', from: '0', to: '360', dur: seconds + 's', repeatCount: 'indefinite' });
  }
  function message(error) { return error && error.message ? error.message : String(error); }

  function ago(iso) {
    var at = Date.parse(iso || '');
    if (isNaN(at)) return '';
    var minutes = Math.round((Date.now() - at) / 60000);
    if (minutes < 1) return 'just now';
    if (minutes < 60) return minutes + ' min ago';
    var hours = Math.round(minutes / 60);
    if (hours < 24) return hours + ' h ago';
    var days = Math.round(hours / 24);
    return days === 1 ? 'yesterday' : days + ' days ago';
  }
  function hash(text) {
    var h = 2166136261;
    for (var i = 0; i < text.length; i += 1) { h ^= text.charCodeAt(i); h = Math.imul(h, 16777619); }
    return (h >>> 0) / 4294967295;
  }
  function stateColor(state) { return 'var(--' + (STATE_LABELS[state] ? state : 'active') + ')'; }
  function stateSoft(state) { return 'var(--' + (STATE_LABELS[state] ? state : 'active') + '-soft)'; }

  // ---- The map record

  function map() { return mapDoc && mapDoc.value && Array.isArray(mapDoc.value.areas) ? mapDoc.value : null; }
  function areas() { var value = map(); return value ? value.areas.filter(function (area) { return area && area.id && area.title; }) : []; }
  function sessionsById() {
    var out = {};
    var value = map();
    ((value && value.sessions) || []).forEach(function (session) { if (session && session.id) out[session.id] = session; });
    return out;
  }
  function openSteps(area) {
    return (area.next_steps || []).filter(function (step) { return step && step.id && !dismissedDocs[step.id]; });
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

  function onActions(snapshot) {
    if (snapshot.status === 'loading') return;
    actionItems = snapshot.items || [];
    schedule();
  }

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

  function dismiss(step, areaId) {
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
    if (areaId) focus = areaId;
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

  // ---- Rendering

  function schedule() {
    if (renderQueued) return;
    renderQueued = true;
    (window.requestAnimationFrame || setTimeout)(function () { renderQueued = false; render(); });
  }

  function notice(text, kind) {
    var box = $('notice');
    box.hidden = !text;
    box.textContent = text || '';
    if (kind) box.setAttribute('data-kind', kind); else box.removeAttribute('data-kind');
  }

  function render() {
    var value = map();
    var list = areas();
    $('loading').hidden = mapLoaded;
    renderHeader(value);
    renderScanStatus(value);
    var empty = mapLoaded && !value;
    $('empty').hidden = !empty;
    $('content').hidden = !value;
    $('guide').open = $('guide').open || empty;
    if (!value) return;
    $('nothing').hidden = list.length > 0;
    $('map-section').hidden = list.length === 0;
    renderMap(list);
    renderNextUp(list);
    renderAreas(list);
    renderDone();
  }

  function renderHeader(value) {
    var parts = [];
    if (value) {
      if (value.example) parts.push('Example map');
      else if (value.scanned_at) parts.push('Scanned ' + ago(value.scanned_at));
      parts.push('last ' + value.window_days + (value.window_days === 1 ? ' day' : ' days'));
      if (typeof value.sessions_read === 'number') parts.push(value.sessions_read + (value.sessions_read === 1 ? ' session read' : ' sessions read'));
    }
    $('meta').textContent = parts.join(' · ');
    $('headline').textContent = (value && value.headline) || '';
    $('headline').hidden = !(value && value.headline);
    $('scan').hidden = !value;
    var liveButton = $('live');
    liveButton.hidden = !value || preview || !client.sessions || !client.sessions.canList;
    liveButton.setAttribute('aria-pressed', live.on ? 'true' : 'false');
    liveButton.textContent = live.on ? 'Live status on' : 'Show live status';
  }

  function renderScanStatus(value) {
    var box = $('run');
    box.replaceChildren();
    var action = latestAction('scan');
    if (pendingScan && (Date.now() - pendingScan.at > PENDING_MS || (action && pendingScan.actionId === action.id))) pendingScan = null;
    var open = Boolean(pendingScan && !pendingScan.starting) || (isOpen(action) && action.status !== 'pending');
    var undelivered = Boolean(pendingScan && pendingScan.starting) || (!pendingScan && action && action.status === 'pending');
    var label = value ? 'Rescan' : 'Scan my recent work';
    [$('scan'), $('scan-empty')].forEach(function (button) {
      button.disabled = !canAct || (open && !undelivered);
      button.textContent = open ? 'Scanning…' : undelivered ? 'Open the session again' : button.id === 'scan' ? label : 'Scan my recent work';
    });
    if (open) box.appendChild(el('span', { class: 'spinner', 'aria-hidden': 'true' }));
    var textValue = pendingScan ? pendingScan.text : action && SCAN_TEXT[action.status] ? SCAN_TEXT[action.status] + (action.summary && !open ? ': ' + action.summary : '') : '';
    if (textValue) box.appendChild(el('span', { text: textValue }));
    $('empty-run').textContent = !value && textValue ? textValue : '';
    if (mapSvg) mapSvg.sweep.setAttribute('data-on', open ? 'true' : 'false');
  }

  // Related areas sit next to each other: walk the related links from the oldest area first.
  function ordered(list) {
    var byId = {};
    list.forEach(function (area, index) { byId[area.id] = { area: area, index: index }; });
    var seen = {};
    var out = [];
    function visit(area) {
      if (seen[area.id]) return;
      seen[area.id] = true;
      out.push(area);
      (area.related || []).forEach(function (other) { if (byId[other]) visit(byId[other].area); });
    }
    list.forEach(visit);
    return out;
  }

  function wrap(text, maxChars, maxLines) {
    var words = String(text).split(' ');
    var lines = [];
    var line = '';
    for (var i = 0; i < words.length; i += 1) {
      var next = line ? line + ' ' + words[i] : words[i];
      if (next.length <= maxChars || !line) { line = next; continue; }
      lines.push(line);
      line = words[i];
      if (lines.length === maxLines) break;
    }
    if (lines.length < maxLines && line) { lines.push(line); line = ''; }
    var cut = lines.length === maxLines && (line || i < words.length);
    lines = lines.map(function (entry) { return entry.length > maxChars ? entry.slice(0, maxChars - 1) + '…' : entry; });
    if (cut) lines[maxLines - 1] = lines[maxLines - 1].slice(0, Math.max(1, maxChars - 1)) + '…';
    return lines;
  }

  function layout(list, width) {
    var columns = Math.max(1, Math.min(list.length, Math.max(2, Math.min(5, Math.floor(width / 175)))));
    var cell = width / columns;
    var rowHeight = 178;
    var positions = {};
    list.forEach(function (area, index) {
      var col = index % columns;
      var row = Math.floor(index / columns);
      var jitterX = (hash(area.id) - 0.5) * Math.min(26, cell * 0.12);
      var jitterY = (hash(area.id + '-y') - 0.5) * 18;
      var weight = (area.items || []).length + openSteps(area).length;
      positions[area.id] = {
        x: (col + 0.5) * cell + jitterX,
        y: row * rowHeight + 66 + (col % 2 ? 26 : 0) + jitterY,
        r: 26 + 3 * Math.min(8, weight),
        cell: cell,
      };
    });
    var rows = Math.ceil(list.length / columns);
    return { positions: positions, height: rows * rowHeight + (columns > 1 ? 34 : 10) };
  }

  function ensureMapSvg() {
    if (mapSvg) return mapSvg;
    var root = svg('svg', { class: 'map-svg', role: 'group', 'aria-label': 'Map of your work' });
    var defs = svg('defs');
    var edges = svg('g', { class: 'edges' });
    var nodeLayer = svg('g', { class: 'nodes' });
    var sweepArm = svg('g', { class: 'sweep-arm' }, [svg('path', { d: 'M0 0 L900 -260 A930 930 0 0 1 900 260 Z' }), spin(3.2)]);
    var sweep = svg('g', { class: 'sweep', 'data-on': 'false' }, [sweepArm]);
    root.appendChild(defs);
    root.appendChild(sweep);
    root.appendChild(edges);
    root.appendChild(nodeLayer);
    root.addEventListener('click', function (event) { if (event.target === root) setFocus(null); });
    $('map').appendChild(root);
    mapSvg = { root: root, edges: edges, nodes: nodeLayer, sweep: sweep };
    return mapSvg;
  }

  function setFocus(areaId) {
    focus = focus === areaId ? null : areaId;
    schedule();
    if (focus) {
      var card = $('card-' + focus);
      if (card && card.scrollIntoView) card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
  }

  function buildNode(area) {
    var group = svg('g', { class: 'node', tabindex: '0', role: 'button', 'data-new': 'true' });
    group.addEventListener('click', function (event) { event.stopPropagation(); setFocus(group.getAttribute('data-id')); });
    group.addEventListener('keydown', function (event) {
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); setFocus(group.getAttribute('data-id')); }
    });
    group.parts = {
      tooltip: svg('title'),
      glow: svg('circle', { class: 'glow' }),
      orbit: svg('g', { class: 'orbit' }),
      core: svg('circle', { class: 'core' }),
      count: svg('text', { class: 'count' }),
      countLabel: svg('text', { class: 'count-label' }),
      title: svg('text', { class: 'title' }),
      sub: svg('text', { class: 'sub' }),
    };
    ['tooltip', 'glow', 'orbit', 'core', 'count', 'countLabel', 'title', 'sub'].forEach(function (key) { group.appendChild(group.parts[key]); });
    // Appear where the node will be, then fade in.
    setTimeout(function () { group.removeAttribute('data-new'); }, 30);
    return group;
  }

  function updateNode(group, area, place) {
    var parts = group.parts;
    var steps = openSteps(area);
    var items = area.items || [];
    group.setAttribute('data-id', area.id);
    group.setAttribute('data-state', STATE_LABELS[area.state] ? area.state : 'active');
    group.setAttribute('data-focused', focus === area.id ? 'true' : 'false');
    group.setAttribute('data-omnigent-ref', 'maps/current#areas/' + area.id);
    group.setAttribute('data-omnigent-label', area.title);
    group.setAttribute('aria-label', area.title + ', ' + (STATE_LABELS[area.state] || 'Active') + ', ' + steps.length + ' next steps');
    group.style.transform = 'translate(' + place.x.toFixed(1) + 'px, ' + place.y.toFixed(1) + 'px)';
    parts.tooltip.textContent = area.title + (area.summary ? ': ' + area.summary : '');
    setProps(parts.core, { r: place.r, fill: stateSoft(area.state), stroke: stateColor(area.state) });
    setProps(parts.glow, { r: place.r + 4, stroke: stateColor(area.state) });
    parts.count.textContent = String(steps.length || items.length);
    setProps(parts.count, { y: -5, fill: stateColor(area.state) });
    parts.countLabel.textContent = steps.length ? (steps.length === 1 ? 'next step' : 'next steps') : (items.length === 1 ? 'thread' : 'threads');
    setProps(parts.countLabel, { y: 13 });
    var maxChars = Math.max(10, Math.floor((place.cell - 14) / 7.2));
    var lines = wrap(area.title, maxChars, 2);
    parts.title.replaceChildren();
    lines.forEach(function (line, index) {
      parts.title.appendChild(svg('tspan', { x: 0, dy: index ? 16 : 0, text: line }));
    });
    setProps(parts.title, { y: place.r + 20 });
    parts.sub.textContent = (STATE_LABELS[area.state] || 'Active') + (items.length ? ' · ' + items.length + (items.length === 1 ? ' thread' : ' threads') : '');
    setProps(parts.sub, { y: place.r + 20 + lines.length * 16 });
    parts.orbit.replaceChildren();
    var slow = area.state === 'done' || area.state === 'waiting';
    var turning = spin(slow ? 200 : 80);
    if (turning) parts.orbit.appendChild(turning);
    var total = items.length + steps.length;
    items.forEach(function (item, index) {
      var angle = (index / Math.max(1, total)) * Math.PI * 2 - Math.PI / 2;
      parts.orbit.appendChild(svg('circle', { class: 'sat', r: 5, cx: (Math.cos(angle) * (place.r + 9)).toFixed(1), cy: (Math.sin(angle) * (place.r + 9)).toFixed(1), fill: stateColor(item.state) }));
    });
    steps.forEach(function (step, index) {
      var angle = ((items.length + index) / Math.max(1, total)) * Math.PI * 2 - Math.PI / 2;
      var run = stepState(step).run;
      var cx = Math.cos(angle) * (place.r + 12);
      var cy = Math.sin(angle) * (place.r + 12);
      parts.orbit.appendChild(svg('rect', { class: 'sat-step', width: 9, height: 9, x: (cx - 4.5).toFixed(1), y: (cy - 4.5).toFixed(1), rx: 2, transform: 'rotate(45 ' + cx.toFixed(1) + ' ' + cy.toFixed(1) + ')', 'data-open': run === 'idle' && step.kind === 'act' ? 'true' : 'false', style: run === 'done' ? 'fill: var(--done)' : run === 'running' || run === 'sending' ? 'fill: var(--waiting)' : null }));
    });
  }

  function renderMap(list) {
    var box = $('map');
    var width = Math.max(280, Math.round(box.clientWidth || 640));
    lastWidth = width;
    var parts = ensureMapSvg();
    var order = ordered(list);
    var placed = layout(order, width);
    parts.root.setAttribute('viewBox', '0 0 ' + width + ' ' + placed.height);
    if (focus && !placed.positions[focus]) focus = null;
    if (focus) parts.root.setAttribute('data-focus', focus); else parts.root.removeAttribute('data-focus');
    parts.sweep.style.transform = 'translate(' + (width / 2) + 'px, ' + (placed.height / 2) + 'px)';

    var keep = {};
    order.forEach(function (area) {
      keep[area.id] = true;
      var group = nodes[area.id];
      if (!group) {
        group = buildNode(area);
        nodes[area.id] = group;
        parts.nodes.appendChild(group);
      }
      updateNode(group, area, placed.positions[area.id]);
    });
    Object.keys(nodes).forEach(function (areaId) {
      if (keep[areaId]) return;
      var gone = nodes[areaId];
      delete nodes[areaId];
      gone.style.opacity = '0';
      setTimeout(function () { if (gone.parentNode) gone.parentNode.removeChild(gone); }, 500);
    });

    parts.edges.replaceChildren();
    var drawn = {};
    order.forEach(function (area) {
      (area.related || []).forEach(function (other) {
        var a = placed.positions[area.id];
        var b = placed.positions[other];
        var key = area.id < other ? area.id + '|' + other : other + '|' + area.id;
        if (!a || !b || drawn[key] || other === area.id) return;
        drawn[key] = true;
        var mx = (a.x + b.x) / 2;
        var my = (a.y + b.y) / 2;
        var dx = b.x - a.x;
        var dy = b.y - a.y;
        var length = Math.max(1, Math.sqrt(dx * dx + dy * dy));
        var bend = 0.18 * length;
        var path = 'M' + a.x.toFixed(1) + ' ' + a.y.toFixed(1) + ' Q' + (mx - (dy / length) * bend).toFixed(1) + ' ' + (my + (dx / length) * bend).toFixed(1) + ' ' + b.x.toFixed(1) + ' ' + b.y.toFixed(1);
        parts.edges.appendChild(svg('path', { class: 'edge', d: path, 'data-focused': focus === area.id || focus === other ? 'true' : 'false' }));
      });
    });
  }

  function kindIcon(kind) {
    if (kind === 'monitor') {
      return svg('svg', { class: 'kind-icon', viewBox: '0 0 18 18', 'aria-hidden': 'true' }, [
        svg('path', { d: 'M1.5 9 C4 4.5 14 4.5 16.5 9 C14 13.5 4 13.5 1.5 9 Z' }),
        svg('circle', { class: 'fill', cx: 9, cy: 9, r: 2.4 }),
      ]);
    }
    return svg('svg', { class: 'kind-icon', viewBox: '0 0 18 18', 'aria-hidden': 'true' }, [
      svg('circle', { cx: 9, cy: 9, r: 7.5 }),
      svg('path', { class: 'fill', d: 'M7.2 5.6 L12.4 9 L7.2 12.4 Z' }),
    ]);
  }

  function openSession(sessionId) {
    client.navigation.openSession(sessionId, { display: 'panel' }).catch(function (error) {
      notice('Could not open that session: ' + message(error), 'error');
    });
  }

  function sessionTitle(sessionId, index) {
    var session = index[sessionId];
    return session && session.title ? session.title : 'a session';
  }

  function renderStep(area, step, index) {
    var state = stepState(step);
    var busy = state.run === 'running' || (state.run === 'sending' && !state.retry);
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
      why.push(el('button', { type: 'button', class: 'cite', text: sessionTitle(cited, index), on: { click: function () { openSession(cited); } } }));
      if (step.evidence && step.evidence.quote) why.push(el('span', { class: 'quote', text: ': “' + step.evidence.quote + '”' }));
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
      children.push(el('button', { type: 'button', class: 'step-x', 'data-omnigent-requires': 'write', 'aria-label': 'Hide this next step', title: 'Hide this next step', disabled: Boolean(dismissing[step.id]), text: '×', on: { click: function () { dismiss(step, area.id); } } }));
    }
    return el('div', { class: 'step', 'data-kind': step.kind, 'data-run': state.run, 'data-omnigent-ref': 'maps/current#steps/' + step.id, 'data-omnigent-label': step.label }, children);
  }

  function renderSession(session) {
    var state = liveState(session.id, session.status);
    var steps = session.steps || [];
    var meta = [];
    if (state) meta.push(LIVE_TEXT[state] || state);
    if (live.on && live.byId[session.id] && live.byId[session.id].unread) meta.push('new messages');
    if (session.why) meta.push(session.why);
    var done = steps.filter(function (step) { return step.status === 'done'; }).length;
    if (steps.length) meta.push(done + ' of ' + steps.length + ' steps done');
    return el('div', { class: 'session', 'data-omnigent-ref': 'maps/current#sessions/' + session.id, 'data-omnigent-label': session.title || 'Session' }, [
      el('button', { type: 'button', class: 'session-link', on: { click: function () { openSession(session.id); } } }, [
        el('span', { class: 'live-dot', 'data-live': state || 'unknown', 'aria-hidden': 'true' }),
        el('span', { text: session.title || 'Untitled session' }),
      ]),
      meta.length ? el('div', { class: 'session-meta', text: meta.join(' · ') }) : null,
      session.outcome ? el('div', { class: 'session-meta', text: session.outcome }) : null,
      steps.length ? el('div', { class: 'steps-bar', 'aria-hidden': 'true' }, steps.map(function (step) { return el('i', { 'data-s': step.status, title: step.step + ': ' + step.status }); })) : null,
    ]);
  }

  function renderAreas(list) {
    var index = sessionsById();
    var value = map();
    var cards = ordered(list).slice().sort(function (a, b) {
      return (STATE_ORDER[a.state] - STATE_ORDER[b.state]) || 0;
    }).map(function (area) {
      var steps = openSteps(area);
      var hidden = (area.next_steps || []).length - steps.length;
      var watched = ((value && value.sessions) || []).filter(function (session) { return session.area === area.id && session.why; });
      return el('article', {
        class: 'area', id: 'card-' + area.id, 'data-state': STATE_LABELS[area.state] ? area.state : 'active',
        'data-focused': focus === area.id ? 'true' : 'false', 'data-omnigent-ref': 'maps/current#areas/' + area.id, 'data-omnigent-label': area.title,
      }, [
        el('div', { class: 'area-head' }, [
          el('h3', { text: area.title }),
          el('span', { class: 'pill', text: STATE_LABELS[area.state] || 'Active' }),
        ]),
        area.summary ? el('p', { class: 'summary', text: area.summary }) : null,
        steps.length ? el('div', { class: 'block-label', text: 'Next steps' }) : null,
        steps.length ? el('div', { class: 'steps' }, steps.map(function (step) { return renderStep(area, step, index); })) : null,
        (area.items || []).length ? el('div', { class: 'block-label', text: 'Threads' }) : null,
        (area.items || []).length ? el('ul', { class: 'items' }, area.items.map(function (item) {
          return el('li', { 'data-omnigent-ref': 'maps/current#items/' + item.id, 'data-omnigent-label': item.title }, [
            el('span', { class: 'dot', style: 'background:' + stateColor(item.state), title: STATE_LABELS[item.state] || '' }),
            el('div', {}, [el('div', { text: item.title }), item.summary ? el('div', { class: 'item-sum', text: item.summary }) : null]),
          ]);
        })) : null,
        watched.length ? el('div', { class: 'block-label', text: 'Keep an eye on' }) : null,
        watched.length ? el('div', { class: 'watch' }, watched.map(renderSession)) : null,
        hidden > 0 ? el('div', { class: 'dismissed-note' }, [
          hidden + (hidden === 1 ? ' next step hidden. ' : ' next steps hidden. '),
          canWrite ? el('button', { type: 'button', class: 'link-button', 'data-omnigent-requires': 'write', text: 'Show again', on: { click: function () {
            (area.next_steps || []).forEach(function (step) { if (dismissedDocs[step.id]) dismiss(step, area.id); });
          } } }) : null,
        ]) : null,
      ]);
    });
    $('areas').replaceChildren.apply($('areas'), cards);
  }

  // The most useful next steps first: blocked work, then things to do, then things to watch.
  function renderNextUp(list) {
    var picks = [];
    list.forEach(function (area) {
      openSteps(area).forEach(function (step, position) {
        var state = stepState(step);
        if (state.run === 'done') return;
        picks.push({ area: area, step: step, state: state, score: STATE_ORDER[area.state] * 10 + (step.kind === 'act' ? 0 : 5) + position });
      });
    });
    picks.sort(function (a, b) { return a.score - b.score; });
    picks = picks.slice(0, 4);
    $('next-section').hidden = picks.length === 0;
    $('next-list').replaceChildren.apply($('next-list'), picks.map(function (pick) {
      var busy = pick.state.run === 'running' || (pick.state.run === 'sending' && !pick.state.retry);
      return el('button', {
        type: 'button', class: 'next-chip', 'data-omnigent-requires': 'action', disabled: !canAct || busy,
        'data-omnigent-ref': 'maps/current#steps/' + pick.step.id, 'data-omnigent-label': pick.step.label,
        on: { click: function () { requestStep(pick.area, pick.step); } },
      }, [
        busy ? el('span', { class: 'spinner', 'aria-hidden': 'true' }) : kindIcon(pick.step.kind),
        el('span', { text: pick.step.label }),
        el('span', { class: 'where', text: pick.area.title }),
      ]);
    }));
  }

  function renderDone() {
    var rows = Object.keys(runDocs).map(function (key) { return runDocs[key]; }).filter(function (doc) {
      return doc.value && (doc.value.state === 'done' || doc.value.state === 'failed');
    }).sort(function (a, b) { return a.updatedAt < b.updatedAt ? 1 : -1; }).slice(0, 6);
    $('done-section').hidden = rows.length === 0;
    $('done-list').replaceChildren.apply($('done-list'), rows.map(function (doc) {
      var value = doc.value;
      return el('li', { 'data-state': value.state, 'data-omnigent-ref': 'step_runs/' + doc.id, 'data-omnigent-label': value.label }, [
        el('span', { class: 'mark', 'aria-hidden': 'true', text: value.state === 'done' ? '✓' : '!' }),
        el('div', {}, [
          el('div', { text: value.label }),
          el('div', { class: 'session-meta' }, [
            (value.state === 'done' ? 'Done ' : 'Did not work, ') + ago(value.updated || doc.updatedAt) + (value.outcome ? ': ' + value.outcome : ''),
            value.session_id ? ' ' : null,
            value.session_id ? el('button', { type: 'button', class: 'link-button', text: 'Open session', on: { click: function () { openSession(value.session_id); } } }) : null,
          ]),
        ]),
      ]);
    }));
  }

  // ---- The template preview's example map, kept in memory

  function seedPreview() {
    previewSeeded = true;
    var now = new Date().toISOString();
    maps.create({
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
        { id: 'bug-export', title: 'CSV export bug', state: 'active', kind: 'bug',
          summary: 'Exports drop the last row for large files; a test reproduces it.',
          items: [{ id: 'repro', title: 'Reproduced with a failing test', state: 'done' }],
          next_steps: [{ id: 'bug-fix', label: 'Fix the off-by-one and rerun the test', kind: 'act' }, { id: 'bug-reply', label: 'Reply to the bug report', kind: 'act' }] },
      ],
      sessions: [
        { id: 'example-1', title: 'Ramp checkout flag', area: 'checkout-rollout', why: 'Watch errors after the next ramp', status: 'idle', outcome: 'Ramped to 5%; error rate unchanged.', steps: [{ step: 'ramp', status: 'done' }, { step: 'verify', status: 'done' }, { step: 'next-ramp', status: 'started' }] },
      ],
    }, { id: 'current' }).then(function (doc) {
      // Show it now rather than at the next poll.
      if (!mapDoc) { mapDoc = doc; schedule(); }
    }, function () { return undefined; });
  }

  // ---- Wiring

  function onMaps(snapshot) {
    if (snapshot.status === 'loading') return;
    if (snapshot.status === 'stale' && snapshot.error) notice('Could not load the map: ' + message(snapshot.error), 'error');
    mapDoc = (snapshot.items || []).filter(function (doc) { return doc.id === 'current'; })[0] || null;
    mapLoaded = true;
    if (preview && !mapDoc && !previewSeeded && snapshot.status === 'live') seedPreview();
    var value = map();
    if (value && !$('window').dataset.touched) $('window').value = String([1, 3, 7, 14].indexOf(value.window_days) !== -1 ? value.window_days : 3);
    schedule();
  }
  function byIdOf(items) {
    var out = {};
    (items || []).forEach(function (doc) { out[doc.id] = doc; });
    return out;
  }

  $('scan').addEventListener('click', requestScan);
  $('scan-empty').addEventListener('click', requestScan);
  $('window').addEventListener('change', function () { $('window').dataset.touched = 'true'; });
  $('live').addEventListener('click', function () { setLive(!live.on); });
  $('daily').addEventListener('click', function () {
    var name = client.context.app.name;
    client.navigation.newAutomation({
      name: 'Refresh ' + name,
      prompt: 'Load the thingy skill and follow it. Refresh the thingy "' + name + '" (thingy_id ' + client.context.app.id + '): scan my sessions from the last ' + windowDays() + ' days as its guide describes under Scan, and replace its map.',
      schedule: 'FREQ=DAILY;BYHOUR=8;BYMINUTE=0',
    }).catch(function (error) { notice('Could not open Automations: ' + message(error), 'error'); });
  });
  if (window.ResizeObserver) {
    new ResizeObserver(function () {
      var width = Math.round($('map').clientWidth || 0);
      if (width && Math.abs(width - lastWidth) > 8) schedule();
    }).observe($('map'));
  }
  setInterval(schedule, 60000);

  window.omnigent.connect().then(function (connected) {
    client = connected;
    document.body.setAttribute('data-theme', client.ui.colorScheme);
    preview = client.context.preview === true;
    canWrite = client.hasCapability('data.write') && client.can('write');
    canAct = Boolean(client.agent) && client.can('action') && !preview;
    $('daily').hidden = !canAct;
    if (!preview && !client.can('action')) notice('You can look at this map. Scanning and next steps run in the owner’s thingy session.');
    maps = client.data.collection('maps');
    stepRuns = client.data.collection('step_runs');
    dismissed = client.data.collection('dismissed');
    maps.watch(onMaps, { intervalMs: 4000, limit: 5 });
    stepRuns.watch(function (snapshot) { if (snapshot.status !== 'loading') { runDocs = byIdOf(snapshot.items); schedule(); } }, { intervalMs: 4000, limit: 100 });
    dismissed.watch(function (snapshot) { if (snapshot.status !== 'loading') { dismissedDocs = byIdOf(snapshot.items); schedule(); } }, { intervalMs: 8000, limit: 100 });
    if (canAct && typeof client.agent.watch === 'function') client.agent.watch(onActions, { intervalMs: 3000 });
    schedule();
  }, function (error) {
    $('loading').hidden = true;
    notice('The thingy could not connect to Omnigent: ' + message(error), 'error');
  });
})();
`;

const FORMING = `
<svg class="forming" viewBox="0 0 320 220" aria-hidden="true">
  <path class="f-line" d="M70 60 Q130 40 170 90"/>
  <path class="f-line" d="M170 90 Q220 140 260 120"/>
  <path class="f-line" d="M170 90 Q140 150 90 170"/>
  <path class="f-line" d="M260 120 Q280 70 240 40"/>
  <circle class="f-ring" cx="170" cy="90" r="30"/>
  <circle class="f-dot" cx="170" cy="90" r="22"/>
  <circle class="f-dot d2" cx="70" cy="60" r="14"/>
  <circle class="f-dot d3" cx="260" cy="120" r="17"/>
  <circle class="f-dot d4" cx="90" cy="170" r="11"/>
  <circle class="f-dot d2" cx="240" cy="40" r="9"/>
</svg>`;

const BODY = `
<main>
  <div class="top">
    <div>
      <p class="kicker">My work <span class="badge" title="Built from the owner's own sessions. Anyone you share this thingy with sees these summaries.">Personal</span></p>
      <h1>My work map</h1>
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
  <p id="notice" class="notice" role="status" hidden></p>
  <p id="loading" class="muted">Loading…</p>

  <section id="empty" class="empty" hidden>
    <div>
      <h2>See your work at a glance</h2>
      <p>Your thingy session reads the sessions you worked in recently and draws what they add up to: the efforts you are driving, what is blocked or waiting, and the obvious next steps, such as ramping a flag, checking a metric or following up on a bug.</p>
      <p>Click a next step and your thingy session does it or checks on it, and the map keeps up as the work moves.</p>
      <button type="button" id="scan-empty" class="primary" data-omnigent-requires="action">Scan my recent work</button>
      <p id="empty-run" class="status"></p>
    </div>
    ${FORMING}
  </section>

  <div id="content" hidden>
    <p id="nothing" class="notice" hidden>Nothing to map in this window. Try a longer one, then Rescan.</p>
    <section id="map-section">
      <div class="map-card">
        <div id="map"></div>
        <div class="legend" aria-hidden="true">
          <span><i style="background:var(--active)"></i>Active</span>
          <span><i style="background:var(--blocked)"></i>Blocked</span>
          <span><i style="background:var(--waiting)"></i>Waiting</span>
          <span><i style="background:var(--done)"></i>Done</span>
          <span>Small dots are threads; diamonds are next steps. Click an area to see it below.</span>
        </div>
      </div>
    </section>
    <section id="next-section" hidden>
      <h2>Next up</h2>
      <div id="next-list" class="next-list"></div>
    </section>
    <section>
      <div id="areas" class="areas"></div>
    </section>
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
            <div class="layer"><svg viewBox="0 0 26 26" aria-hidden="true"><circle cx="13" cy="13" r="9" fill="var(--active-soft)" stroke="var(--active)" stroke-width="2"/></svg><div><b>The map</b> comes from your thingy session. On Scan it reads your recent sessions (their titles, journals and last messages, not whole transcripts), groups them by what you are trying to get done, and writes the next steps it can point to.</div></div>
            <div class="layer"><svg viewBox="0 0 26 26" aria-hidden="true"><circle cx="13" cy="13" r="5" fill="var(--active)"/><circle cx="13" cy="13" r="10" fill="none" stroke="var(--active)" stroke-width="1.5" stroke-dasharray="3 3"/></svg><div><b>Live status</b> is optional. Turn it on and this page shows, without asking the agent, which linked sessions are running or need your input.</div></div>
            <div class="layer"><svg viewBox="0 0 26 26" aria-hidden="true"><rect x="8" y="8" width="10" height="10" rx="2" transform="rotate(45 13 13)" fill="var(--accent)"/></svg><div><b>Next steps</b> go to your thingy session, which does them, hands them to the session they came from, or starts a new one. Each step shows its progress here. Hide the ones you do not need with ×.</div></div>
          </div>
          <h3>Personal</h3>
          <ul><li>The map summarizes your own sessions. Anyone you share this thingy with sees those summaries, so share it only when that is fine.</li></ul>
        </div>
        <div>
          <h3>Make it yours</h3>
          <ul>
            <li>Ask your thingy session, in its chat: <span class="ask">“Add an area for my pull request reviews.”</span></li>
            <li><span class="ask">“Scan 7 days by default and leave out sessions about the docs site.”</span></li>
            <li><span class="ask">“Group by project instead of by goal.”</span></li>
            <li><span class="ask">“Make the map more compact and show blocked work first.”</span></li>
            <li>Rescan whenever you like, or keep it fresh on its own: <button type="button" id="daily" class="link-button" data-omnigent-requires="action" hidden>refresh it every morning</button>.</li>
          </ul>
        </div>
      </div>
    </details>
  </section>
  <footer>Select any text, or Alt+click an area or next step, to reply about it or ask in a side chat.</footer>
</main>
`;

export const WORK_MAP_HTML = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="omnigent-template" content="work-map">
<title>My work map</title>
<style>${STYLE}</style>
</head>
<body>
${BODY}
<script>${SCRIPT}</script>
</body>
</html>
`;

export const WORK_MAP_GUIDE = [
  "# My work map",
  "",
  "A map of what the viewer has been working on, built from their own recent sessions and organized by goal, not by session. The page draws areas (efforts such as a flag rollout, an investigation or a review queue) with their threads and next steps, and links the sessions the viewer should watch or act in. It is personal: it summarizes the owner's sessions.",
  "",
  "## Collections",
  "",
  "- `maps`: exactly one record, ID `current`, holding the whole map. Each scan replaces it in one write (`sys_thingy_records` `get` it, then `replace` with its `version`; `create` with `id` `current` when it does not exist yet). Never create other records here.",
  "- `step_runs`: one record per next step you worked on, with the next step's ID as the record ID: `{label, area, state: running|done|failed, outcome, session_id, updated}`. The page shows its progress on the step and lists finished ones under Done recently.",
  "- `dismissed`: next steps the viewer hid, with the next step's ID as the record ID. The page writes these; you only read them.",
  "",
  "## The map record",
  "",
  "- `scanned_at` (ISO 8601 UTC), `window_days`, `sessions_read`, and `headline`: one plain sentence on where the work stands.",
  "- `areas` (up to 12): `{id, title, summary, state, kind, related, items, next_steps}`. `id` is a stable lowercase slug (`[a-z0-9][a-z0-9_-]*`, at most 64), such as `checkout-rollout`. `state` is `active`, `blocked`, `waiting` (on someone or something else) or `done`. `kind` is a word such as rollout, investigation, review, bug, incident or project. `related` lists IDs of areas that depend on or inform this one.",
  "- `items` (up to 8 per area): the threads of work in it, `{id, title, summary, state, session_ids}`.",
  "- `next_steps` (up to 5 per area): `{id, label, why, kind, session_id, evidence: {session_id, quote}}`. `kind` is `act` (something to do) or `monitor` (something to check). `id` is a stable slug, unique across the map, which also names its `step_runs` and `dismissed` records.",
  "- `sessions` (up to 30): `{id, title, area, why, status, outcome, steps}` for each session the map refers to. Set `why` only on sessions the viewer should monitor or act in; those appear under Keep an eye on. `status` is the session's status when you scanned, `outcome` one sentence from its journal or last messages, and `steps` the latest plan step statuses from its journal.",
  "",
  "## Scan",
  "",
  "The Scan button requests the `scan` action with `window_days`. A chat request to scan, or an automation's prompt, works the same way without the action.",
  "",
  "1. List sessions with `sys_session_list` (visibility `mine`), and keep those updated within `window_days`, at most 25, newest first. Skip this thingy session, sessions you started for this thingy's own next steps, and trivial ones (a greeting, an empty or one-message session).",
  "2. For each kept session read only a little: its title and status from the list, `sys_journal_read` with its `conversation_id` (plan step statuses and notes), and `sys_session_get_history` limited to its last few items when the journal is empty. Never read whole transcripts. Read in parallel where you can.",
  "3. Group the sessions by goal. Several sessions about one rollout are one area; one session can feed several areas. Name each area by what the viewer is trying to get done.",
  "4. Write next steps only when the sessions point to something specific: a flag to ramp after a quiet bake, a metric to check after a change, a bug to follow up on, a review someone waits for, a failed run to retry. Cite the session and a short quote in `evidence`. Write none for an area when nothing specific stands out; never pad with generic advice.",
  "5. Read `dismissed` and leave those next steps out, and read `step_runs`: drop steps that are done unless the work still needs a follow-up, which gets a new step.",
  "6. On a rescan, keep the IDs of areas, items and next steps that are still the same thing, so the page moves them instead of redrawing, and mark finished work `done` rather than removing it at once.",
  '7. Replace the map record in one write, then report the action `done` with a one-line summary (for example "4 areas, 6 next steps from 14 sessions").',
  "",
  "## Next steps",
  "",
  "A click on a next step requests `do_next_step` with `next_step` (its ID), `label` and `area`. Read the step from the map, then:",
  "",
  "1. Create or replace its `step_runs` record with `state` `running`.",
  "2. Carry it out. When the step names a session (`session_id` or `evidence.session_id`) and the work belongs there, message that session with `sys_session_message`; otherwise do it yourself or start a session with `sys_session_create`. Record the session you used in `session_id` on the run and on the action.",
  "3. For a `monitor` step, check and report what you found rather than changing anything.",
  "4. Set the run `done` or `failed` with a one-sentence `outcome`, update the matching area or item in the map when the step changed its state, and report the action.",
  "",
  "Ask in the session before anything with a wide effect, such as ramping a flag in production or posting to other people.",
  "",
  "## Customizing",
  "",
  "People ask the thingy session to change the map: add an area, scan a different window by default, leave sessions out, group differently, or change the look. Change the scan rules in this guide and the page in a new revision; keep the collections, since a published collection's schema cannot change.",
].join("\n");

export const WORK_MAP_REVISION: CreateThingyRevisionRequest = {
  html: WORK_MAP_HTML,
  sdk_version: 1,
  capabilities: ["data.read", "data.write", "sessions.read"],
  collections: {
    [WORK_MAP_COLLECTION]: WORK_MAP_SCHEMA,
    step_runs: WORK_MAP_STEP_RUN_SCHEMA,
    dismissed: WORK_MAP_DISMISSED_SCHEMA,
  },
  guide: WORK_MAP_GUIDE,
  constraints: [
    {
      name: "one_map",
      kind: "max_records",
      collection: WORK_MAP_COLLECTION,
      max: 1,
      message: "The map is one record with ID current: replace it instead of creating another.",
    },
  ],
  actions: [
    {
      name: "scan",
      description:
        "Rebuild the map from the viewer's recent sessions as the guide's Scan section says: list their sessions " +
        "updated in the last params.window_days days (default 3, at most 25 sessions), read each one's journal and " +
        "only its last few messages, group them by goal into areas with threads and specific next steps that cite " +
        "their session, and replace the maps record with ID current in one write.",
      params: {
        type: "object",
        additionalProperties: false,
        properties: { window_days: { type: "integer", minimum: 1, maximum: 30 } },
      },
    },
    {
      name: "do_next_step",
      description:
        "Do or check the next step params.next_step from the map, as the guide's Next steps section says: track it " +
        "in its step_runs record (running, then done or failed with an outcome), work in the session it came from " +
        "when that is where it belongs, and update the map's area when the step changed it.",
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
  ],
};
