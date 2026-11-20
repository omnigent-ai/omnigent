// Reference thingy: a small kanban board uploaded as an ordinary revision. It is a
// single self-contained HTML document that uses only `window.omnigent`; it gets no
// host privileges the SDK does not give every app. Sample data is synthetic. It is not a
// template in the New thingy dialog; it stays as a full example of the SDK (collections with
// rules, drag and drop, per-record actions) that its tests exercise end to end.
//
// The script is plain ES2018 inside a template literal: keep it free of backticks,
// `${`, and backslashes. Characters that would need escapes are built with
// String.fromCharCode.

import type { CreateThingyRevisionRequest } from "../api/thingyApi";

export const REFERENCE_KANBAN_COLLECTION = "cards";

// Published apps keep each collection's schema identity, so this stays unchanged.
// Markdown card notes live in the additional `notes` property; `checklist` mirrors
// the task items in those notes so earlier revisions of the app still show them.
export const REFERENCE_KANBAN_CARD_SCHEMA = {
  type: "object",
  additionalProperties: true,
  required: ["title", "owner", "column", "order", "checklist"],
  properties: {
    title: { type: "string", minLength: 1, maxLength: 200 },
    owner: { type: "string", maxLength: 100 },
    column: { type: "string", minLength: 1, maxLength: 64 },
    order: { type: "number" },
    synthetic: { type: "boolean" },
    checklist: {
      type: "array",
      maxItems: 20,
      items: {
        type: "object",
        additionalProperties: false,
        required: ["text", "done"],
        properties: {
          text: { type: "string", minLength: 1, maxLength: 200 },
          done: { type: "boolean" },
        },
      },
    },
  },
} satisfies Record<string, unknown>;

export const REFERENCE_KANBAN_RUNS_COLLECTION = "runs";

// One record per session started from a card. The session writes it through the
// thingy MCP tools, so people editing the card never conflict with the agent.
export const REFERENCE_KANBAN_RUN_SCHEMA = {
  type: "object",
  additionalProperties: true,
  required: ["cardId", "state"],
  properties: {
    cardId: { type: "string", minLength: 1, maxLength: 128 },
    state: { type: "string", enum: ["running", "needs_input", "done", "failed"] },
    summary: { type: "string", maxLength: 4000 },
    sessionId: { type: "string", maxLength: 128 },
  },
} satisfies Record<string, unknown>;

const STYLE = `
:root { color-scheme: light; --bg: #f4f5f8; --panel: #ffffff; --raised: #ffffff; --text: #1d2733; --muted: #5d6978; --border: #d6dbe2; --accent: #2266d4; --accent-soft: rgba(34, 102, 212, 0.1); --danger: #c62828; --danger-soft: #fdecec; --warn-bg: #fff4d6; --skeleton: #e6e9ee; --code: #eef1f5; --shadow: 0 12px 28px rgba(20, 30, 45, 0.2); }
body[data-theme='dark'] { color-scheme: dark; --bg: #11161c; --panel: #1a2129; --raised: #212a34; --text: #e6e9ee; --muted: #9aa5b4; --border: #34404d; --accent: #6ea3ff; --accent-soft: rgba(110, 163, 255, 0.14); --danger: #ff8a80; --danger-soft: #3a2224; --warn-bg: #3b3220; --skeleton: #2a3440; --code: #262f3a; --shadow: 0 12px 28px rgba(0, 0, 0, 0.55); }
* { box-sizing: border-box; }
body { margin: 0; font: 14px/1.4 system-ui, -apple-system, 'Segoe UI', sans-serif; background: var(--bg); color: var(--text); }
header.top { display: flex; flex-direction: column; gap: 4px; padding: 12px 16px; border-bottom: 1px solid var(--border); background: var(--panel); }
.top-row { display: flex; flex-wrap: wrap; gap: 6px 12px; align-items: center; }
h1 { font-size: 18px; margin: 0; }
h2 { font-size: 13px; margin: 0; display: flex; align-items: center; gap: 6px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted); }
.muted { color: var(--muted); font-size: 12px; }
.status { font-size: 12px; padding: 1px 8px; border-radius: 10px; border: 1px solid var(--border); }
.status[data-status='live'] { border-color: var(--accent); }
.status[data-status='stale'], .status[data-status='reconnecting'] { background: var(--warn-bg); }
.sync { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--muted); }
.sync[data-state='error'] { color: var(--danger); }
#message { margin: 8px 16px 0; }
#message:empty { display: none; }
#message[data-kind='error'] { color: var(--danger); }
input, textarea, button { font: inherit; color: var(--text); background: var(--panel); border: 1px solid var(--border); border-radius: 4px; padding: 4px 8px; }
textarea { font: 13px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; resize: vertical; }
button { cursor: pointer; }
button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
button.danger { color: var(--danger); }
button.danger.confirm { background: var(--danger); border-color: var(--danger); color: #fff; }
button:disabled, input:disabled { opacity: 0.55; cursor: not-allowed; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.sr-only { position: absolute; width: 1px; height: 1px; margin: -1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0; padding: 0; }
.row { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.board { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 15rem), 1fr)); gap: 12px; padding: 12px 16px 24px; align-items: start; }
.column { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 10px 8px 8px; display: flex; flex-direction: column; gap: 8px; transition: background-color 120ms ease, border-color 120ms ease; }
.column.drop-target { border-color: var(--accent); background: var(--accent-soft); }
.count { font-variant-numeric: tabular-nums; font-weight: 600; color: var(--text); background: var(--bg); border-radius: 8px; padding: 0 6px; letter-spacing: 0; }
.cards { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; min-height: 40px; }
.empty { color: var(--muted); font-size: 12px; text-align: center; padding: 10px 0; border: 1px dashed var(--border); border-radius: 6px; }
.column.drop-target .empty { display: none; }
.card { position: relative; overflow: hidden; background: var(--raised); border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px; display: flex; flex-direction: column; gap: 6px; }
.board[data-can-write='true'] .card:not(.editing) { cursor: grab; }
.card-head { display: flex; gap: 6px; align-items: flex-start; }
.card h3 { font-size: 14px; margin: 0; min-width: 0; flex: 1; overflow-wrap: anywhere; }
.card-title { display: block; width: 100%; padding: 0; border: 0; background: none; font: inherit; font-weight: 600; text-align: left; color: inherit; cursor: pointer; overflow-wrap: anywhere; }
span.card-title { cursor: default; }
.grip { flex: none; width: 12px; height: 20px; margin: 0; padding: 0; border: 0; border-radius: 3px; opacity: 0.55; cursor: grab; touch-action: none; background: radial-gradient(circle, var(--muted) 1.25px, transparent 1.75px) 0 1px / 6px 6px; }
.grip:hover, .grip:focus-visible { opacity: 1; }
.owner { font-size: 12px; color: var(--muted); }
.sample { font-size: 11px; color: var(--muted); }
.add-notes { align-self: flex-start; padding: 0; border: 0; background: none; color: var(--muted); font-size: 12px; }
.add-notes:hover { color: var(--text); text-decoration: underline; }
.md { font-size: 13px; overflow-wrap: anywhere; display: flex; flex-direction: column; gap: 6px; }
.md > * { margin: 0; }
.md ul, .md ol { padding-left: 18px; display: flex; flex-direction: column; gap: 2px; }
.md ul.md-tasks { list-style: none; padding-left: 0; }
.md-task label { display: flex; align-items: flex-start; gap: 6px; cursor: pointer; }
.md-task input { margin: 2px 0 0; flex: none; }
.md-task.done span { color: var(--muted); text-decoration: line-through; }
.md h4 { font-size: 14px; } .md h5 { font-size: 13px; } .md h6 { font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted); }
.md code { font: 12px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; background: var(--code); border-radius: 3px; padding: 0 3px; }
.md pre { background: var(--code); border-radius: 4px; padding: 6px 8px; overflow-x: auto; }
.md pre code { padding: 0; background: none; }
.md blockquote { border-left: 3px solid var(--border); padding-left: 8px; color: var(--muted); }
.md hr { border: 0; border-top: 1px solid var(--border); width: 100%; }
.md-link { text-decoration: underline; }
.md-url { color: var(--muted); font-size: 12px; }
.card-status { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--muted); }
.run { display: flex; flex-direction: column; gap: 4px; padding-top: 6px; border-top: 1px dashed var(--border); font-size: 12px; }
.run-state { display: flex; align-items: center; gap: 6px; color: var(--muted); }
.run-state[data-run='done'] { color: var(--text); }
.run-state[data-run='failed'] { color: var(--danger); }
.run-state[data-run='needs_input'] { color: var(--text); font-weight: 600; }
.run .md { font-size: 12px; }
.run-actions { display: flex; flex-wrap: wrap; gap: 6px; }
.run-actions button { padding: 1px 8px; font-size: 12px; }
.card-error { background: var(--danger-soft); border-radius: 4px; padding: 6px 8px; font-size: 12px; display: flex; flex-direction: column; gap: 6px; cursor: default; }
.card-error p { margin: 0; color: var(--danger); }
.card-error button { font-size: 12px; padding: 2px 8px; }
.card[data-state='error'] { border-color: var(--danger); }
.card[data-state='deleting'] { opacity: 0.55; }
.card[data-state='deleting'] .card-title { text-decoration: line-through; }
.card[data-state='saving']::after, .card[data-state='deleting']::after { content: ''; position: absolute; left: 0; right: 0; top: 0; height: 2px; background: linear-gradient(90deg, transparent, var(--accent), transparent) no-repeat; background-size: 40% 100%; animation: sweep 1.1s ease-in-out infinite; }
.card[data-state='creating'], .skeleton { background: linear-gradient(100deg, var(--raised) 30%, var(--skeleton) 50%, var(--raised) 70%); background-size: 300% 100%; animation: shimmer 1.4s linear infinite; }
.skeleton { height: 64px; border-radius: 6px; border: 1px solid var(--border); }
.spinner { flex: none; width: 12px; height: 12px; border-radius: 50%; border: 2px solid var(--border); border-top-color: var(--accent); animation: spin 0.8s linear infinite; }
.card.editing { cursor: default; gap: 8px; border-color: var(--accent); }
.field { display: flex; flex-direction: column; gap: 3px; font-size: 12px; color: var(--muted); }
.field input, .field textarea { width: 100%; }
.field-hint { font-size: 11px; color: var(--muted); }
.field-error { margin: 0; font-size: 12px; color: var(--danger); }
.field-error:empty { display: none; }
.editor-actions { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.editor-actions .spacer { flex: 1; }
.composer { display: flex; flex-direction: column; gap: 6px; }
.add-trigger { text-align: left; border: 1px dashed transparent; background: none; color: var(--muted); }
.add-trigger:hover { border-color: var(--border); color: var(--text); }
.composer-form input { width: 100%; }
.drop-slot { border: 2px dashed var(--accent); border-radius: 6px; background: var(--accent-soft); }
.drag-source { display: none; }
.ghost { position: fixed; z-index: 50; margin: 0; list-style: none; pointer-events: none; box-shadow: var(--shadow); transform: rotate(1.5deg); }
.card.lifted { outline: 2px solid var(--accent); outline-offset: 2px; box-shadow: var(--shadow); }
body.dragging, body.dragging * { cursor: grabbing !important; user-select: none; }
@keyframes spin { to { transform: rotate(360deg); } }
@keyframes sweep { from { background-position: -40% 0; } to { background-position: 140% 0; } }
@keyframes shimmer { from { background-position: 100% 0; } to { background-position: 0 0; } }
@media (prefers-reduced-motion: reduce) {
  *, ::before, ::after { animation: none !important; transition: none !important; }
  .ghost { transform: none; }
}
`;

const SCRIPT = `
(function () {
  'use strict';
  var NL = String.fromCharCode(10);
  var CR = String.fromCharCode(13);
  var TAB = String.fromCharCode(9);
  var TICK = String.fromCharCode(96);
  var FENCE = TICK + TICK + TICK;
  var NOTES_LIMIT = 6000;
  var SAVED_OVERLAY_MS = 30000;
  var UNKNOWN_OUTCOME = ['DEADLINE_EXCEEDED', 'UNAVAILABLE', 'CANCELLED'];
  var COLUMNS = [
    { id: 'todo', title: 'Backlog' },
    { id: 'doing', title: 'In progress' },
    { id: 'review', title: 'Review' },
    { id: 'done', title: 'Done' },
  ];
  var SAMPLE_CARDS = [
    { title: 'Sample: draft the onboarding checklist', owner: 'Example owner A (synthetic)', column: 'todo', notes: ['First-week plan for a **synthetic** new hire.', '', '- [ ] List first-week tasks', '- [ ] Ask a teammate to review'].join(NL) },
    { title: 'Sample: review the demo dataset', owner: 'Example owner B (synthetic)', column: 'doing', notes: ['- [x] Check column names', '- [ ] Note open questions', '', '> Synthetic rows only.'].join(NL) },
    { title: 'Sample: publish the weekly summary', owner: 'Example owner C (synthetic)', column: 'done', notes: ['- [x] Write summary', '', 'Shared with the _synthetic_ team.'].join(NL) },
  ];

  var client = null;
  var cards = null;
  var runs = null;
  // Run records written by sessions (their summary), requests on their way, and the progress
  // Omnigent derives from the sessions doing the work, which is what the run state shows.
  var runDocs = [];
  var pendingRuns = {};
  var actionItems = [];
  var actionWatch = null;
  var ACTION_LABELS = {
    pending: 'Start your thingy session beside the board to run this card.',
    queued: 'Waiting for your thingy session',
    running: 'Running in your thingy session',
    needs_input: 'Waiting for your input in the session',
    done: 'Done',
    failed: 'Failed',
    cancelled: 'Cancelled',
    finished_unreported: 'Finished. The session did not report a result.',
    interrupted: 'Stopped before it finished. Run it again.',
  };
  var OPEN_ACTION_STATUSES = ['pending', 'queued', 'running', 'needs_input'];
  var PENDING_RUN_MS = 10 * 60 * 1000;
  var RUN_LABELS = { running: 'Session running', needs_input: 'Session needs input', done: 'Session done', failed: 'Session failed' };
  var canWrite = false;
  // Actions run in the viewer's thingy session, which needs edit access to the thingy itself.
  var canEdit = false;
  var watchStatus = 'loading';
  // Last page read from the server, and local changes that are saving, failed, or
  // saved but not yet visible in a later read. Local changes are drawn over the server
  // copy so every action shows at once.
  var serverDocs = [];
  var local = {};
  var tempSeq = 0;
  var columnSeq = 0;
  var anyChange = false;
  var preview = false;
  var previewSeeded = false;
  var editing = {};
  var columnNodes = {};
  var cardNodes = {};
  var renderQueued = false;
  var renderAfterMove = false;
  var focusAfterRender = null;
  var drag = null;
  var keyMove = null;
  var suppressClick = false;

  function el(tag, props, children) {
    var node = document.createElement(tag);
    Object.keys(props || {}).forEach(function (key) {
      var value = props[key];
      if (key === 'text') {
        node.textContent = value;
      } else if (key === 'on') {
        Object.keys(value).forEach(function (type) {
          node.addEventListener(type, value[type]);
        });
      } else if (key === 'disabled' || key === 'checked' || key === 'hidden' || key === 'required') {
        node[key] = Boolean(value);
      } else if (key === 'value') {
        node.value = value;
      } else if (value !== null && value !== undefined) {
        node.setAttribute(key, value);
      }
    });
    append(node, children);
    return node;
  }

  function append(node, child) {
    if (!child) return;
    if (Array.isArray(child)) {
      child.forEach(function (item) { append(node, item); });
    } else {
      node.appendChild(child);
    }
  }

  function clone(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function showMessage(text, kind) {
    var box = document.getElementById('message');
    box.textContent = text || '';
    box.setAttribute('data-kind', kind || 'info');
  }

  function announce(text) {
    document.getElementById('announce').textContent = text;
  }

  // ---- Markdown: a small, text-only subset. Nodes are built with textContent, so
  // card notes can never inject markup or script.

  function normalize(text) {
    return String(text || '').split(CR).join('');
  }

  function ltrim(text) {
    var index = 0;
    while (index < text.length && (text.charAt(index) === ' ' || text.charAt(index) === TAB)) index++;
    return text.slice(index);
  }

  function isWordChar(ch) {
    var lower = ch.toLowerCase();
    return (lower >= 'a' && lower <= 'z') || (lower >= '0' && lower <= '9');
  }

  function listMarker(text) {
    var first = text.charAt(0);
    if ((first === '-' || first === '*' || first === '+') && text.charAt(1) === ' ') {
      return { ordered: false, rest: text.slice(2) };
    }
    var index = 0;
    while (index < text.length && index < 9 && text.charCodeAt(index) >= 48 && text.charCodeAt(index) <= 57) index++;
    if (index > 0 && (text.charAt(index) === '.' || text.charAt(index) === ')') && text.charAt(index + 1) === ' ') {
      return { ordered: true, rest: text.slice(index + 2), start: Number(text.slice(0, index)) };
    }
    return null;
  }

  function taskOf(rest) {
    var head = rest.slice(0, 3);
    if ((head === '[ ]' || head === '[x]' || head === '[X]') && (rest.length === 3 || rest.charAt(3) === ' ')) {
      return { done: head !== '[ ]', text: rest.slice(4) };
    }
    return null;
  }

  function headingOf(text) {
    var level = 0;
    while (level < 3 && text.charAt(level) === '#') level++;
    if (level > 0 && text.charAt(level) === ' ') return { level: level, text: text.slice(level + 1) };
    return null;
  }

  function inline(text) {
    var out = [];
    var buffer = '';
    var index = 0;
    function flush() {
      if (buffer) {
        out.push(document.createTextNode(buffer));
        buffer = '';
      }
    }
    while (index < text.length) {
      var ch = text.charAt(index);
      if (ch === TICK) {
        var codeEnd = text.indexOf(TICK, index + 1);
        if (codeEnd > index + 1) {
          flush();
          out.push(el('code', { text: text.slice(index + 1, codeEnd) }));
          index = codeEnd + 1;
          continue;
        }
      }
      var pair = text.slice(index, index + 2);
      if (pair === '**' || pair === '__' || pair === '~~') {
        var pairEnd = text.indexOf(pair, index + 2);
        if (pairEnd > index + 2 && text.charAt(index + 2) !== ' ') {
          flush();
          out.push(el(pair === '~~' ? 's' : 'strong', {}, inline(text.slice(index + 2, pairEnd))));
          index = pairEnd + 2;
          continue;
        }
      }
      if ((ch === '*' || ch === '_') && (index === 0 || !isWordChar(text.charAt(index - 1))) && text.charAt(index + 1) && text.charAt(index + 1) !== ' ' && text.charAt(index + 1) !== ch) {
        var emEnd = text.indexOf(ch, index + 1);
        while (emEnd > 0 && (text.charAt(emEnd - 1) === ' ' || isWordChar(text.charAt(emEnd + 1)))) {
          emEnd = text.indexOf(ch, emEnd + 1);
        }
        if (emEnd > index + 1) {
          flush();
          out.push(el('em', {}, inline(text.slice(index + 1, emEnd))));
          index = emEnd + 1;
          continue;
        }
      }
      if (ch === '[') {
        var labelEnd = text.indexOf('](', index + 1);
        var urlEnd = labelEnd > index ? text.indexOf(')', labelEnd + 2) : -1;
        if (labelEnd > index + 1 && urlEnd > labelEnd + 2) {
          flush();
          var url = text.slice(labelEnd + 2, urlEnd);
          // Apps cannot open other pages, so links show their address instead.
          out.push(el('span', { class: 'md-link', title: url }, inline(text.slice(index + 1, labelEnd))));
          out.push(el('span', { class: 'md-url', text: ' (' + url + ')' }));
          index = urlEnd + 1;
          continue;
        }
      }
      buffer += ch;
      index++;
    }
    flush();
    return out;
  }

  function inlineLines(lines) {
    var out = [];
    lines.forEach(function (line, index) {
      if (index) out.push(el('br'));
      out = out.concat(inline(line));
    });
    return out;
  }

  // Renders notes into block nodes. Task items call onTask(lineIndex, done).
  function renderMarkdown(text, onTask, cardId) {
    var lines = normalize(text).split(NL);
    var blocks = [];
    var paragraph = [];
    var index = 0;
    function flushParagraph() {
      if (paragraph.length) {
        blocks.push(el('p', {}, inlineLines(paragraph)));
        paragraph = [];
      }
    }
    while (index < lines.length) {
      var trimmed = ltrim(lines[index]);
      if (trimmed.indexOf(FENCE) === 0) {
        flushParagraph();
        var code = [];
        index++;
        while (index < lines.length && ltrim(lines[index]).indexOf(FENCE) !== 0) {
          code.push(lines[index]);
          index++;
        }
        index++;
        blocks.push(el('pre', {}, [el('code', { text: code.join(NL) })]));
        continue;
      }
      if (!trimmed.trim()) {
        flushParagraph();
        index++;
        continue;
      }
      var heading = headingOf(trimmed);
      if (heading) {
        flushParagraph();
        blocks.push(el('h' + (heading.level + 3), {}, inline(heading.text)));
        index++;
        continue;
      }
      if (trimmed === '---' || trimmed === '***' || trimmed === '___') {
        flushParagraph();
        blocks.push(el('hr'));
        index++;
        continue;
      }
      if (trimmed.charAt(0) === '>') {
        flushParagraph();
        var quote = [];
        while (index < lines.length && ltrim(lines[index]).charAt(0) === '>') {
          quote.push(ltrim(ltrim(lines[index]).slice(1)));
          index++;
        }
        blocks.push(el('blockquote', {}, inlineLines(quote)));
        continue;
      }
      var marker = listMarker(trimmed);
      if (marker) {
        flushParagraph();
        var list = el(marker.ordered ? 'ol' : 'ul', marker.ordered && marker.start !== 1 ? { start: String(marker.start) } : {});
        var tasks = 0;
        while (index < lines.length) {
          var item = listMarker(ltrim(lines[index]));
          if (!item || item.ordered !== marker.ordered) break;
          var task = taskOf(item.rest);
          if (task) {
            tasks++;
            list.appendChild(renderTask(task, index, onTask, cardId));
          } else {
            list.appendChild(el('li', {}, inline(item.rest)));
          }
          index++;
        }
        if (tasks === list.children.length) list.className = 'md-tasks';
        blocks.push(list);
        continue;
      }
      paragraph.push(trimmed);
      index++;
    }
    flushParagraph();
    return blocks;
  }

  function renderTask(task, lineIndex, onTask, cardId) {
    var box = el('input', {
      type: 'checkbox',
      checked: task.done,
      disabled: !onTask,
      'data-focus-key': 'task:' + cardId + ':' + lineIndex,
      on: onTask ? { change: function (event) { onTask(lineIndex, event.target.checked); } } : {},
    });
    return el('li', { class: 'md-task' + (task.done ? ' done' : '') }, [el('label', {}, [box, el('span', {}, inline(task.text))])]);
  }

  function notesOf(value) {
    if (typeof value.notes === 'string') return value.notes;
    return (value.checklist || []).map(function (item) {
      return '- [' + (item.done ? 'x' : ' ') + '] ' + item.text;
    }).join(NL);
  }

  // Task items in the notes, mirrored into the schema's checklist.
  function tasksFrom(notes) {
    var out = [];
    var inFence = false;
    normalize(notes).split(NL).forEach(function (line) {
      var trimmed = ltrim(line);
      if (trimmed.indexOf(FENCE) === 0) {
        inFence = !inFence;
        return;
      }
      if (inFence || out.length >= 20) return;
      var marker = listMarker(trimmed);
      var task = marker && taskOf(marker.rest);
      var text = task ? task.text.trim().slice(0, 200) : '';
      if (text) out.push({ text: text, done: task.done });
    });
    return out;
  }

  function toggleTaskLine(notes, lineIndex, done) {
    var lines = normalize(notes).split(NL);
    var line = lines[lineIndex];
    if (line === undefined) return notes;
    var lead = line.length - ltrim(line).length;
    var marker = listMarker(line.slice(lead));
    if (!marker || !taskOf(marker.rest)) return notes;
    var prefix = line.length - lead - marker.rest.length;
    lines[lineIndex] = line.slice(0, lead + prefix) + (done ? '[x]' : '[ ]') + marker.rest.slice(3);
    return lines.join(NL);
  }

  // ---- Board data

  function isRun(doc) {
    return doc && doc.value && typeof doc.value.cardId === 'string' && Object.prototype.hasOwnProperty.call(RUN_LABELS, doc.value.state);
  }

  function stamp(doc) {
    var time = typeof doc.updatedAt === 'number' ? doc.updatedAt : Date.parse(doc.updatedAt);
    return isNaN(time) ? 0 : time;
  }

  function latestRun(cardId) {
    var latest = null;
    runDocs.forEach(function (doc) {
      if (doc.value.cardId === cardId && (latest === null || stamp(doc) >= stamp(latest))) latest = doc;
    });
    return latest;
  }

  function latestAction(cardId) {
    var latest = null;
    actionItems.forEach(function (item) {
      if (item.action === 'run_card' && item.params && item.params.cardId === cardId) latest = item;
    });
    return latest;
  }

  // A request shows as on its way until Omnigent reports progress for it or the session writes
  // its run record.
  function pendingRun(cardId) {
    var pending = pendingRuns[cardId];
    if (!pending) return null;
    var tracked = pending.actionId && actionItems.some(function (item) { return item.id === pending.actionId; });
    var arrived = runDocs.some(function (doc) { return doc.id === pending.runId; });
    if (tracked || arrived || Date.now() - pending.at > PENDING_RUN_MS) {
      delete pendingRuns[cardId];
      return null;
    }
    return pending;
  }

  function isOpenAction(action) {
    return Boolean(action) && OPEN_ACTION_STATUSES.indexOf(action.status) !== -1;
  }

  function runSignature(cardId) {
    var run = latestRun(cardId);
    var action = latestAction(cardId);
    return [run ? [run.id, run.version] : null, pendingRun(cardId), action ? [action.id, action.status, action.summary] : null];
  }

  function onActions(snapshot) {
    if (snapshot.status === 'loading') return;
    actionItems = snapshot.items;
    scheduleRender();
  }

  function canRunSessions() {
    return canEdit && runs !== null && Boolean(client.agent);
  }

  function runInSession(id) {
    var item = findItem(displayCards(), id);
    if (!item || !canRunSessions()) return;
    var runId = 'run-' + Date.now().toString(36) + '-' + Math.floor(Math.random() * 1679616).toString(36);
    var pending = { runId: runId, at: Date.now(), error: '', delivery: '' };
    pendingRuns[id] = pending;
    scheduleRender();
    client.agent.request('run_card', { cardId: item.id, runId: runId }).then(function (receipt) {
      if (pendingRuns[id] !== pending) return;
      pending.delivery = receipt && receipt.delivery ? receipt.delivery : 'sent';
      pending.actionId = receipt && receipt.actionId ? receipt.actionId : '';
      if (actionWatch) actionWatch.refresh();
      scheduleRender();
    }, function (error) {
      if (pendingRuns[id] !== pending) return;
      pending.error = error && error.code === 'PERMISSION_DENIED'
        ? (error.message || 'Your thingy session did not get the card. Click Run in a session again.')
        : 'Could not send the card to your thingy session: ' + (error && error.message ? error.message : String(error));
      scheduleRender();
    });
  }

  function renderRun(item, interactive) {
    var run = latestRun(item.id);
    var pending = pendingRun(item.id);
    var action = latestAction(item.id);
    var children = [];
    if (pending) {
      children.push(pending.error
        ? el('div', { class: 'run-state', 'data-run': 'failed', role: 'alert', text: pending.error })
        : el('div', { class: 'run-state', 'data-run': 'pending' }, [el('span', { class: 'spinner', 'aria-hidden': 'true' }), el('span', { text: pending.delivery === 'starting' ? 'Start your thingy session beside the board to run this card.' : pending.delivery === 'sent' || pending.delivery === 'merged' ? 'Sent to your thingy session.' : 'Sending to your thingy session…' })]));
    } else if (action && Object.prototype.hasOwnProperty.call(ACTION_LABELS, action.status)) {
      var open = isOpenAction(action);
      var shown = action.status === 'interrupted' || action.status === 'finished_unreported' ? 'failed' : action.status;
      children.push(el('div', { class: 'run-state', 'data-run': open && action.status !== 'needs_input' ? 'running' : shown }, [open ? el('span', { class: 'spinner', 'aria-hidden': 'true' }) : null, el('span', { text: ACTION_LABELS[action.status] })]));
      var summary = run && typeof run.value.summary === 'string' && run.value.summary.trim() ? run.value.summary : action.summary;
      if (typeof summary === 'string' && summary.trim()) {
        children.push(el('div', { class: 'md' }, renderMarkdown(summary, null, 'run-' + item.id)));
      }
    } else if (run) {
      var state = run.value.state;
      children.push(el('div', { class: 'run-state', 'data-run': state }, [state === 'running' ? el('span', { class: 'spinner', 'aria-hidden': 'true' }) : null, el('span', { text: RUN_LABELS[state] })]));
      if (typeof run.value.summary === 'string' && run.value.summary.trim()) {
        children.push(el('div', { class: 'md' }, renderMarkdown(run.value.summary, null, 'run-' + item.id)));
      }
    }
    // The thingy header's session toggle opens the session doing the work, so cards offer no Open session.
    var actions = [];
    if (interactive && canRunSessions()) {
      // A run that never reached a session (its New session was left) can be sent again.
      var undelivered = pending ? pending.delivery === 'starting' : Boolean(action) && action.status === 'pending';
      var busy = !undelivered && (Boolean(pending && !pending.error) || isOpenAction(action));
      actions.push(el('button', { type: 'button', text: busy ? 'Running…' : undelivered ? 'Open New session again' : run || pending || action ? 'Run again in a session' : 'Run in a session', disabled: busy, 'data-focus-key': 'run:' + item.id, on: { click: function () { runInSession(item.id); } } }));
    }
    if (actions.length) children.push(el('div', { class: 'run-actions' }, actions));
    return children.length ? el('div', { class: 'run', 'aria-live': 'polite' }, children) : null;
  }

  function onRuns(snapshot) {
    if (snapshot.status === 'loading') return;
    runDocs = snapshot.items.filter(isRun);
    scheduleRender();
  }

  function isCard(doc) {
    return doc && doc.value && typeof doc.value.title === 'string' && typeof doc.value.column === 'string';
  }

  function serverDoc(id) {
    for (var index = 0; index < serverDocs.length; index++) {
      if (serverDocs[index].id === id) return serverDocs[index];
    }
    return null;
  }

  function orderOf(item) {
    return typeof item.value.order === 'number' ? item.value.order : 0;
  }

  function byOrder(a, b) {
    var difference = orderOf(a) - orderOf(b);
    if (difference) return difference;
    return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
  }

  function displayCards() {
    var out = [];
    var seen = {};
    serverDocs.forEach(function (doc) {
      seen[doc.id] = true;
      var rec = local[doc.id];
      if (!rec) {
        out.push({ id: doc.id, value: doc.value, rec: null });
      } else if (!(rec.deleted && rec.status === 'saved')) {
        out.push({ id: doc.id, value: rec.value, rec: rec });
      }
    });
    Object.keys(local).forEach(function (id) {
      var rec = local[id];
      if (!seen[id] && !(rec.deleted && rec.status === 'saved')) {
        out.push({ id: id, value: rec.value, rec: rec });
      }
    });
    return out;
  }

  function findItem(items, id) {
    for (var index = 0; index < items.length; index++) {
      if (items[index].id === id) return items[index];
    }
    return null;
  }

  function columnCards(items, columnId) {
    return items.filter(function (item) { return item.value.column === columnId; }).sort(byOrder);
  }

  function visibleColumns(items) {
    var columns = COLUMNS.slice();
    items.forEach(function (item) {
      if (!columns.some(function (column) { return column.id === item.value.column; })) {
        columns.push({ id: item.value.column, title: 'Other: ' + item.value.column });
      }
    });
    return columns;
  }

  function columnTitle(columnId) {
    var columns = visibleColumns(displayCards());
    for (var index = 0; index < columns.length; index++) {
      if (columns[index].id === columnId) return columns[index].title;
    }
    return columnId;
  }

  // Drops local copies once a later read shows the saved change, or someone's newer one.
  function reconcile() {
    var now = Date.now();
    Object.keys(local).forEach(function (id) {
      var rec = local[id];
      if (rec.status !== 'saved' || rec.inFlight || rec.dirty || rec.temp) return;
      var doc = serverDoc(id);
      var drop;
      if (now - rec.savedAt > SAVED_OVERLAY_MS) {
        drop = true;
      } else if (!doc) {
        drop = rec.deleted || !rec.createdHere;
      } else {
        drop = rec.superseded.indexOf(doc.version) < 0;
      }
      if (drop) delete local[id];
    });
  }

  // ---- Saving

  // base is the server copy the local edits started from, so a rebase can tell which fields
  // this tab changed.
  function newRec(id, value, version) {
    return { id: id, value: value, base: clone(value), version: version, superseded: [], status: 'saved', temp: false, createdHere: false, deleted: false, discarded: false, inFlight: false, dirty: false, label: '', message: '', errorKind: null, lastRequest: null, retryKey: null, savedAt: 0 };
  }

  function ensureRec(id) {
    if (local[id]) return local[id];
    var doc = serverDoc(id);
    if (!doc) return null;
    local[id] = newRec(id, clone(doc.value), doc.version);
    return local[id];
  }

  function queue(rec, label) {
    rec.label = label;
    rec.status = 'saving';
    rec.message = '';
    rec.errorKind = null;
    rec.dirty = true;
    anyChange = true;
    scheduleRender();
    pump(rec);
  }

  function updateCard(id, change, label) {
    var rec = ensureRec(id);
    if (!rec || rec.temp || rec.deleted) return;
    var next = clone(rec.value);
    change(next);
    rec.value = next;
    queue(rec, label);
  }

  function removeCard(id) {
    var rec = ensureRec(id);
    if (!rec || rec.temp) return;
    rec.deleted = true;
    queue(rec, 'Deleting…');
  }

  // One request per card at a time. Changes made meanwhile are folded into the next
  // request, which is based on the version the previous one returned.
  function pump(rec) {
    if (rec.inFlight || !rec.dirty || rec.temp) return;
    rec.dirty = false;
    send(rec, rec.deleted ? { kind: 'delete', ifVersion: rec.version } : { kind: 'replace', value: clone(rec.value), ifVersion: rec.version }, null);
  }

  function send(rec, request, key) {
    rec.inFlight = true;
    rec.status = 'saving';
    rec.lastRequest = request;
    rec.retryKey = null;
    var options = key ? { idempotencyKey: key } : {};
    var call;
    if (request.kind === 'create') {
      call = cards.create(request.value, options);
    } else if (request.kind === 'delete') {
      call = cards.delete(rec.id, Object.assign({ ifVersion: request.ifVersion }, options));
    } else {
      call = cards.replace(rec.id, request.value, Object.assign({ ifVersion: request.ifVersion }, options));
    }
    call.then(
      function (result) { succeeded(rec, request, result); },
      function (error) { failed(rec, request, error); }
    );
  }

  function succeeded(rec, request, result) {
    rec.inFlight = false;
    if (rec.discarded) return;
    if (request.kind === 'create') {
      delete local[rec.id];
      delete cardNodes[rec.id];
      rec.id = result.id;
      rec.temp = false;
      local[rec.id] = rec;
    } else if (rec.version) {
      rec.superseded.push(rec.version);
    }
    if (request.value) rec.base = clone(request.value);
    rec.version = result.version;
    if (rec.dirty) {
      pump(rec);
    } else {
      rec.status = 'saved';
      rec.savedAt = Date.now();
    }
    scheduleRender();
  }

  function failed(rec, request, error) {
    rec.inFlight = false;
    if (rec.discarded) return;
    var code = window.omnigent.isOmnigentError(error) ? error.code : null;
    if (request.kind === 'delete' && code === 'NOT_FOUND') {
      rec.status = 'saved';
      rec.savedAt = Date.now();
      scheduleRender();
      return;
    }
    rec.status = 'error';
    if (code && UNKNOWN_OUTCOME.indexOf(code) >= 0 && error.operationId) {
      rec.errorKind = 'unknown';
      rec.retryKey = error.operationId;
      rec.message = 'This change might not be saved. Check your connection and retry.';
    } else if (code === 'CONFLICT') {
      rec.errorKind = 'conflict';
      rec.message = 'Someone else changed this card.';
    } else if (code === 'NOT_FOUND') {
      rec.errorKind = 'fatal';
      rec.message = 'This card was deleted by someone else.';
    } else if (code === 'PERMISSION_DENIED') {
      rec.errorKind = 'fatal';
      rec.message = 'You can no longer edit this board.';
    } else if (code === 'REVISION_CHANGED') {
      rec.errorKind = 'fatal';
      rec.message = 'This thingy was updated. Reload it to keep editing.';
    } else if (code === 'CONSTRAINT_VIOLATION') {
      // The board's rules refused the change; the message names the rule. Dismiss puts the card back.
      rec.errorKind = 'fatal';
      rec.message = error.message;
    } else {
      rec.errorKind = 'retry';
      rec.message = 'Not saved' + (error && error.message ? ': ' + error.message : '.');
    }
    scheduleRender();
  }

  function retryRec(id) {
    var rec = local[id];
    if (!rec) return;
    rec.message = '';
    rec.errorKind = null;
    if (rec.retryKey) {
      send(rec, rec.lastRequest, rec.retryKey);
    } else if (rec.temp) {
      send(rec, { kind: 'create', value: clone(rec.value) }, null);
    } else {
      rec.dirty = true;
      rec.status = 'saving';
      pump(rec);
    }
    scheduleRender();
  }

  // The newest server copy with only the fields this tab changed (local against its base)
  // applied, so someone else's edits to other fields survive.
  // A missing field and an empty one are the same to the editor (legacy cards have no notes).
  function sameField(a, b) {
    function norm(value) { return value === undefined || value === '' || (Array.isArray(value) && !value.length) ? null : value; }
    return JSON.stringify(norm(a)) === JSON.stringify(norm(b));
  }

  function rebase(rec, latest) {
    var merged = clone(latest);
    var keys = Object.keys(rec.base || {}).concat(Object.keys(rec.value));
    keys.forEach(function (key) {
      var mine = rec.value[key];
      if (sameField(mine, (rec.base || {})[key])) return;
      if (mine === undefined) delete merged[key];
      else merged[key] = clone(mine);
    });
    return merged;
  }

  // Rebases the local change on the newest server copy and saves it again.
  function keepMine(id) {
    var rec = local[id];
    if (!rec) return;
    rec.status = 'saving';
    rec.message = '';
    rec.errorKind = null;
    rec.inFlight = true;
    scheduleRender();
    cards.get(rec.id).then(function (doc) {
      rec.inFlight = false;
      if (rec.discarded) return;
      if (!doc) {
        failed(rec, { kind: rec.deleted ? 'delete' : 'replace' }, { name: 'OmnigentError', code: 'NOT_FOUND' });
        return;
      }
      if (!rec.deleted) rec.value = rebase(rec, doc.value);
      rec.base = clone(doc.value);
      rec.version = doc.version;
      rec.dirty = true;
      pump(rec);
    }, function (error) {
      failed(rec, { kind: 'replace' }, error);
    });
  }

  function discardRec(id) {
    var rec = local[id];
    if (!rec) return;
    rec.discarded = true;
    delete local[id];
    scheduleRender();
  }

  function addCardValue(value) {
    var id = 'new-' + (++tempSeq);
    var rec = newRec(id, value, null);
    rec.temp = true;
    rec.createdHere = true;
    rec.status = 'saving';
    rec.label = 'Adding…';
    local[id] = rec;
    anyChange = true;
    scheduleRender();
    send(rec, { kind: 'create', value: clone(value) }, null);
  }

  function lastOrder(columnId) {
    var list = columnCards(displayCards(), columnId);
    return list.length ? orderOf(list[list.length - 1]) + 1 : 1;
  }

  function addCard(columnId, title) {
    addCardValue({ title: title, owner: '', column: columnId, order: lastOrder(columnId), checklist: [], notes: '' });
    announce('Adding “' + title + '” to ' + columnTitle(columnId) + '.');
  }

  function addSamples() {
    SAMPLE_CARDS.forEach(function (sample) {
      addCardValue({ title: sample.title, owner: sample.owner, column: sample.column, order: lastOrder(sample.column), synthetic: true, notes: sample.notes, checklist: tasksFrom(sample.notes) });
    });
  }

  function moveTo(id, columnId, index) {
    var items = displayCards();
    var item = findItem(items, id);
    if (!item) return;
    var current = columnCards(items, item.value.column);
    var target = columnCards(items, columnId).filter(function (other) { return other.id !== id; });
    index = Math.max(0, Math.min(index, target.length));
    if (item.value.column === columnId && current.indexOf(item) === index) {
      announce('“' + item.value.title + '” was not moved.');
      return;
    }
    var before = target[index - 1];
    var after = target[index];
    var order;
    if (!before && !after) order = 1;
    else if (!before) order = orderOf(after) - 1;
    else if (!after) order = orderOf(before) + 1;
    else order = (orderOf(before) + orderOf(after)) / 2;
    updateCard(id, function (value) {
      value.column = columnId;
      value.order = order;
    }, 'Moving…');
    announce('Moved “' + item.value.title + '” to ' + columnTitle(columnId) + ', position ' + (index + 1) + ' of ' + (target.length + 1) + '.');
  }

  function toggleTask(id, lineIndex, done) {
    updateCard(id, function (value) {
      var notes = toggleTaskLine(notesOf(value), lineIndex, done);
      value.notes = notes;
      value.checklist = tasksFrom(notes);
    }, 'Saving…');
  }

  // ---- Rendering

  function scheduleRender() {
    if (renderQueued) return;
    renderQueued = true;
    Promise.resolve().then(render);
  }

  function stateOf(item) {
    var rec = item.rec;
    if (!rec) return 'idle';
    if (rec.status === 'saving') return rec.temp ? 'creating' : rec.deleted ? 'deleting' : 'saving';
    if (rec.status === 'error') return 'error';
    return 'idle';
  }

  function focusKey(key) {
    var nodes = document.querySelectorAll('[data-focus-key]');
    for (var index = 0; index < nodes.length; index++) {
      if (nodes[index].getAttribute('data-focus-key') === key) {
        var node = nodes[index];
        node.focus();
        if (node.tagName === 'TEXTAREA' && node.setSelectionRange) {
          node.setSelectionRange(node.value.length, node.value.length);
        }
        return true;
      }
    }
    return false;
  }

  function render() {
    renderQueued = false;
    if ((drag && drag.started) || keyMove) {
      renderAfterMove = true;
      renderStatus();
      return;
    }
    var active = document.activeElement;
    var activeKey = active && active.getAttribute ? active.getAttribute('data-focus-key') : null;
    var items = displayCards();
    var columns = visibleColumns(items);
    var board = document.getElementById('board');
    board.setAttribute('data-can-write', canWrite ? 'true' : 'false');
    var wantedCards = {};
    var wantedSections = [];
    columns.forEach(function (column, columnIndex) {
      var entry = columnNode(column);
      wantedSections.push(entry.section);
      if (board.children[columnIndex] !== entry.section) {
        board.insertBefore(entry.section, board.children[columnIndex] || null);
      }
      var nodes;
      if (watchStatus === 'loading') {
        entry.count.textContent = '';
        nodes = [el('li', { class: 'skeleton', 'aria-hidden': 'true' }), el('li', { class: 'skeleton', 'aria-hidden': 'true' })];
        if (entry.list.querySelector('.skeleton')) nodes = Array.from(entry.list.children);
      } else {
        var list = columnCards(items, column.id);
        entry.count.textContent = String(list.length);
        nodes = list.map(function (item) {
          wantedCards[item.id] = true;
          return cardNode(item);
        });
        if (!nodes.length) {
          if (!entry.empty) entry.empty = el('li', { class: 'empty', text: 'No cards' });
          nodes = [entry.empty];
        }
      }
      Array.from(entry.list.children).forEach(function (child) {
        if (nodes.indexOf(child) < 0) child.remove();
      });
      nodes.forEach(function (node, index) {
        if (entry.list.children[index] !== node) {
          entry.list.insertBefore(node, entry.list.children[index] || null);
        }
      });
    });
    Array.from(board.children).forEach(function (child) {
      if (wantedSections.indexOf(child) < 0) child.remove();
    });
    Object.keys(columnNodes).forEach(function (id) {
      if (wantedSections.indexOf(columnNodes[id].section) < 0) delete columnNodes[id];
    });
    Object.keys(cardNodes).forEach(function (id) {
      if (!wantedCards[id]) delete cardNodes[id];
    });
    if (focusAfterRender) {
      focusKey(focusAfterRender);
      focusAfterRender = null;
    } else if (activeKey && (!document.activeElement || document.activeElement === document.body)) {
      focusKey(activeKey);
    }
    renderStatus();
  }

  function renderStatus() {
    var status = document.getElementById('status');
    var labels = { loading: 'Loading', live: preview ? 'Preview' : 'Live', reconnecting: 'Reconnecting', stale: 'Out of date' };
    status.textContent = labels[watchStatus] || watchStatus;
    status.setAttribute('data-status', watchStatus);
    var saving = 0;
    var failedCount = 0;
    Object.keys(local).forEach(function (id) {
      if (local[id].status === 'saving') saving++;
      if (local[id].status === 'error') failedCount++;
    });
    var sync = document.getElementById('sync');
    if (failedCount) {
      sync.setAttribute('data-state', 'error');
      sync.replaceChildren(document.createTextNode(failedCount + (failedCount === 1 ? ' change' : ' changes') + ' not saved'));
    } else if (preview) {
      // Nothing in a preview is saved, so it shows no saving state.
      sync.setAttribute('data-state', 'idle');
      sync.replaceChildren();
    } else if (saving) {
      sync.setAttribute('data-state', 'saving');
      sync.replaceChildren(el('span', { class: 'spinner', 'aria-hidden': 'true' }), document.createTextNode('Saving ' + saving + (saving === 1 ? ' change…' : ' changes…')));
    } else if (anyChange) {
      sync.setAttribute('data-state', 'saved');
      sync.replaceChildren(document.createTextNode('All changes saved'));
    } else {
      sync.setAttribute('data-state', 'idle');
      sync.replaceChildren();
    }
    document.getElementById('add-samples').hidden = !(canWrite && watchStatus !== 'loading' && displayCards().length === 0);
  }

  function columnNode(column) {
    var entry = columnNodes[column.id];
    if (entry) return entry;
    var headingId = 'column-heading-' + (++columnSeq);
    var count = el('span', { class: 'count' });
    var list = el('ul', { class: 'cards', 'aria-labelledby': headingId });
    var section = el('section', { class: 'column', 'data-column': column.id, 'aria-labelledby': headingId }, [
      el('h2', { id: headingId }, [el('span', { text: column.title }), count]),
      list,
      canWrite ? composer(column) : null,
    ]);
    entry = { section: section, count: count, list: list, empty: null };
    columnNodes[column.id] = entry;
    return entry;
  }

  function composer(column) {
    var input = el('input', { type: 'text', maxlength: '200', placeholder: 'Card title', 'aria-label': 'Title of the new card in ' + column.title });
    var form;
    var trigger;
    function open() {
      trigger.hidden = true;
      form.hidden = false;
      input.focus();
    }
    function close() {
      input.value = '';
      form.hidden = true;
      trigger.hidden = false;
      trigger.focus();
    }
    function submit() {
      var title = input.value.trim();
      if (!title) {
        input.focus();
        return;
      }
      input.value = '';
      addCard(column.id, title);
      input.focus();
    }
    input.addEventListener('keydown', function (event) {
      if (event.key === 'Enter') {
        event.preventDefault();
        submit();
      } else if (event.key === 'Escape') {
        event.preventDefault();
        close();
      }
    });
    form = el('div', { class: 'composer-form', hidden: true }, [
      input,
      el('div', { class: 'row' }, [
        el('button', { type: 'button', class: 'primary', text: 'Add', on: { click: submit } }),
        el('button', { type: 'button', text: 'Cancel', on: { click: close } }),
      ]),
    ]);
    trigger = el('button', { type: 'button', class: 'add-trigger', text: '+ Add card', 'aria-label': 'Add a card to ' + column.title, on: { click: open } });
    return el('div', { class: 'composer' }, [trigger, form]);
  }

  function cardNode(item) {
    var isEditing = Boolean(editing[item.id]);
    var rec = item.rec;
    var sig = isEditing ? 'editing' : JSON.stringify([item.value, stateOf(item), rec ? [rec.label, rec.message, rec.errorKind] : null, canWrite, runSignature(item.id)]);
    var entry = cardNodes[item.id];
    if (entry && entry.sig === sig) return entry.node;
    var node = isEditing ? renderEditor(item) : renderCard(item);
    if (entry && entry.node.parentNode) entry.node.parentNode.replaceChild(node, entry.node);
    cardNodes[item.id] = { sig: sig, node: node };
    return node;
  }

  function renderCard(item) {
    var value = item.value;
    var state = stateOf(item);
    var interactive = canWrite && state !== 'creating' && state !== 'deleting' && !(item.rec && (item.rec.temp || item.rec.deleted));
    var titleId = 'card-title-' + item.id;
    var title = interactive
      ? el('button', { type: 'button', class: 'card-title', id: titleId, 'data-focus-key': 'title:' + item.id, text: value.title, on: { click: function () { openEditor(item.id, 'title'); } } })
      : el('span', { class: 'card-title', id: titleId, text: value.title });
    var grip = interactive
      ? el('button', {
        type: 'button',
        class: 'grip',
        'aria-label': 'Move ' + value.title,
        'aria-describedby': 'move-help',
        'data-focus-key': 'grip:' + item.id,
        on: { keydown: function (event) { onGripKey(event, item.id); }, blur: onGripBlur },
      })
      : null;
    var notes = notesOf(value);
    var body = null;
    if (notes.trim()) {
      body = el('div', { class: 'md' }, renderMarkdown(notes, interactive ? function (lineIndex, done) { toggleTask(item.id, lineIndex, done); } : null, item.id));
    } else if (interactive) {
      body = el('button', { type: 'button', class: 'add-notes', text: 'Add notes', on: { click: function () { openEditor(item.id, 'notes'); } } });
    }
    // The reference and label name the card when the viewer selects text in it or Alt+clicks it.
    var node = el('li', { class: 'card', 'data-id': item.id, 'data-omnigent-ref': 'cards/' + item.id, 'data-omnigent-label': 'Card ' + JSON.stringify(value.title), 'data-state': state, 'aria-labelledby': titleId, 'aria-busy': state === 'idle' || state === 'error' ? 'false' : 'true' }, [
      el('div', { class: 'card-head' }, [grip, el('h3', {}, [title])]),
      value.owner ? el('div', { class: 'owner', text: value.owner }) : null,
      value.synthetic ? el('div', { class: 'sample', text: 'Synthetic sample data' }) : null,
      body,
      renderRun(item, interactive),
      renderCardState(item, state),
    ]);
    if (interactive) {
      node.addEventListener('click', function (event) {
        if (event.target.closest('button, input, label, textarea, .card-error')) return;
        var selection = window.getSelection ? String(window.getSelection()) : '';
        if (selection) return;
        openEditor(item.id, 'notes');
      });
    }
    return node;
  }

  function renderCardState(item, state) {
    var rec = item.rec;
    if (state === 'saving' || state === 'creating' || state === 'deleting') {
      var label = state === 'creating' ? 'Adding…' : state === 'deleting' ? 'Deleting…' : rec.label || 'Saving…';
      return el('div', { class: 'card-status' }, [el('span', { class: 'spinner', 'aria-hidden': 'true' }), el('span', { text: label })]);
    }
    if (state !== 'error') return null;
    function action(text, handler, primary) {
      return el('button', { type: 'button', class: primary ? 'primary' : null, text: text, 'data-focus-key': text + ':' + item.id, on: { click: function () { handler(item.id); } } });
    }
    var actions;
    if (rec.errorKind === 'conflict') {
      actions = [action('Keep my change', keepMine, true), action('Use latest', discardRec)];
    } else if (rec.errorKind === 'fatal') {
      actions = [action('Dismiss', discardRec)];
    } else {
      actions = [action('Retry', retryRec, true), action('Discard', discardRec)];
    }
    return el('div', { class: 'card-error', role: 'alert' }, [el('p', { text: rec.message }), el('div', { class: 'row' }, actions)]);
  }

  function openEditor(id, field) {
    if (!canWrite || suppressClick) return;
    var item = findItem(displayCards(), id);
    if (!item) return;
    var state = stateOf(item);
    if (state === 'creating' || state === 'deleting' || (item.rec && (item.rec.temp || item.rec.deleted))) return;
    editing[id] = { title: item.value.title, owner: item.value.owner || '', notes: notesOf(item.value), confirmDelete: false };
    focusAfterRender = (field === 'title' ? 'edit-title:' : 'edit-notes:') + id;
    scheduleRender();
  }

  function closeEditor(id) {
    delete editing[id];
    focusAfterRender = 'title:' + id;
    scheduleRender();
  }

  function rowsFor(text) {
    return String(Math.min(16, Math.max(4, normalize(text).split(NL).length + 1)));
  }

  function renderEditor(item) {
    var id = item.id;
    var draft = editing[id];
    var base = 'edit-' + id;
    var error = el('p', { class: 'field-error', id: base + '-error', role: 'alert' });
    var titleInput = el('input', { id: base + '-title', type: 'text', maxlength: '200', value: draft.title, 'data-focus-key': 'edit-title:' + id, on: { input: function (event) { draft.title = event.target.value; } } });
    var ownerInput = el('input', { id: base + '-owner', type: 'text', maxlength: '100', value: draft.owner, 'data-focus-key': 'edit-owner:' + id, on: { input: function (event) { draft.owner = event.target.value; } } });
    var notesInput = el('textarea', {
      id: base + '-notes',
      rows: rowsFor(draft.notes),
      maxlength: String(NOTES_LIMIT),
      value: draft.notes,
      'aria-describedby': base + '-hint',
      'data-focus-key': 'edit-notes:' + id,
      on: { input: function (event) { draft.notes = event.target.value; event.target.rows = rowsFor(draft.notes); } },
    });
    var deleteButton = el('button', { type: 'button', class: 'danger', text: 'Delete card' });
    deleteButton.addEventListener('click', function () {
      if (draft.confirmDelete) {
        delete editing[id];
        removeCard(id);
        return;
      }
      draft.confirmDelete = true;
      deleteButton.textContent = 'Confirm delete';
      deleteButton.classList.add('confirm');
      window.setTimeout(function () {
        draft.confirmDelete = false;
        deleteButton.textContent = 'Delete card';
        deleteButton.classList.remove('confirm');
      }, 4000);
    });
    function save() {
      var title = draft.title.trim();
      if (!title) {
        error.textContent = 'Add a title.';
        titleInput.focus();
        return;
      }
      if (draft.notes.length > NOTES_LIMIT) {
        error.textContent = 'Notes can be at most ' + NOTES_LIMIT + ' characters.';
        notesInput.focus();
        return;
      }
      var current = findItem(displayCards(), id);
      var changed = !current || current.value.title !== title || (current.value.owner || '') !== draft.owner.trim() || notesOf(current.value) !== draft.notes;
      var notes = draft.notes;
      var owner = draft.owner.trim();
      closeEditor(id);
      if (!changed) return;
      updateCard(id, function (value) {
        value.title = title;
        value.owner = owner;
        value.notes = notes;
        value.checklist = tasksFrom(notes);
      }, 'Saving…');
    }
    var node = el('li', { class: 'card editing', 'data-id': id, 'aria-label': 'Edit ' + item.value.title }, [
      el('label', { class: 'field', for: base + '-title' }, ['Title', titleInput].map(textOrNode)),
      el('label', { class: 'field', for: base + '-owner' }, ['Owner', ownerInput].map(textOrNode)),
      el('label', { class: 'field', for: base + '-notes' }, ['Notes', notesInput].map(textOrNode)),
      el('div', { class: 'field-hint', id: base + '-hint', text: 'Markdown: **bold**, _italic_, - [ ] task, # heading. Ctrl or ⌘ + Enter saves; Esc cancels.' }),
      error,
      el('div', { class: 'editor-actions' }, [
        el('button', { type: 'button', class: 'primary', text: 'Save', on: { click: save } }),
        el('button', { type: 'button', text: 'Cancel', on: { click: function () { closeEditor(id); } } }),
        el('span', { class: 'spacer' }),
        deleteButton,
      ]),
    ]);
    node.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') {
        event.preventDefault();
        closeEditor(id);
      } else if (event.key === 'Enter' && (event.metaKey || event.ctrlKey || event.target.tagName === 'INPUT')) {
        event.preventDefault();
        save();
      }
    });
    return node;
  }

  function textOrNode(part) {
    return typeof part === 'string' ? document.createTextNode(part) : part;
  }

  // ---- Moving cards: pointer drag for mouse, touch and pen; keyboard moves on the grip.

  function onPointerDown(event) {
    if (!canWrite || drag || keyMove || event.button !== 0 || event.isPrimary === false) return;
    var target = event.target;
    var card = target.closest ? target.closest('.card[data-id]') : null;
    if (!card || card.classList.contains('editing')) return;
    var state = card.getAttribute('data-state');
    var pending = local[card.getAttribute('data-id')];
    if (state === 'creating' || state === 'deleting' || (pending && (pending.temp || pending.deleted))) return;
    var fromGrip = Boolean(target.closest('.grip'));
    if (!fromGrip && (event.pointerType !== 'mouse' || target.closest('input, textarea, label, .card-error'))) return;
    drag = { id: card.getAttribute('data-id'), card: card, pointerId: event.pointerId, startX: event.clientX, startY: event.clientY, x: event.clientX, y: event.clientY, started: false, cancelled: false };
    document.addEventListener('pointermove', onPointerMove);
    document.addEventListener('pointerup', onPointerUp);
    document.addEventListener('pointercancel', onPointerCancel);
  }

  function onPointerMove(event) {
    if (!drag || event.pointerId !== drag.pointerId || drag.cancelled) return;
    drag.x = event.clientX;
    drag.y = event.clientY;
    if (!drag.started) {
      if (Math.abs(drag.x - drag.startX) + Math.abs(drag.y - drag.startY) < 6) return;
      startDrag();
    }
    event.preventDefault();
    drag.ghost.style.left = (drag.x - drag.offsetX) + 'px';
    drag.ghost.style.top = (drag.y - drag.offsetY) + 'px';
    locateDrop();
  }

  function startDrag() {
    var card = drag.card;
    var rect = card.getBoundingClientRect();
    drag.started = true;
    drag.offsetX = drag.startX - rect.left;
    drag.offsetY = drag.startY - rect.top;
    var ghost = card.cloneNode(true);
    ghost.classList.add('ghost');
    ghost.removeAttribute('data-id');
    ghost.setAttribute('aria-hidden', 'true');
    Array.from(ghost.querySelectorAll('[id], [data-focus-key]')).forEach(function (node) {
      node.removeAttribute('id');
      node.removeAttribute('data-focus-key');
    });
    ghost.style.width = rect.width + 'px';
    ghost.style.left = rect.left + 'px';
    ghost.style.top = rect.top + 'px';
    var slot = el('li', { class: 'drop-slot', 'aria-hidden': 'true' });
    slot.style.height = rect.height + 'px';
    card.parentNode.insertBefore(slot, card);
    card.classList.add('drag-source');
    document.body.appendChild(ghost);
    document.body.classList.add('dragging');
    drag.ghost = ghost;
    drag.slot = slot;
    suppressClick = true;
    if (window.getSelection) window.getSelection().removeAllRanges();
    try {
      document.body.setPointerCapture(drag.pointerId);
    } catch (ignored) {
      // Capture only keeps events flowing when the pointer leaves the frame.
    }
    autoScroll();
  }

  function locateDrop() {
    var under = document.elementFromPoint(drag.x, drag.y);
    var column = under && under.closest ? under.closest('.column') : null;
    Array.from(document.querySelectorAll('.column.drop-target')).forEach(function (node) {
      if (node !== column) node.classList.remove('drop-target');
    });
    if (!column) return;
    column.classList.add('drop-target');
    var list = column.querySelector('.cards');
    var siblings = Array.from(list.children).filter(function (node) {
      return node.classList.contains('card') && !node.classList.contains('drag-source');
    });
    var before = null;
    for (var index = 0; index < siblings.length; index++) {
      var rect = siblings[index].getBoundingClientRect();
      if (drag.y < rect.top + rect.height / 2) {
        before = siblings[index];
        break;
      }
    }
    if (before) {
      if (drag.slot.nextSibling !== before) list.insertBefore(drag.slot, before);
    } else if (drag.slot.parentNode !== list || drag.slot.nextSibling) {
      list.appendChild(drag.slot);
    }
  }

  function autoScroll() {
    if (!drag || !drag.started || drag.cancelled) return;
    var edge = 48;
    var step = drag.y < edge ? -12 : drag.y > window.innerHeight - edge ? 12 : 0;
    if (step) {
      window.scrollBy(0, step);
      locateDrop();
    }
    drag.frame = window.requestAnimationFrame(autoScroll);
  }

  function onPointerUp(event) {
    if (!drag || event.pointerId !== drag.pointerId) return;
    var current = drag;
    if (!current.started || current.cancelled) {
      finishDrag();
      return;
    }
    var list = current.slot.parentNode;
    var column = list ? list.closest('.column') : null;
    var index = 0;
    Array.from(list ? list.children : []).some(function (node) {
      if (node === current.slot) return true;
      if (node.classList.contains('card') && !node.classList.contains('drag-source')) index++;
      return false;
    });
    finishDrag();
    if (column) moveTo(current.id, column.getAttribute('data-column'), index);
  }

  function onPointerCancel(event) {
    if (drag && event.pointerId === drag.pointerId) finishDrag();
  }

  function cancelDrag() {
    restoreDragVisuals();
    drag.cancelled = true;
    announce('Move cancelled.');
  }

  function restoreDragVisuals() {
    if (drag.frame) window.cancelAnimationFrame(drag.frame);
    if (drag.ghost) drag.ghost.remove();
    if (drag.slot) drag.slot.remove();
    drag.card.classList.remove('drag-source');
    document.body.classList.remove('dragging');
    Array.from(document.querySelectorAll('.column.drop-target')).forEach(function (node) {
      node.classList.remove('drop-target');
    });
  }

  function finishDrag() {
    if (drag.started && !drag.cancelled) restoreDragVisuals();
    try {
      document.body.releasePointerCapture(drag.pointerId);
    } catch (ignored) {
      // Capture may already be gone.
    }
    document.removeEventListener('pointermove', onPointerMove);
    document.removeEventListener('pointerup', onPointerUp);
    document.removeEventListener('pointercancel', onPointerCancel);
    drag = null;
    // The click that follows a drag must not open the editor.
    window.setTimeout(function () { suppressClick = false; }, 0);
    if (renderAfterMove) {
      renderAfterMove = false;
      scheduleRender();
    }
  }

  function onGripKey(event, id) {
    var key = event.key;
    if (!keyMove) {
      if (key === ' ' || key === 'Enter') {
        event.preventDefault();
        startKeyMove(id);
      }
      return;
    }
    if (keyMove.id !== id) return;
    if (key === ' ' || key === 'Enter') {
      event.preventDefault();
      commitKeyMove();
    } else if (key === 'Escape') {
      event.preventDefault();
      cancelKeyMove();
    } else if (key === 'ArrowUp' || key === 'ArrowDown' || key === 'ArrowLeft' || key === 'ArrowRight') {
      event.preventDefault();
      stepKeyMove(key);
    }
  }

  function startKeyMove(id) {
    var items = displayCards();
    var item = findItem(items, id);
    if (!item || !cardNodes[id]) return;
    keyMove = { id: id, column: item.value.column, index: columnCards(items, item.value.column).indexOf(item), moving: false };
    cardNodes[id].node.classList.add('lifted');
    announce('Picked up “' + item.value.title + '”. Use the arrow keys to move it, Space to drop it, or Escape to cancel.');
  }

  function stepKeyMove(key) {
    var items = displayCards();
    var columns = visibleColumns(items);
    function others(columnId) {
      return columnCards(items, columnId).filter(function (item) { return item.id !== keyMove.id; });
    }
    var columnIndex = columns.findIndex(function (column) { return column.id === keyMove.column; });
    if (key === 'ArrowUp') {
      keyMove.index = Math.max(0, keyMove.index - 1);
    } else if (key === 'ArrowDown') {
      keyMove.index = Math.min(others(keyMove.column).length, keyMove.index + 1);
    } else {
      var nextIndex = columnIndex + (key === 'ArrowLeft' ? -1 : 1);
      if (nextIndex >= 0 && nextIndex < columns.length) {
        keyMove.column = columns[nextIndex].id;
        keyMove.index = Math.min(keyMove.index, others(keyMove.column).length);
      }
    }
    var node = cardNodes[keyMove.id].node;
    var entry = columnNodes[keyMove.column];
    Array.from(entry.list.querySelectorAll('.empty')).forEach(function (empty) { empty.remove(); });
    var siblings = Array.from(entry.list.children).filter(function (child) {
      return child.classList.contains('card') && child !== node;
    });
    keyMove.moving = true;
    entry.list.insertBefore(node, siblings[keyMove.index] || null);
    var grip = node.querySelector('.grip');
    if (grip) grip.focus();
    keyMove.moving = false;
    announce(columnTitle(keyMove.column) + ', position ' + (keyMove.index + 1) + ' of ' + (others(keyMove.column).length + 1) + '.');
  }

  function commitKeyMove() {
    var move = keyMove;
    cardNodes[move.id].node.classList.remove('lifted');
    keyMove = null;
    focusAfterRender = 'grip:' + move.id;
    moveTo(move.id, move.column, move.index);
    scheduleRender();
  }

  function cancelKeyMove() {
    var move = keyMove;
    if (cardNodes[move.id]) cardNodes[move.id].node.classList.remove('lifted');
    keyMove = null;
    focusAfterRender = 'grip:' + move.id;
    announce('Move cancelled.');
    scheduleRender();
  }

  function onGripBlur() {
    if (keyMove && !keyMove.moving) {
      var move = keyMove;
      if (cardNodes[move.id]) cardNodes[move.id].node.classList.remove('lifted');
      keyMove = null;
      scheduleRender();
    }
  }

  // ---- Watching the board

  function describeLoadError(error) {
    if (!window.omnigent.isOmnigentError(error)) return 'Could not load the board.';
    if (error.code === 'PERMISSION_DENIED') return 'You do not have permission to view this board.';
    if (error.code === 'REVISION_CHANGED') return 'A newer version of this thingy was published. Reload the thingy to continue.';
    return 'Could not refresh the board: ' + error.message;
  }

  function onSnapshot(snapshot) {
    watchStatus = snapshot.status;
    var box = document.getElementById('message');
    if (snapshot.status === 'stale' && snapshot.error) {
      showMessage(describeLoadError(snapshot.error), 'error');
      box.setAttribute('data-source', 'load');
    } else if (box.getAttribute('data-source') === 'load') {
      showMessage('');
      box.removeAttribute('data-source');
    }
    if (snapshot.status !== 'loading') {
      serverDocs = snapshot.items.filter(isCard);
      // A template preview starts with its sample cards in memory, so it shows what the board does.
      if (preview && !previewSeeded && snapshot.status === 'live' && serverDocs.length === 0) {
        previewSeeded = true;
        addSamples();
      }
      document.getElementById('truncated').textContent = snapshot.nextCursor ? 'Showing the first ' + snapshot.items.length + ' cards.' : '';
      reconcile();
    }
    scheduleRender();
  }

  window.omnigent.connect().then(
    function (connected) {
      client = connected;
      document.body.setAttribute('data-theme', client.ui.colorScheme);
      preview = client.context.preview === true;
      document.getElementById('viewer').textContent = preview ? 'Preview with sample cards' : 'Signed in as ' + client.context.viewer.displayName;
      canWrite = client.hasCapability('data.write') && client.context.permissions.writeData;
      canEdit = canWrite && client.context.permissions.publish === true;
      document.getElementById('hint').hidden = !canWrite;
      if (!canWrite) {
        showMessage('You can view this board. Ask the thingy owner for edit access to change it.');
      }
      cards = client.data.collection('cards');
      runs = client.navigation ? client.data.collection('runs') : null;
      document.getElementById('add-samples').addEventListener('click', addSamples);
      document.addEventListener('pointerdown', onPointerDown);
      document.addEventListener('click', function (event) {
        if (suppressClick) {
          event.preventDefault();
          event.stopPropagation();
        }
      }, true);
      document.addEventListener('keydown', function (event) {
        if (event.key === 'Escape' && drag && drag.started && !drag.cancelled) cancelDrag();
      });
      scheduleRender();
      cards.watch(onSnapshot, { intervalMs: 2000 });
      if (runs) runs.watch(onRuns, { intervalMs: 3000 });
      if (canEdit && client.agent && typeof client.agent.watch === 'function') {
        actionWatch = client.agent.watch(onActions, { intervalMs: 3000 });
      }
    },
    function (error) {
      showMessage('The thingy could not connect to Omnigent: ' + (error && error.message ? error.message : String(error)), 'error');
    }
  );
})();
`;

const BODY = `
<header class="top">
  <div class="top-row">
    <h1>Team board</h1>
    <span id="status" class="status" role="status" data-status="loading">Loading</span>
    <span id="sync" class="sync" data-state="idle" aria-live="polite"></span>
  </div>
  <div class="top-row">
    <span id="viewer" class="muted"></span>
    <span id="hint" class="muted" hidden>Drag cards to move them. Click a card to edit it, or hand it to an agent with Run in a session.</span>
    <span id="truncated" class="muted"></span>
    <button id="add-samples" type="button" hidden>Add sample cards</button>
  </div>
</header>
<div id="message" aria-live="polite"></div>
<p id="move-help" class="sr-only">Drag the card to move it. With a keyboard, press Space, move with the arrow keys, and press Space again to drop. Escape cancels.</p>
<div id="announce" class="sr-only" aria-live="assertive"></div>
<main id="board" class="board" aria-label="Board"></main>
`;

export const REFERENCE_KANBAN_HTML = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Team board</title>
<style>${STYLE}</style>
</head>
<body>
${BODY}
<script>${SCRIPT}</script>
</body>
</html>
`;

// Matches every reference Kanban revision, including older ones without the runs collection,
// so the page offers to upgrade those but never to overwrite an unrelated thingy.
export function isReferenceKanbanHtml(html: string): boolean {
  return html.includes("<title>Team board</title>");
}

export const REFERENCE_KANBAN_WIP_LIMIT = 5;

/** What an agent reads through `sys_thingy_describe` before it works with the board. */
export const REFERENCE_KANBAN_GUIDE = [
  "# Team board",
  "",
  "A Kanban board shared by the people who can open this thingy.",
  "",
  "## cards",
  "",
  "One record per card. `column` is `todo` (Backlog), `doing` (In progress), `review` or `done`, shown in that order.",
  "`order` sorts cards within a column (smaller first); put a moved card between its neighbours.",
  "`notes` is Markdown, and `checklist` holds `{text, done}` items. Keep `title` short; details belong in `notes`.",
  `In progress holds at most ${REFERENCE_KANBAN_WIP_LIMIT} cards. When it is full, move a card on before starting another.`,
  "",
  "## Actions",
  "",
  "`run_card` (params `cardId`, `runId`): someone asked an agent to work on a card. Read the card, then do what its",
  "title, notes and checklist ask. Report through a `runs` record with ID `runId` (below). Use a child session for each",
  "card and never run two actions for the same card at once; a second request for a card that is already running is",
  "merged into the first.",
  "",
  "## runs",
  "",
  "One record per agent session working on a card, written by that session.",
  "`cardId` is the card record ID. Set `state` to `running`, `needs_input`, `done` or `failed`, put a short Markdown",
  "`summary` of the progress or result, and your Omnigent session ID in `sessionId`. Update the same record as you go.",
  "Deleting a card deletes its runs. Do not edit people's cards unless asked; report through your run instead.",
].join("\n");

export const REFERENCE_KANBAN_REVISION: CreateThingyRevisionRequest = {
  html: REFERENCE_KANBAN_HTML,
  sdk_version: 1,
  capabilities: ["data.read", "data.write"],
  collections: {
    [REFERENCE_KANBAN_COLLECTION]: REFERENCE_KANBAN_CARD_SCHEMA,
    [REFERENCE_KANBAN_RUNS_COLLECTION]: REFERENCE_KANBAN_RUN_SCHEMA,
  },
  guide: REFERENCE_KANBAN_GUIDE,
  actions: [
    {
      name: "run_card",
      description:
        "Work on one card. Read the cards record params.cardId and do what its title, notes and checklist ask. " +
        'Create the runs record with ID params.runId: {cardId, state: "running", summary: one line on what you are doing, ' +
        'sessionId: the session doing the work}; update it as the work goes; finish with state "done" or "failed" and a ' +
        'short Markdown summary, or "needs_input" while you wait for the person. Do not edit the card. Use a child ' +
        "session per card, and never two actions for the same card at once.",
      params: {
        type: "object",
        additionalProperties: false,
        required: ["cardId", "runId"],
        properties: {
          cardId: { type: "string", maxLength: 128 },
          runId: { type: "string", maxLength: 64 },
        },
      },
    },
  ],
  constraints: [
    {
      name: "wip_limit",
      kind: "max_records",
      collection: REFERENCE_KANBAN_COLLECTION,
      where: { column: "doing" },
      max: REFERENCE_KANBAN_WIP_LIMIT,
      message: `In progress holds at most ${REFERENCE_KANBAN_WIP_LIMIT} cards. Move one on first.`,
    },
    {
      name: "run_card",
      kind: "reference",
      collection: REFERENCE_KANBAN_RUNS_COLLECTION,
      field: "cardId",
      target: REFERENCE_KANBAN_COLLECTION,
      on_delete: "cascade",
      message: "A run must belong to a card on this board.",
    },
  ],
};
