"use strict";

// The pages, and the server that serves them, for
// desktop_browser_agent_input.e2e.js.

const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");

const FIXTURE_PAGE = `<!doctype html>
<html><head><title>Agent input fixture</title><style>
  body { margin: 0; font: 14px sans-serif; }
  #menu-trigger:hover + #menu, #menu:hover { display: block; }
  #menu { display: none; position: absolute; left: 20px; top: 56px; background: #ffd; padding: 8px; }
  #drop, #lane { width: 160px; height: 60px; border: 2px dashed #888; }
  #scroller { height: 80px; overflow: auto; border: 1px solid #ccc; }
  #scroller div { height: 400px; }
  #spacer { height: 2000px; }
</style></head><body>
  <button id="menu-trigger" style="margin:20px">Menu</button>
  <div id="menu">Hover menu open</div>
  <div id="ctx" style="margin:20px;padding:8px;background:#eef">Right-click me</div>
  <button id="dbl">Double-click me</button>
  <div id="card" draggable="true" style="width:80px;padding:8px;background:#fcc">Card</div>
  <div id="drop">Drop zone</div>
  <div id="knob" style="width:40px;height:30px;background:#cfc;touch-action:none">Knob</div>
  <div id="lane">Pointer lane</div>
  <form id="form"><input id="q" aria-label="Query"></form>
  <select id="fruit"><option value="a">Apple</option><option value="b">Banana</option></select>
  <label><input id="agree" type="checkbox"> Agree</label>
  <button id="tap">Tap</button>
  <div id="editor" contenteditable="true" style="border:1px solid #999;padding:4px">old text</div>
  <input id="qty" type="number" value="5">
  <input id="when" type="date">
  <div id="fruit-wrap" style="display:inline-block;padding:2px"><select id="veg"><option>Kale</option></select></div>
  <select id="hidden-select" style="display:none"><option value="a">A</option><option value="b">B</option></select>
  <label id="hue-label" for="hue" style="display:inline-block;padding:4px">Hue</label><input id="hue" type="color">
  <label id="terms-label" style="display:inline-block;padding:4px 40px;background:#eee;text-align:center">
    <input id="terms" type="checkbox" style="position:absolute;left:-9999px">I agree to the <a id="terms-link" href="#terms-opened">Terms</a>
  </label>
  <label id="links-only" style="display:inline-block"><input id="links-box" type="checkbox" style="position:absolute;left:-9999px"><a href="#links-opened" style="display:block;padding:4px">Only a link</a></label>
  <label id="fancy-label" style="display:inline-block;padding:4px;background:#eee">
    <input id="fancy" type="checkbox" style="opacity:0;position:absolute;width:1px;height:1px">Fancy switch
  </label>
  <div style="position:relative;display:inline-block">
    <button id="buried">Buried</button>
    <div id="sheet" style="position:absolute;inset:0;background:rgba(0,0,0,.1)">sheet</div>
  </div>
  <input id="file" type="file">
  <input id="alt-moves" aria-label="Alt moves focus"><input id="alt-elsewhere" aria-label="Elsewhere">
  <input id="alt-picks" aria-label="Alt focuses the select">
  <button id="late-upload-500">Upload later</button><input id="late-file-500" type="file">
  <button id="late-upload-1000">Upload much later</button><input id="late-file-1000" type="file">
  <div id="scroller"><div>Tall content</div></div>
  <div id="spacer"></div>
  <script>
    window.log = [];
    const note = (entry) => window.log.push(entry);
    ctx.addEventListener("contextmenu", (e) => { e.preventDefault(); note("contextmenu:" + e.isTrusted); });
    dbl.addEventListener("dblclick", (e) => note("dblclick:" + e.isTrusted));
    card.addEventListener("dragstart", (e) => e.dataTransfer.setData("text/plain", "card"));
    drop.addEventListener("dragover", (e) => e.preventDefault());
    drop.addEventListener("drop", (e) => { e.preventDefault(); drop.textContent = "Dropped " + e.dataTransfer.getData("text/plain"); });
    let dragging = false;
    knob.addEventListener("pointerdown", () => { dragging = true; });
    document.addEventListener("pointerup", (e) => {
      if (!dragging) return;
      dragging = false;
      if (document.elementFromPoint(e.clientX, e.clientY) === lane) lane.textContent = "Knob landed";
    });
    form.addEventListener("submit", (e) => { e.preventDefault(); note("submit:" + q.value); });
    q.addEventListener("keydown", (e) => { if (e.key === "Escape") note("escape"); });
    window.trusted = [];
    const trust = (e) => window.trusted.push(e.type + (e.target.id ? "#" + e.target.id : "") + ":" + e.isTrusted);
    for (const type of ["pointerdown", "mousedown", "click"]) tap.addEventListener(type, trust);
    for (const type of ["beforeinput", "input", "change"]) {
      editor.addEventListener(type, trust);
      q.addEventListener(type, trust);
    }
    fancy.addEventListener("click", trust);
    document.addEventListener("wheel", trust, { passive: true });
    // Alt's keydown moves focus: to another field, or to the native select.
    window.keys = [];
    document.getElementById("alt-moves").addEventListener("keydown", (e) => {
      if (e.key === "Alt") document.getElementById("alt-elsewhere").focus();
    });
    document.getElementById("alt-picks").addEventListener("keydown", (e) => {
      if (e.key === "Alt") fruit.focus();
    });
    document.addEventListener("keydown", (e) => window.keys.push("down:" + e.key + "@" + e.target.id), true);
    document.addEventListener("keyup", (e) => window.keys.push("up:" + e.key + "@" + e.target.id), true);
    // Upload buttons that open their file input after a timer, on the
    // click's activation.
    window.lateUploads = [];
    for (const ms of [500, 1000]) {
      document.getElementById("late-upload-" + ms).addEventListener("click", () => {
        setTimeout(() => {
          window.lateUploads.push(ms + ":" + navigator.userActivation.isActive);
          document.getElementById("late-file-" + ms).click();
        }, ms);
      });
    }
  </script>
</body></html>`;

// A board whose card and far column can't share the viewport, with an item
// that would sit under the card's old position once the column is scrolled in.
const BOARD_PAGE = `<!doctype html>
<html><head><title>Long board</title><style>
  body { margin: 0; font: 14px sans-serif; }
  .item { height: 60px; margin: 4px; background: #eef; }
</style></head><body>
  <div id="far-card" draggable="true" class="item">Card</div>
  <div style="height: 3000px"></div>
  <div id="bystander" class="item" draggable="true">Bystander</div>
  <div id="far-column" style="height: 200px; border: 2px dashed #888">Column</div>
  <div style="height: 3000px"></div>
  <script>
    window.events = [];
    for (const type of ["pointerdown", "dragstart", "drop"]) {
      document.addEventListener(type, (e) => window.events.push(type + "#" + (e.target.id || e.target.tagName)), true);
    }
  </script>
</body></html>`;

// A sortable list: the drag's first dragover on the column inserts a
// placeholder that pushes it down, and a trash zone takes its old place.
const SORTABLE_PAGE = `<!doctype html>
<html><head><title>Sortable</title><style>
  body { margin: 0; font: 14px sans-serif; }
  #item { width: 80px; padding: 8px; background: #fcc; }
  #column, #trash { width: 200px; height: 60px; border: 2px dashed #888; }
  #placeholder { height: 120px; background: #eee; }
</style></head><body>
  <div id="item" draggable="true">Item</div>
  <div id="slot"></div>
  <div id="column">Column</div>
  <script>
    window.drops = [];
    item.addEventListener("dragstart", (e) => e.dataTransfer.setData("text/plain", "item"));
    let moved = false;
    column.addEventListener("dragover", (e) => {
      e.preventDefault();
      if (moved) return;
      moved = true;
      // The trash zone sits where the column was; the column moves below.
      slot.innerHTML = '<div id="trash">Trash</div><div id="placeholder"></div>';
      trash.addEventListener("dragover", (e) => e.preventDefault());
      trash.addEventListener("drop", (e) => { e.preventDefault(); window.drops.push("trash"); });
    });
    column.addEventListener("drop", (e) => { e.preventDefault(); window.drops.push("column"); });
  </script>
</body></html>`;

// Off-screen controls in a smooth-scrolling page and a smooth-scrolling container.
// Focusing the field sends the tab to another page with a focused field.
const HANDOFF_PAGE = `<!doctype html>
<title>Handoff</title>
<input id="q" onfocus="location.href = '/landing'">`;
// Focusing the first field hands focus to the second a moment later.
const FOCUS_THIEF_PAGE = `<!doctype html>
<title>Focus thief</title>
<input id="wanted" onfocus="queueMicrotask(() => window.other.focus())">
<input id="other">`;
// The editor puts the caret at its end on focus, after the page selected it.
const CARET_EDITOR_PAGE = `<!doctype html>
<title>Caret editor</title>
<div id="ed" contenteditable="true"
  onfocus="queueMicrotask(() => getSelection().collapse(this, this.childNodes.length))">old text</div>`;
// Hovering the button shows another one over it.
const HOVER_COVER_PAGE = `<!doctype html>
<title>Hover cover</title>
<script>window.clicks = [];</script>
<div style="position: relative; display: inline-block; margin: 40px">
  <button id="target" style="width: 120px; height: 40px" onclick="clicks.push('target')"
    onpointerenter="cover.style.display = 'block'">Target</button>
  <button id="cover" onclick="clicks.push('cover')"
    style="display: none; position: absolute; inset: 0; width: 120px; height: 40px">Cover</button>
</div>`;
// Hovering the button replaces it with another one, by the same selector,
// further down: what the agent measured is gone.
const REPLACED_ON_HOVER_PAGE = `<!doctype html>
<title>Replaced on hover</title>
<script>window.clicks = [];
function swap(old) {
  const next = document.createElement("button");
  next.id = "buy"; next.textContent = "Buy (new)";
  next.style.cssText = "display: block; margin: 120px 40px; width: 120px; height: 40px";
  next.onclick = () => clicks.push("new");
  old.replaceWith(next);
}</script>
<button id="buy" style="display: block; margin: 40px; width: 120px; height: 40px"
  onclick="clicks.push('old')" onpointerenter="swap(this)">Buy</button>`;
const LANDING_PAGE = `<!doctype html>
<title>Landing</title>
<input id="landing" autofocus>`;

const SMOOTH_PAGE = `<!doctype html>
<html style="scroll-behavior: smooth"><head><title>Smooth</title></head><body style="margin:0">
  <div style="height: 2400px"></div>
  <label><input id="far-check" type="checkbox"> Far</label>
  <div id="inner" style="height: 120px; overflow: auto; scroll-behavior: smooth; border: 1px solid #ccc">
    <div style="height: 900px"></div>
    <label><input id="inner-check" type="checkbox"> Inner</label>
    <input id="inner-field" aria-label="Inner field">
  </div>
  <div style="height: 2400px"></div>
</body></html>`;

// An edited page that asks to stay when left, counting its loads.
const GUARDED_PAGE = `<!doctype html>
<title>Guarded</title>
<textarea id="notes" aria-label="Notes"></textarea>
<script>
  sessionStorage.loads = String(Number(sessionStorage.loads || 0) + 1);
  addEventListener("beforeunload", (e) => {
    if (notes.value) e.preventDefault();
  });
</script>`;
// Two pages with the same form control, the second served when the test says.
const FORM_PAGE = (title) => `<!doctype html>
<title>${title}</title>
<select id="pick" aria-label="Pick"><option value="x">X</option><option value="y">Y</option></select>`;

// Agents may reach loopback pages only as an eligible localhost preview; a
// test turns that on for this process with global.omnigentTestLocalPreviews.
function localPreviewsPreload(dir) {
  const file = path.join(dir, "local-previews.cjs");
  fs.writeFileSync(
    file,
    `const mod = require(${JSON.stringify(path.join(__dirname, "../src/browserViewRegistry.js"))});\n` +
      `const create = mod.createBrowserViewRegistry;\n` +
      `mod.createBrowserViewRegistry = (deps = {}) => create({ ...deps, allowsLocalhost: (c) =>\n` +
      `  global.omnigentTestLocalPreviews === true || (deps.allowsLocalhost ? deps.allowsLocalhost(c) : false) });\n`,
  );
  return file;
}

function startFixture() {
  let held = null;
  const server = http.createServer((req, res) => {
    if (req.url === "/held-form") {
      // Answered only once the test releases it, so the tab stays mid-navigation.
      const answer = () => {
        res.writeHead(200, { "Content-Type": "text/html" });
        res.end(FORM_PAGE("Held form"));
      };
      if (held) held.waiting.push(answer);
      else answer();
      return;
    }
    res.writeHead(200, { "Content-Type": "text/html" });
    const pages = {
      "/second": "<title>Second</title><p>Second page</p>",
      "/linker":
        '<title>Linker</title><a id="to-second" href="/second" style="font-size:40px">Second</a>',
      "/board": BOARD_PAGE,
      "/sortable": SORTABLE_PAGE,
      "/smooth": SMOOTH_PAGE,
      "/handoff": HANDOFF_PAGE,
      "/landing": LANDING_PAGE,
      "/focus-thief": FOCUS_THIEF_PAGE,
      "/caret-editor": CARET_EDITOR_PAGE,
      "/hover-cover": HOVER_COVER_PAGE,
      "/replaced-on-hover": REPLACED_ON_HOVER_PAGE,
      "/guarded": GUARDED_PAGE,
      "/form": FORM_PAGE("Form"),
    };
    res.end(pages[req.url] ?? FIXTURE_PAGE);
  });
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () =>
      resolve({
        url: `http://127.0.0.1:${server.address().port}/`,
        /** Hold /held-form until the returned function is called. */
        holdForm: () => {
          held = { waiting: [] };
          const current = held;
          return () => {
            if (held === current) held = null;
            for (const answer of current.waiting) answer();
          };
        },
        close: () =>
          new Promise((done) => {
            server.close(done);
          }),
      }),
    );
  });
}

module.exports = { localPreviewsPreload, startFixture };
