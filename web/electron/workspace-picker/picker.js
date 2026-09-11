// Renderer for the workspace picker modal. Talks to main only through the
// preload-exposed `window.workspacePicker` (see workspace_picker_preload.js).

"use strict";

const MAX_ROWS = 200; // cap rendered rows; accounts can have hundreds of workspaces

const searchEl = document.getElementById("search");
const listEl = document.getElementById("list");
const countEl = document.getElementById("count");
const cancelEl = document.getElementById("cancel");

let workspaces = [];
let filtered = [];
let activeIndex = 0;

function render() {
  const q = searchEl.value.trim().toLowerCase();
  filtered = q
    ? workspaces.filter((w) => w.name.toLowerCase().includes(q) || w.fqdn.toLowerCase().includes(q))
    : workspaces;
  activeIndex = 0;
  const shown = filtered.slice(0, MAX_ROWS);
  listEl.replaceChildren();
  shown.forEach((w, i) => {
    const li = document.createElement("li");
    li.dataset.index = String(i);
    if (i === activeIndex) li.classList.add("active");
    const name = document.createElement("div");
    name.className = "name";
    name.textContent = w.name;
    const fqdn = document.createElement("div");
    fqdn.className = "fqdn";
    fqdn.textContent = w.fqdn;
    li.append(name, fqdn);
    li.addEventListener("click", () => window.workspacePicker.choose(w.workspaceId));
    listEl.appendChild(li);
  });
  const total = filtered.length;
  countEl.textContent =
    total > MAX_ROWS ? `Showing ${MAX_ROWS} of ${total} — refine your search` : `${total} workspace(s)`;
}

function setActive(next) {
  const rows = listEl.children;
  if (rows.length === 0) return;
  activeIndex = (next + rows.length) % rows.length;
  for (let i = 0; i < rows.length; i++) rows[i].classList.toggle("active", i === activeIndex);
  rows[activeIndex].scrollIntoView({ block: "nearest" });
}

searchEl.addEventListener("input", render);
cancelEl.addEventListener("click", () => window.workspacePicker.cancel());

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    window.workspacePicker.cancel();
  } else if (e.key === "ArrowDown") {
    e.preventDefault();
    setActive(activeIndex + 1);
  } else if (e.key === "ArrowUp") {
    e.preventDefault();
    setActive(activeIndex - 1);
  } else if (e.key === "Enter") {
    const picked = filtered[activeIndex];
    if (picked) window.workspacePicker.choose(picked.workspaceId);
  }
});

window.workspacePicker.list().then((ws) => {
  workspaces = Array.isArray(ws) ? ws : [];
  render();
});
