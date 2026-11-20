// Data story template: a scrolling, designed report in one self-contained HTML document that uses
// only `window.omnigent`. Its numbers live in one `snapshots` record that the viewer's thingy
// session recomputes on Refresh; on first load it seeds one synthetic sample snapshot.
//
// The script is plain ES2018 inside a template literal: keep it free of backticks, `${`, and
// backslashes. Characters that would need escapes are built with String.fromCharCode.

import type { CreateThingyRevisionRequest } from "../api/thingyApi";

export const DATA_STORY_COLLECTION = "snapshots";
// The ID of the synthetic snapshot seeded on first load.
export const DATA_STORY_SAMPLE_ID = "sample-synthetic";

// One record holds a whole computed dataset, so a refresh is one write and the page never shows
// half of an update. A record is at most 64 KiB, which fits about 1,000 series rows.
export const DATA_STORY_SNAPSHOT_SCHEMA = {
  type: "object",
  additionalProperties: true,
  required: ["capturedAt", "title", "metric", "segments", "series"],
  properties: {
    capturedAt: { type: "string", minLength: 10, maxLength: 40 },
    title: { type: "string", minLength: 1, maxLength: 120 },
    summary: { type: "string", maxLength: 2000 },
    source: { type: "string", maxLength: 400 },
    synthetic: { type: "boolean" },
    metric: {
      type: "object",
      additionalProperties: false,
      required: ["name", "aggregate"],
      properties: {
        name: { type: "string", minLength: 1, maxLength: 80 },
        unit: { type: "string", maxLength: 20 },
        aggregate: { type: "string", enum: ["sum", "average"] },
      },
    },
    segments: {
      type: "array",
      minItems: 1,
      maxItems: 8,
      items: { type: "string", minLength: 1, maxLength: 60 },
    },
    series: {
      type: "array",
      maxItems: 1000,
      items: {
        type: "object",
        additionalProperties: false,
        required: ["date", "segment", "value"],
        properties: {
          date: { type: "string", minLength: 10, maxLength: 10 },
          segment: { type: "string", minLength: 1, maxLength: 60 },
          value: { type: "number" },
        },
      },
    },
    annotations: {
      type: "array",
      maxItems: 20,
      items: {
        type: "object",
        additionalProperties: false,
        required: ["date", "title"],
        properties: {
          date: { type: "string", minLength: 10, maxLength: 10 },
          title: { type: "string", minLength: 1, maxLength: 80 },
          text: { type: "string", maxLength: 600 },
          segment: { type: "string", maxLength: 60 },
        },
      },
    },
  },
} satisfies Record<string, unknown>;

const STYLE = `
:root {
  color-scheme: light;
  --bg: #fbfaf7; --surface: #ffffff; --text: #1d1b16; --muted: #6b665c; --faint: #e9e5dc; --line: #d9d3c7;
  --accent: #c2410c; --accent-soft: #fde8dc; --good: #15803d; --bad: #b91c1c; --focus: #2563eb;
  --s1: #c2410c; --s2: #2563eb; --s3: #7c3aed; --s4: #0f766e; --s5: #a16207; --s6: #be185d; --s7: #4d7c0f; --s8: #475569;
  --serif: Georgia, 'Times New Roman', serif; --sans: system-ui, -apple-system, 'Segoe UI', sans-serif;
}
body[data-theme='dark'] {
  color-scheme: dark;
  --bg: #15140f; --surface: #1e1c17; --text: #efe9dc; --muted: #a59e90; --faint: #2c2a23; --line: #3a372e;
  --accent: #fb923c; --accent-soft: #3b2415; --good: #4ade80; --bad: #f87171; --focus: #60a5fa;
  --s1: #fb923c; --s2: #60a5fa; --s3: #a78bfa; --s4: #2dd4bf; --s5: #facc15; --s6: #f472b6; --s7: #a3e635; --s8: #94a3b8;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text); font: 15px/1.55 var(--sans); }
main { max-width: 920px; margin: 0 auto; padding: 32px 24px 64px; }
h1 { font: 600 34px/1.15 var(--serif); margin: 0 0 6px; letter-spacing: -0.01em; }
h2 { font: 600 21px/1.3 var(--serif); margin: 0 0 4px; }
.kicker { text-transform: uppercase; letter-spacing: 0.08em; font-size: 12px; color: var(--accent); font-weight: 600; margin: 0 0 6px; }
.muted { color: var(--muted); }
.meta { display: flex; flex-wrap: wrap; align-items: center; gap: 10px 16px; font-size: 13px; color: var(--muted); }
.toolbar { display: flex; align-items: center; gap: 12px; margin-top: 12px; flex-wrap: wrap; }
button { font: inherit; color: inherit; }
.primary { border: 1px solid var(--accent); background: var(--accent); color: #fff; border-radius: 8px; padding: 6px 14px; font-weight: 600; cursor: pointer; }
.primary:disabled { opacity: 0.55; cursor: default; }
.quiet { border: 1px solid var(--line); background: var(--surface); border-radius: 8px; padding: 4px 10px; cursor: pointer; }
.quiet[aria-pressed='true'] { border-color: var(--accent); background: var(--accent-soft); }
button:focus-visible, [tabindex]:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
.status { display: inline-flex; align-items: center; gap: 8px; font-size: 13px; color: var(--muted); }
.spinner { width: 12px; height: 12px; border: 2px solid var(--accent); border-right-color: transparent; border-radius: 50%; animation: spin 0.8s linear infinite; flex: none; }
@keyframes spin { to { transform: rotate(360deg); } }
.notice { margin: 16px 0 0; padding: 10px 12px; border-radius: 8px; background: var(--faint); font-size: 14px; }
.notice[data-kind='error'] { background: var(--accent-soft); color: var(--bad); }
section { margin-top: 40px; }
.headline { display: grid; grid-template-columns: minmax(0, 1fr) 260px; gap: 24px; align-items: end; padding: 24px; background: var(--surface); border: 1px solid var(--line); border-radius: 14px; }
.big { font: 600 56px/1 var(--serif); letter-spacing: -0.02em; }
.delta { font-weight: 600; margin-top: 8px; }
.delta[data-dir='up'] { color: var(--good); }
.delta[data-dir='down'] { color: var(--bad); }
.story { font: 18px/1.6 var(--serif); margin: 20px 0 0; max-width: 680px; }
.filters { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-top: 12px; }
.chip { display: inline-flex; align-items: center; gap: 6px; }
.swatch { width: 10px; height: 10px; border-radius: 3px; display: inline-block; }
.chart { position: relative; margin-top: 14px; background: var(--surface); border: 1px solid var(--line); border-radius: 14px; padding: 12px 8px 4px; }
.chart svg { display: block; width: 100%; height: auto; touch-action: none; }
.axis text { fill: var(--muted); font-size: 11px; }
.grid line { stroke: var(--faint); }
.series path { fill: none; stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
.series path[data-dim='true'] { opacity: 0.25; }
.points circle { fill: transparent; cursor: pointer; }
.points circle:hover { fill: currentColor; }
.brush { fill: var(--accent); opacity: 0.12; }
.cursor-line { stroke: var(--muted); stroke-dasharray: 3 3; }
.note-line { stroke: var(--accent); stroke-dasharray: 2 4; }
.note-dot { fill: var(--accent); cursor: pointer; }
.note-dot[data-active='true'] { stroke: var(--text); stroke-width: 2; }
.note-num { fill: #fff; font-size: 11px; font-weight: 700; pointer-events: none; }
.readout { min-height: 22px; font-size: 13px; color: var(--muted); padding: 4px 8px 6px; }
.notes { list-style: none; margin: 16px 0 0; padding: 0; display: grid; gap: 10px; }
.notes li { display: grid; grid-template-columns: 26px minmax(0, 1fr); gap: 10px; padding: 12px; border-radius: 10px; border: 1px solid var(--line); background: var(--surface); }
.notes li[data-active='true'] { border-color: var(--accent); }
.num { width: 22px; height: 22px; border-radius: 50%; background: var(--accent); color: #fff; font-size: 12px; font-weight: 700; display: inline-flex; align-items: center; justify-content: center; }
.bars { display: grid; gap: 8px; margin-top: 14px; }
.bar { display: grid; grid-template-columns: 140px minmax(0, 1fr) 90px; gap: 12px; align-items: center; text-align: left; border: 1px solid transparent; background: none; padding: 6px 8px; border-radius: 8px; cursor: pointer; }
.bar:hover, .bar[aria-pressed='true'] { border-color: var(--line); background: var(--surface); }
.bar-track { height: 14px; background: var(--faint); border-radius: 7px; overflow: hidden; }
.bar-fill { height: 100%; border-radius: 7px; }
.bar-value { text-align: right; font-variant-numeric: tabular-nums; }
table { width: 100%; border-collapse: collapse; margin-top: 12px; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: 7px 8px; border-bottom: 1px solid var(--faint); }
th { font-size: 12px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); }
th button { border: 0; background: none; padding: 0; font: inherit; color: inherit; cursor: pointer; text-transform: inherit; letter-spacing: inherit; }
td.n, th.n { text-align: right; }
.badge { display: inline-block; border-radius: 999px; padding: 2px 8px; font-size: 12px; background: var(--faint); color: var(--muted); }
.skeleton { height: 18px; border-radius: 6px; background: var(--faint); animation: pulse 1.4s ease-in-out infinite; margin: 10px 0; }
@keyframes pulse { 50% { opacity: 0.5; } }
footer { margin-top: 48px; font-size: 12px; color: var(--muted); }
@media (max-width: 640px) {
  main { padding: 20px 14px 48px; }
  .headline { grid-template-columns: minmax(0, 1fr); }
  .big { font-size: 44px; }
  .bar { grid-template-columns: 100px minmax(0, 1fr) 70px; }
}
@media (prefers-reduced-motion: reduce) { .spinner, .skeleton { animation: none; } }
`;

const SCRIPT = `
(function () {
  'use strict';
  var SVG_NS = 'http://www.w3.org/2000/svg';
  var UP = String.fromCharCode(9650);
  var DOWN = String.fromCharCode(9660);
  var DASH = String.fromCharCode(8212);
  var MIDDOT = String.fromCharCode(183);
  var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  var DAY_MS = 86400000;
  var COLORS = ['var(--s1)', 'var(--s2)', 'var(--s3)', 'var(--s4)', 'var(--s5)', 'var(--s6)', 'var(--s7)', 'var(--s8)'];
  var PROGRESS_TEXT = {
    pending: 'Start your thingy session below this page to refresh.',
    queued: 'Waiting for your thingy session',
    running: 'Refreshing',
    needs_input: 'Waiting for your input in the thingy session',
    done: 'Refresh done',
    failed: 'Refresh failed',
    cancelled: 'Refresh cancelled',
    finished_unreported: 'Refresh finished without a report',
    interrupted: 'Refresh stopped before it finished. Click Refresh to try again.',
  };
  var OPEN = ['pending', 'queued', 'running', 'needs_input'];
  var PENDING_MS = 10 * 60 * 1000;

  var client = null;
  var snapshots = null;
  var canWrite = false;
  var canRefresh = false;
  var seeded = false;
  var seeding = false;
  var preview = false;
  var doc = null;
  var data = null;
  var refreshAction = null;
  var actionWatch = null;
  var pending = null;
  var state = { selected: null, from: 0, to: 0, preset: 'all', cursor: -1, drill: null, sort: 'date', note: -1 };
  var brush = null;

  function el(tag, props, children) {
    var node = document.createElement(tag);
    setProps(node, props);
    (children || []).forEach(function (child) {
      if (child !== null && child !== undefined) node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
    });
    return node;
  }
  function svg(tag, props, children) {
    var node = document.createElementNS(SVG_NS, tag);
    setProps(node, props);
    (children || []).forEach(function (child) { if (child) node.appendChild(child); });
    return node;
  }
  function setProps(node, props) {
    Object.keys(props || {}).forEach(function (key) {
      var value = props[key];
      if (value === null || value === undefined || value === false) return;
      if (key === 'text') node.textContent = value;
      else if (key === 'on') Object.keys(value).forEach(function (type) { node.addEventListener(type, value[type]); });
      else if (key === 'style') node.setAttribute('style', value);
      else node.setAttribute(key, value === true ? '' : String(value));
    });
  }
  function $(id) { return document.getElementById(id); }

  // ---- Dates and numbers

  function dayOf(text) { return Date.UTC(Number(text.slice(0, 4)), Number(text.slice(5, 7)) - 1, Number(text.slice(8, 10))); }
  function isoOf(ms) { return new Date(ms).toISOString().slice(0, 10); }
  function shortDate(text) { var d = new Date(dayOf(text)); return MONTHS[d.getUTCMonth()] + ' ' + d.getUTCDate(); }
  function longDate(text) { var d = new Date(dayOf(text)); return MONTHS[d.getUTCMonth()] + ' ' + d.getUTCDate() + ', ' + d.getUTCFullYear(); }
  function fmt(value) {
    if (value === null || value === undefined || isNaN(value)) return DASH;
    var unit = data && data.metric.unit ? ' ' + data.metric.unit : '';
    var abs = Math.abs(value);
    var text = abs >= 10000 ? Math.round(value).toLocaleString() : abs >= 100 ? value.toFixed(0) : value.toFixed(1);
    return text + unit;
  }
  function pct(value) { return (value > 0 ? '+' : '') + (value * 100).toFixed(1) + '%'; }
  function when(iso) {
    var d = new Date(iso);
    return isNaN(d) ? 'an unknown time' : d.toISOString().replace('T', ' ').slice(0, 16) + ' UTC';
  }

  // ---- The synthetic sample

  function sampleSnapshot() {
    var seed = 42;
    function rand() { seed = (seed * 16807) % 2147483647; return seed / 2147483647; }
    var segments = ['Enterprise', 'Mid-market', 'Self-serve', 'Partners'];
    var base = [420, 310, 520, 140];
    var growth = [0.9, 0.6, 1.1, 0.35];
    var start = dayOf('2026-08-03');
    var series = [];
    for (var day = 0; day < 90; day += 1) {
      var date = isoOf(start + day * DAY_MS);
      var weekday = new Date(start + day * DAY_MS).getUTCDay();
      var weekly = weekday === 0 || weekday === 6 ? 0.78 : 1;
      segments.forEach(function (segment, index) {
        var value = (base[index] + growth[index] * day) * weekly * (0.95 + rand() * 0.1);
        if (segment === 'Self-serve' && day >= 42 && day <= 46) value *= 0.62 + (day - 42) * 0.07;
        if (segment === 'Enterprise' && day >= 64) value *= 1.18;
        if (segment === 'Partners' && day >= 75) value *= 1 + (day - 75) * 0.02;
        series.push({ date: date, segment: segment, value: Math.round(value) });
      });
    }
    return {
      capturedAt: '2026-11-01T09:00:00Z',
      title: 'How active teams grew this quarter',
      summary: 'Sample story with synthetic numbers. Active teams grew steadily through the quarter. Self-serve dipped for a week in mid-September and recovered; Enterprise stepped up after the October launch, and Partners has been climbing since mid-October.',
      source: 'Synthetic sample data. Ask your thingy session to connect it to your own table.',
      synthetic: true,
      metric: { name: 'Active teams', unit: '', aggregate: 'average' },
      segments: segments,
      series: series,
      annotations: [
        { date: '2026-09-14', title: 'Self-serve sign-up outage', text: 'A synthetic sign-up incident cut new self-serve teams for five days; the segment recovered by Sep 19.', segment: 'Self-serve' },
        { date: '2026-10-06', title: 'Enterprise launch', text: 'The synthetic enterprise plan launched; active enterprise teams rose about 18% within a week.', segment: 'Enterprise' },
        { date: '2026-10-17', title: 'Partner program opens', text: 'Partner-sourced teams have grown about 2% a day since the synthetic partner program opened.', segment: 'Partners' },
      ],
    };
  }

  // ---- Shaping the snapshot

  function prepare(value) {
    var segments = Array.isArray(value.segments) ? value.segments.slice(0, 8) : [];
    var byDate = {};
    (value.series || []).forEach(function (row) {
      if (!row || typeof row.date !== 'string' || typeof row.value !== 'number') return;
      if (segments.indexOf(row.segment) === -1) segments.push(row.segment);
      (byDate[row.date] = byDate[row.date] || {})[row.segment] = row.value;
    });
    var dates = Object.keys(byDate).sort();
    return {
      title: value.title || 'Data story',
      summary: value.summary || '',
      source: value.source || '',
      synthetic: value.synthetic === true,
      capturedAt: value.capturedAt,
      metric: { name: (value.metric && value.metric.name) || 'Value', unit: (value.metric && value.metric.unit) || '', aggregate: value.metric && value.metric.aggregate === 'average' ? 'average' : 'sum' },
      segments: segments,
      dates: dates,
      values: byDate,
      annotations: (value.annotations || []).filter(function (note) { return note && byDate[note.date]; }).sort(function (a, b) { return a.date < b.date ? -1 : 1; }),
    };
  }
  function selectedSegments() { return data.segments.filter(function (segment) { return state.selected[segment]; }); }
  function colorOf(segment) { return COLORS[data.segments.indexOf(segment) % COLORS.length]; }
  function total(date, segments) {
    var row = data.values[date] || {};
    var sum = 0;
    segments.forEach(function (segment) { sum += row[segment] || 0; });
    return sum;
  }
  // Sum over the window, or the daily average when the metric is an average.
  function aggregate(fromIndex, toIndex, segments) {
    var sum = 0;
    var count = 0;
    for (var i = Math.max(0, fromIndex); i <= Math.min(toIndex, data.dates.length - 1); i += 1) {
      sum += total(data.dates[i], segments);
      count += 1;
    }
    if (!count) return null;
    return data.metric.aggregate === 'average' ? sum / count : sum;
  }
  function ref(date, segment) { return 'snapshots/' + doc.id + '#series/' + date + '/' + segment; }

  // ---- Refresh and its progress

  function refreshOpen() { return Boolean(refreshAction) && OPEN.indexOf(refreshAction.status) !== -1; }
  function renderRefresh() {
    var button = $('refresh');
    var box = $('run');
    box.replaceChildren();
    if (pending && Date.now() - pending.at > PENDING_MS) pending = null;
    button.hidden = !canRefresh;
    // A refresh that never reached a session (its New session was left) can be sent again.
    var undelivered = (pending && pending.starting) || (!pending && refreshAction && refreshAction.status === 'pending');
    button.disabled = !canRefresh || ((Boolean(pending) || refreshOpen()) && !undelivered);
    var open = Boolean(pending) || refreshOpen();
    if (open) box.appendChild(el('span', { class: 'spinner', 'aria-hidden': 'true' }));
    if (pending) {
      box.appendChild(el('span', { text: pending.text }));
    } else if (refreshAction && PROGRESS_TEXT[refreshAction.status]) {
      var summary = refreshAction.summary && (open || refreshAction.status === 'done' || refreshAction.status === 'failed') ? ': ' + refreshAction.summary : '';
      box.appendChild(el('span', { text: PROGRESS_TEXT[refreshAction.status] + summary }));
    }
  }
  function onActions(snapshot) {
    if (snapshot.status === 'loading') return;
    var mine = snapshot.items.filter(function (item) { return item.action === 'refresh'; });
    refreshAction = mine.length ? mine[mine.length - 1] : null;
    if (refreshAction) pending = null;
    renderRefresh();
  }
  function startRefresh() {
    pending = { at: Date.now(), text: 'Sending to your thingy session' + String.fromCharCode(8230) };
    renderRefresh();
    client.agent.request('refresh', {}).then(function (receipt) {
      var starting = Boolean(receipt && receipt.delivery === 'starting');
      var text = starting ? 'Start your thingy session below this page to refresh.' : 'Sent to your thingy session.';
      pending = { at: Date.now(), text: text, starting: starting };
      if (actionWatch) actionWatch.refresh();
      renderRefresh();
    }, function (error) {
      pending = null;
      renderRefresh();
      notice('Could not send the refresh to your thingy session: ' + (error && error.message ? error.message : String(error)), 'error');
    });
  }

  function notice(text, kind) {
    var box = $('notice');
    box.hidden = !text;
    box.textContent = text || '';
    if (kind) box.setAttribute('data-kind', kind); else box.removeAttribute('data-kind');
  }

  // ---- Rendering

  function render() {
    if (!data) return;
    $('title').textContent = data.title;
    var meta = $('meta');
    meta.replaceChildren(
      el('span', { text: 'Last refreshed ' + when(data.capturedAt) }),
      data.source ? el('span', { text: data.source }) : null,
      data.synthetic ? el('span', { class: 'badge', text: 'Synthetic sample data' }) : null
    );
    $('story').textContent = data.summary;
    $('story').hidden = !data.summary;
    renderFilters();
    renderHeadline();
    renderChart();
    renderNotes();
    renderBreakdown();
    renderDrill();
    $('content').hidden = false;
    $('loading').hidden = true;
  }

  function setRange(preset) {
    var last = data.dates.length - 1;
    var days = preset === '30' ? 30 : preset === '60' ? 60 : preset === '90' ? 90 : data.dates.length;
    state.preset = preset;
    state.from = Math.max(0, last - days + 1);
    state.to = last;
    state.cursor = -1;
    render();
  }

  function renderFilters() {
    var chips = $('segments');
    chips.replaceChildren();
    data.segments.forEach(function (segment) {
      chips.appendChild(el('button', {
        type: 'button', class: 'quiet chip', 'aria-pressed': state.selected[segment] ? 'true' : 'false',
        on: { click: function () {
          state.selected[segment] = !state.selected[segment];
          if (!selectedSegments().length) state.selected[segment] = true;
          render();
        } },
      }, [el('span', { class: 'swatch', style: 'background:' + colorOf(segment), 'aria-hidden': 'true' }), segment]));
    });
    var ranges = $('ranges');
    ranges.replaceChildren();
    [['30', 'Last 30 days'], ['60', '60 days'], ['90', '90 days'], ['all', 'All']].forEach(function (pair) {
      ranges.appendChild(el('button', { type: 'button', class: 'quiet', 'aria-pressed': state.preset === pair[0] ? 'true' : 'false', text: pair[1], on: { click: function () { setRange(pair[0]); } } }));
    });
    if (state.preset === 'zoom') {
      ranges.appendChild(el('button', { type: 'button', class: 'quiet', 'aria-pressed': 'true', text: 'Zoomed: ' + shortDate(data.dates[state.from]) + ' ' + DASH + ' ' + shortDate(data.dates[state.to]) + '  ' + String.fromCharCode(215), 'aria-label': 'Reset zoom', on: { click: function () { setRange('all'); } } }));
    }
  }

  function renderHeadline() {
    var segments = selectedSegments();
    var span = Math.min(30, state.to - state.from + 1);
    var current = aggregate(state.to - span + 1, state.to, segments);
    var previous = state.to - 2 * span + 1 >= 0 ? aggregate(state.to - 2 * span + 1, state.to - span, segments) : null;
    $('metric-name').textContent = data.metric.name + (data.metric.aggregate === 'average' ? ', daily average' : '') + ', last ' + span + ' days to ' + shortDate(data.dates[state.to]);
    $('big').textContent = fmt(current);
    var delta = $('delta');
    if (previous) {
      var change = (current - previous) / previous;
      delta.setAttribute('data-dir', change >= 0 ? 'up' : 'down');
      delta.textContent = (change >= 0 ? UP : DOWN) + ' ' + pct(change) + ' vs the ' + span + ' days before';
    } else {
      delta.removeAttribute('data-dir');
      delta.textContent = 'Not enough earlier data to compare.';
    }
    var spark = $('spark');
    spark.replaceChildren();
    var width = 260;
    var height = 64;
    var values = [];
    for (var i = state.from; i <= state.to; i += 1) values.push(total(data.dates[i], segments));
    var max = Math.max.apply(null, values.concat([1]));
    var min = Math.min.apply(null, values);
    var d = values.map(function (value, index) {
      var x = values.length > 1 ? (index / (values.length - 1)) * width : 0;
      var y = height - 4 - ((value - min) / Math.max(1, max - min)) * (height - 8);
      return (index ? 'L' : 'M') + x.toFixed(1) + ' ' + y.toFixed(1);
    }).join(' ');
    var svgNode = svg('svg', { viewBox: '0 0 ' + width + ' ' + height, role: 'img', 'aria-label': 'Trend of ' + data.metric.name + ' over the selected range' }, [
      svg('path', { d: d, fill: 'none', stroke: 'var(--accent)', 'stroke-width': 2 }),
    ]);
    spark.appendChild(svgNode);
  }

  // The time series: one line per selected segment, annotation markers, a hover and keyboard
  // cursor, and brushing to zoom a range.
  var chartBox = { width: 880, height: 320, left: 48, right: 12, top: 28, bottom: 28 };

  function xOf(index) {
    var span = Math.max(1, state.to - state.from);
    return chartBox.left + ((index - state.from) / span) * (chartBox.width - chartBox.left - chartBox.right);
  }
  function indexAt(x) {
    var span = Math.max(1, state.to - state.from);
    var ratio = (x - chartBox.left) / (chartBox.width - chartBox.left - chartBox.right);
    return Math.max(state.from, Math.min(state.to, Math.round(state.from + ratio * span)));
  }

  function renderChart() {
    var segments = selectedSegments();
    var max = 1;
    for (var i = state.from; i <= state.to; i += 1) {
      segments.forEach(function (segment) { max = Math.max(max, (data.values[data.dates[i]] || {})[segment] || 0); });
    }
    max = niceMax(max);
    var plotHeight = chartBox.height - chartBox.top - chartBox.bottom;
    function yOf(value) { return chartBox.top + plotHeight - (value / max) * plotHeight; }

    var grid = svg('g', { class: 'grid' });
    var axis = svg('g', { class: 'axis' });
    for (var t = 0; t <= 4; t += 1) {
      var value = (max / 4) * t;
      var y = yOf(value);
      grid.appendChild(svg('line', { x1: chartBox.left, x2: chartBox.width - chartBox.right, y1: y, y2: y }));
      axis.appendChild(svg('text', { x: chartBox.left - 8, y: y + 4, 'text-anchor': 'end', text: fmt(value) }));
    }
    var ticks = 6;
    for (var k = 0; k <= ticks; k += 1) {
      var index = Math.round(state.from + ((state.to - state.from) * k) / ticks);
      axis.appendChild(svg('text', { x: xOf(index), y: chartBox.height - 8, 'text-anchor': 'middle', text: shortDate(data.dates[index]) }));
    }

    var lines = svg('g', { class: 'series' });
    var points = svg('g', { class: 'points' });
    segments.forEach(function (segment) {
      var d = '';
      for (var i = state.from; i <= state.to; i += 1) {
        var date = data.dates[i];
        var v = (data.values[date] || {})[segment];
        if (v === undefined) continue;
        d += (d ? 'L' : 'M') + xOf(i).toFixed(1) + ' ' + yOf(v).toFixed(1);
        points.appendChild(svg('circle', {
          cx: xOf(i).toFixed(1), cy: yOf(v).toFixed(1), r: 4, style: 'color:' + colorOf(segment),
          'data-omnigent-ref': ref(date, segment),
          'data-omnigent-label': 'Point ' + shortDate(date) + ', ' + segment + ': ' + fmt(v),
        }, [svg('title', { text: segment + ' on ' + longDate(date) + ': ' + fmt(v) })]));
      }
      var dim = state.drill && state.drill !== segment;
      lines.appendChild(svg('path', { d: d, stroke: colorOf(segment), 'data-dim': dim ? 'true' : 'false' }));
    });

    var notes = svg('g', {});
    data.annotations.forEach(function (note, n) {
      var index = data.dates.indexOf(note.date);
      if (index < state.from || index > state.to) return;
      var x = xOf(index);
      notes.appendChild(svg('line', { class: 'note-line', x1: x, x2: x, y1: chartBox.top - 6, y2: chartBox.top + plotHeight }));
      var dot = svg('circle', {
        class: 'note-dot', cx: x, cy: chartBox.top - 14, r: 10, tabindex: 0, role: 'button',
        'data-active': state.note === n ? 'true' : 'false',
        'aria-label': 'Note ' + (n + 1) + ': ' + note.title + ', ' + longDate(note.date),
        'data-omnigent-ref': 'snapshots/' + doc.id + '#annotations/' + n,
        'data-omnigent-label': 'Note ' + (n + 1) + ' (' + shortDate(note.date) + '): ' + note.title,
        on: {
          click: function () { focusNote(n); },
          keydown: function (event) { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); focusNote(n); } },
        },
      }, [svg('title', { text: note.title })]);
      notes.appendChild(dot);
      notes.appendChild(svg('text', { class: 'note-num', x: x, y: chartBox.top - 10, 'text-anchor': 'middle', text: String(n + 1) }));
    });

    var overlay = svg('rect', { x: chartBox.left, y: chartBox.top, width: chartBox.width - chartBox.left - chartBox.right, height: plotHeight, fill: 'transparent' });
    var brushRect = svg('rect', { class: 'brush', x: 0, y: chartBox.top, width: 0, height: plotHeight, visibility: 'hidden' });
    var cursor = svg('line', { class: 'cursor-line', x1: 0, x2: 0, y1: chartBox.top, y2: chartBox.top + plotHeight, visibility: 'hidden' });

    var root = svg('svg', {
      viewBox: '0 0 ' + chartBox.width + ' ' + chartBox.height, tabindex: 0, role: 'group',
      'aria-label': data.metric.name + ' by day for ' + segments.join(', ') + '. Use the left and right arrow keys to read values, and drag across the chart to zoom.',
      'aria-describedby': 'readout',
    }, [grid, axis, lines, cursor, brushRect, overlay, points, notes]);

    function local(event) {
      var rect = root.getBoundingClientRect();
      return ((event.clientX - rect.left) / Math.max(1, rect.width)) * chartBox.width;
    }
    function showCursor(index) {
      state.cursor = index;
      var x = xOf(index);
      cursor.setAttribute('x1', x);
      cursor.setAttribute('x2', x);
      cursor.setAttribute('visibility', 'visible');
      var date = data.dates[index];
      $('readout').textContent = longDate(date) + ': ' + segments.map(function (segment) { return segment + ' ' + fmt((data.values[date] || {})[segment]); }).join('  ' + MIDDOT + '  ');
    }
    overlay.addEventListener('pointermove', function (event) {
      var x = local(event);
      if (brush) {
        brush.x1 = Math.max(chartBox.left, Math.min(chartBox.width - chartBox.right, x));
        brushRect.setAttribute('x', Math.min(brush.x0, brush.x1));
        brushRect.setAttribute('width', Math.abs(brush.x1 - brush.x0));
      }
      showCursor(indexAt(x));
    });
    overlay.addEventListener('pointerleave', function () { if (!brush) cursor.setAttribute('visibility', 'hidden'); });
    overlay.addEventListener('pointerdown', function (event) {
      if (event.button !== 0 || event.altKey) return;
      var x = local(event);
      brush = { x0: x, x1: x };
      overlay.setPointerCapture(event.pointerId);
      brushRect.setAttribute('visibility', 'visible');
    });
    function endBrush() {
      if (!brush) return;
      var a = indexAt(Math.min(brush.x0, brush.x1));
      var b = indexAt(Math.max(brush.x0, brush.x1));
      var wide = Math.abs(brush.x1 - brush.x0) > 8 && b - a >= 2;
      brush = null;
      brushRect.setAttribute('visibility', 'hidden');
      if (wide) {
        state.from = a;
        state.to = b;
        state.preset = 'zoom';
        render();
      }
    }
    overlay.addEventListener('pointerup', endBrush);
    overlay.addEventListener('pointercancel', endBrush);
    root.addEventListener('keydown', function (event) {
      if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
        event.preventDefault();
        var next = state.cursor < 0 ? state.to : state.cursor + (event.key === 'ArrowLeft' ? -1 : 1);
        showCursor(Math.max(state.from, Math.min(state.to, next)));
      } else if (event.key === 'Escape' && state.preset === 'zoom') {
        setRange('all');
      }
    });
    var box = $('chart');
    box.replaceChildren(root);
    if (state.cursor >= state.from && state.cursor <= state.to) showCursor(state.cursor);
    else $('readout').textContent = 'Hover or use the arrow keys to read a day. Drag across the chart to zoom in. Alt+click a point to ask about it.';
  }

  function niceMax(value) {
    var power = Math.pow(10, Math.floor(Math.log(value) / Math.LN10));
    var steps = [1, 2, 2.5, 5, 10];
    for (var i = 0; i < steps.length; i += 1) if (steps[i] * power >= value) return steps[i] * power;
    return value;
  }

  function focusNote(n) {
    state.note = state.note === n ? -1 : n;
    renderChart();
    renderNotes();
    var item = document.querySelector('[data-note="' + n + '"]');
    if (item && state.note === n) item.scrollIntoView({ block: 'nearest', behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
  }

  function renderNotes() {
    var list = $('notes');
    list.replaceChildren();
    $('notes-empty').hidden = data.annotations.length > 0;
    data.annotations.forEach(function (note, n) {
      list.appendChild(el('li', {
        'data-note': n, 'data-active': state.note === n ? 'true' : 'false',
        'data-omnigent-ref': 'snapshots/' + doc.id + '#annotations/' + n,
        'data-omnigent-label': 'Note ' + (n + 1) + ' (' + shortDate(note.date) + '): ' + note.title,
      }, [
        el('span', { class: 'num', 'aria-hidden': 'true', text: String(n + 1) }),
        el('div', {}, [
          el('strong', { text: note.title }),
          el('div', { class: 'muted', text: longDate(note.date) + (note.segment ? ' ' + MIDDOT + ' ' + note.segment : '') }),
          note.text ? el('p', { style: 'margin:6px 0 0', text: note.text }) : null,
        ]),
      ]));
    });
  }

  function renderBreakdown() {
    var bars = $('bars');
    bars.replaceChildren();
    var rows = data.segments.map(function (segment) { return { segment: segment, value: aggregate(state.from, state.to, [segment]) || 0 }; });
    var max = Math.max.apply(null, rows.map(function (row) { return row.value; }).concat([1]));
    $('breakdown-range').textContent = (data.metric.aggregate === 'average' ? 'Daily average' : 'Total') + ', ' + longDate(data.dates[state.from]) + ' ' + DASH + ' ' + longDate(data.dates[state.to]) + '. Choose a segment to see its days.';
    rows.sort(function (a, b) { return b.value - a.value; }).forEach(function (row) {
      bars.appendChild(el('button', {
        type: 'button', class: 'bar', 'aria-pressed': state.drill === row.segment ? 'true' : 'false',
        'aria-label': row.segment + ': ' + fmt(row.value) + '. Show its days.',
        'data-omnigent-ref': 'snapshots/' + doc.id + '#segments/' + row.segment,
        'data-omnigent-label': row.segment + ', ' + shortDate(data.dates[state.from]) + ' ' + DASH + ' ' + shortDate(data.dates[state.to]) + ': ' + fmt(row.value),
        on: { click: function () { state.drill = state.drill === row.segment ? null : row.segment; state.sort = 'date'; render(); } },
      }, [
        el('span', { class: 'chip' }, [el('span', { class: 'swatch', style: 'background:' + colorOf(row.segment), 'aria-hidden': 'true' }), row.segment]),
        el('span', { class: 'bar-track', 'aria-hidden': 'true' }, [el('span', { class: 'bar-fill', style: 'display:block;width:' + ((row.value / max) * 100).toFixed(1) + '%;background:' + colorOf(row.segment) })]),
        el('span', { class: 'bar-value', text: fmt(row.value) }),
      ]));
    });
  }

  function renderDrill() {
    var box = $('drill');
    box.replaceChildren();
    box.hidden = !state.drill;
    if (!state.drill) return;
    var segment = state.drill;
    var rows = [];
    for (var i = state.from; i <= state.to; i += 1) {
      var date = data.dates[i];
      var value = (data.values[date] || {})[segment];
      if (value === undefined) continue;
      var weekBefore = i - 7 >= 0 ? (data.values[data.dates[i - 7]] || {})[segment] : undefined;
      rows.push({ date: date, value: value, change: weekBefore ? (value - weekBefore) / weekBefore : null });
    }
    if (state.sort === 'value') rows.sort(function (a, b) { return b.value - a.value; });
    else if (state.sort === 'change') rows.sort(function (a, b) { return (a.change === null ? 1 : b.change === null ? -1 : a.change - b.change); });
    function sortButton(key, label) {
      return el('button', { type: 'button', 'aria-pressed': state.sort === key ? 'true' : 'false', text: label + (state.sort === key ? ' ' + DOWN : ''), on: { click: function () { state.sort = key; renderDrill(); } } });
    }
    var table = el('table', {}, [
      el('caption', { class: 'muted', style: 'text-align:left;caption-side:top', text: segment + ' by day. Sort by a column; Alt+click a row to ask about that day.' }),
      el('thead', {}, [el('tr', {}, [
        el('th', { scope: 'col' }, [sortButton('date', 'Date')]),
        el('th', { scope: 'col', class: 'n' }, [sortButton('value', data.metric.name)]),
        el('th', { scope: 'col', class: 'n' }, [sortButton('change', 'vs a week before')]),
      ])]),
      el('tbody', {}, rows.map(function (row) {
        return el('tr', {
          'data-omnigent-ref': ref(row.date, segment),
          'data-omnigent-label': segment + ' on ' + shortDate(row.date) + ': ' + fmt(row.value),
        }, [
          el('td', { text: longDate(row.date) }),
          el('td', { class: 'n', text: fmt(row.value) }),
          el('td', { class: 'n', text: row.change === null ? DASH : pct(row.change) }),
        ]);
      })),
    ]);
    box.appendChild(el('div', { style: 'display:flex;justify-content:space-between;align-items:center;gap:12px;margin-top:20px' }, [
      el('h2', { text: segment + ', day by day' }),
      el('button', { type: 'button', class: 'quiet', text: 'Back to all segments', on: { click: function () { state.drill = null; render(); } } }),
    ]));
    box.appendChild(table);
  }

  // ---- Data

  function onSnapshots(snapshot) {
    if (snapshot.status === 'stale' && snapshot.error) notice('Could not load the latest numbers: ' + snapshot.error.message, 'error');
    if (snapshot.status === 'loading') return;
    var best = null;
    snapshot.items.forEach(function (item) {
      if (item && item.value && typeof item.value.capturedAt === 'string' && Array.isArray(item.value.series) && (!best || item.value.capturedAt > best.value.capturedAt)) best = item;
    });
    if (!best) {
      if (snapshot.status === 'live' && canWrite && !seeded) {
        seeded = true;
        seeding = true;
        var sample = sampleSnapshot();
        // Show the sample as soon as it is saved, not at the next poll.
        snapshots.create(sample, { id: 'sample-synthetic', idempotencyKey: 'seed-sample-synthetic' }).then(function (created) {
          seeding = false;
          if (!doc && created && typeof created.id === 'string') {
            onSnapshots({ status: 'live', items: [{ id: created.id, version: created.version, value: created.value || sample }] });
          }
        }, function (error) {
          seeding = false;
          if (!doc) {
            $('loading').hidden = true;
            $('empty').hidden = false;
          }
          if (!error || error.code !== 'CONFLICT') notice('Could not add the sample data: ' + (error && error.message ? error.message : String(error)), 'error');
        });
      }
      // While the sample is being added, the loading state stays instead of a brief empty one.
      if (!seeding) {
        $('loading').hidden = true;
        $('empty').hidden = false;
      }
      return;
    }
    $('empty').hidden = true;
    var changed = !doc || doc.id !== best.id || doc.version !== best.version;
    if (!changed) return;
    var first = !data;
    doc = best;
    data = prepare(best.value);
    if (first || !state.selected) {
      state.selected = {};
      data.segments.forEach(function (segment) { state.selected[segment] = true; });
    } else {
      data.segments.forEach(function (segment) { if (state.selected[segment] === undefined) state.selected[segment] = true; });
    }
    if (state.drill && data.segments.indexOf(state.drill) === -1) state.drill = null;
    if (state.note >= data.annotations.length) state.note = -1;
    if (!data.dates.length) {
      $('loading').hidden = true;
      $('empty').hidden = false;
      return;
    }
    if (first || state.preset !== 'zoom' || state.to >= data.dates.length) setRange(state.preset === 'zoom' ? 'all' : state.preset);
    else render();
  }

  window.omnigent.connect().then(function (connected) {
    client = connected;
    document.body.setAttribute('data-theme', client.ui.colorScheme);
    canWrite = client.hasCapability('data.write') && client.context.permissions.writeData;
    canRefresh = canWrite && client.context.permissions.publish === true && Boolean(client.agent);
    preview = client.context.preview === true;
    // A template preview shows its sample without access notices.
    if (preview) notice('');
    else if (!canWrite) notice('You can read this story. Ask the thingy owner for edit access to refresh it.');
    else if (!canRefresh) notice('Refreshing runs in your thingy session, which needs Can edit thingy access. Ask the thingy owner for it.');
    $('refresh').addEventListener('click', startRefresh);
    renderRefresh();
    snapshots = client.data.collection('snapshots');
    snapshots.watch(onSnapshots, { intervalMs: 5000 });
    if (canRefresh && typeof client.agent.watch === 'function') actionWatch = client.agent.watch(onActions, { intervalMs: 3000 });
  }, function (error) {
    $('loading').hidden = true;
    notice('The thingy could not connect to Omnigent: ' + (error && error.message ? error.message : String(error)), 'error');
  });
})();
`;

const BODY = `
<main>
  <p class="kicker">Data story</p>
  <h1 id="title">Data story</h1>
  <div class="meta" id="meta"></div>
  <div class="toolbar">
    <button type="button" id="refresh" class="primary" disabled hidden>Refresh</button>
    <span id="run" class="status" aria-live="polite"></span>
  </div>
  <p id="notice" class="notice" role="status" hidden></p>
  <div id="loading" aria-label="Loading the story">
    <div class="skeleton" style="width:60%"></div>
    <div class="skeleton" style="width:90%"></div>
    <div class="skeleton" style="width:75%"></div>
  </div>
  <p id="empty" class="notice" hidden>No numbers yet. Click Refresh to compute them, or ask your thingy session to connect this story to a table.</p>
  <div id="content" hidden>
    <p id="story" class="story"></p>
    <section aria-labelledby="headline-heading">
      <h2 id="headline-heading" class="kicker">The headline</h2>
      <div class="headline">
        <div>
          <div id="metric-name" class="muted"></div>
          <div id="big" class="big"></div>
          <div id="delta" class="delta"></div>
        </div>
        <div id="spark"></div>
      </div>
    </section>
    <section aria-labelledby="trend-heading">
      <h2 id="trend-heading">The trend, with what happened</h2>
      <div class="filters" role="group" aria-label="Segments" id="segments"></div>
      <div class="filters" role="group" aria-label="Date range" id="ranges"></div>
      <div class="chart">
        <div id="chart"></div>
        <div id="readout" class="readout" aria-live="polite"></div>
      </div>
      <ol class="notes" id="notes" aria-label="What happened"></ol>
      <p id="notes-empty" class="muted" hidden>No notes yet. Your thingy session adds them when it refreshes the numbers.</p>
    </section>
    <section aria-labelledby="breakdown-heading">
      <h2 id="breakdown-heading">Where it comes from</h2>
      <p id="breakdown-range" class="muted" style="margin:0"></p>
      <div class="bars" id="bars"></div>
      <div id="drill" hidden></div>
    </section>
  </div>
  <footer>Select any text, or Alt+click a point, bar, note or row, to reply about it or ask in a side chat.</footer>
</main>
`;

export const DATA_STORY_HTML = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="omnigent-template" content="data-story">
<title>Data story</title>
<style>${STYLE}</style>
</head>
<body>
${BODY}
<script>${SCRIPT}</script>
</body>
</html>
`;

export function isDataStoryHtml(html: string): boolean {
  return html.includes('<meta name="omnigent-template" content="data-story">');
}

export const DATA_STORY_GUIDE = [
  "# Data story",
  "",
  "A scrolling report about one metric: a headline number with its trend, a daily chart with notes on what changed, and a breakdown by segment with a day-by-day table. Everything it shows comes from one record in the `snapshots` collection; the page reads the record with the newest `capturedAt`.",
  "",
  "## The snapshots collection",
  "",
  "One record is one computed dataset, written in a single create so the page never shows half an update. A record is at most 64 KiB: keep `series` to about 1,000 rows (for example 120 days by 8 segments).",
  "",
  '- `capturedAt`: ISO 8601 UTC time the numbers were computed; shown as "Last refreshed".',
  '- `title`: the story headline, written as a finding ("Active teams grew 12% this quarter"), not a label.',
  "- `summary`: two to four plain sentences that tell the story in the numbers. Name the biggest change and its likely reason.",
  "- `source`: where the numbers come from, for example the table name and filter.",
  "- `metric`: `{name, unit, aggregate}`. `aggregate` is `sum` (totals add up over days, like revenue) or `average` (a daily level, like active users).",
  "- `segments`: the segment names in display order (1 to 8).",
  '- `series`: rows `{date: "YYYY-MM-DD", segment, value}`, one per day and segment.',
  "- `annotations`: up to 20 notes `{date, title, text, segment}` drawn on the chart. Each `date` must be a date in `series`.",
  "- `synthetic`: true only for sample data.",
  "",
  "Records with ID `sample-synthetic` hold the synthetic sample the page seeds on first load. Leave it, or delete it once real snapshots exist.",
  "",
  "## Refresh",
  "",
  "The Refresh button requests the `refresh` action. Recompute the dataset and create a new `snapshots` record with ID `snap-<UTC yyyymmddhhmm>`:",
  "",
  "1. Use the SQL table the viewer named for this story (ask in the session if none was named yet, and record it in `source`). Until one is named, refresh the synthetic sample: copy it with a new `capturedAt` and keep `synthetic: true`.",
  "2. Query one row per day and segment for the last 90 days (or the range the viewer asked for), with the metric as `value`.",
  "3. Write notes for the notable changes: a step change, a dip and recovery, a new high, or a segment that moved against the rest. Use the first day the change shows as `date`, a short title, and one or two sentences in `text` with the size of the change.",
  "4. Write the `title` and `summary` from what the notes say.",
  "",
  "The page shows the refresh as running while your session works on it, so finish the work in the session (or a child session) and report the result when done.",
  "",
  "## Answering questions about the chart",
  "",
  "Selections name what the viewer pointed at with refs such as `snapshots/<id>#series/2026-10-03/Enterprise` (one day of one segment), `#segments/<segment>`, or `#annotations/<index>`. Read that snapshot record to answer, and query the source table for detail when the record is not enough.",
].join("\n");

export const DATA_STORY_REVISION: CreateThingyRevisionRequest = {
  html: DATA_STORY_HTML,
  sdk_version: 1,
  capabilities: ["data.read", "data.write"],
  collections: { [DATA_STORY_COLLECTION]: DATA_STORY_SNAPSHOT_SCHEMA },
  guide: DATA_STORY_GUIDE,
  actions: [
    {
      name: "refresh",
      description:
        "Recompute the story. Query the SQL table the viewer named for it (ask if none was named; until then refresh " +
        "the synthetic sample with a new capturedAt and synthetic: true) for one row per day and segment over the last 90 " +
        "days. Create a new snapshots record with ID snap-<UTC yyyymmddhhmm> holding capturedAt, title, summary, source, " +
        "metric, segments, series and annotations as the guide describes, with short annotations on notable changes. " +
        "Optional params.table names the table to use.",
      params: {
        type: "object",
        additionalProperties: false,
        properties: { table: { type: "string", maxLength: 300 } },
      },
    },
  ],
  constraints: [],
};
