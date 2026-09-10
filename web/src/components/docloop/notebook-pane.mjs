/** Imperative pane behind a thin native React wrapper; no replacement chat UI. */
const draftsBySession = new Map();
const LIMIT = 256 * 1024;
// Retained drafts must still warn when the pane is closed or another session is open.
if (typeof window !== "undefined")
  window.addEventListener("beforeunload", (event) => {
    if ([...draftsBySession.values()].some((state) => state.drafts.size || state.pendingCreate)) {
      event.preventDefault();
      event.returnValue = "";
    }
  });

function textElement(tag, text, cls) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = String(text);
  if (cls) element.className = cls;
  return element;
}
function button(text, action) {
  const element = textElement("button", text);
  element.type = "button";
  element.addEventListener("click", action);
  return element;
}
function validSnapshot(value, sessionId) {
  return (
    value &&
    value.schema_version === 1 &&
    value.session_id === sessionId &&
    /^[a-f0-9]{64}$/.test(value.binding_id) &&
    /^[a-f0-9]{64}$/.test(value.revision) &&
    ["org", "ipynb"].includes(value.format) &&
    Array.isArray(value.nodes) &&
    value.nodes.every(
      (node) =>
        node &&
        typeof node.id === "string" &&
        typeof node.source === "string" &&
        typeof node.editable === "boolean",
    )
  );
}

/** Draft text remains only in this tab's memory, keyed by session AND binding. */
export function mountNotebookPane(host, { sessionId, fetcher, pollMs = 2500 }) {
  if (!/^[A-Za-z0-9_-]{1,128}$/.test(sessionId)) throw new Error("Invalid session ID");
  const root = host.shadowRoot || host.attachShadow({ mode: "open" });
  root.replaceChildren();
  const style = textElement(
    "style",
    `
    :host{display:block;height:100%;min-height:0;color:inherit;font:14px system-ui,sans-serif}
    *{box-sizing:border-box} .pane{height:100%;display:flex;flex-direction:column;min-height:0}
    header{padding:12px;border-bottom:1px solid #8886;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
    h2{font-size:16px;margin:0;margin-right:auto}button,select{font:inherit;min-height:44px;padding:6px 10px;background:transparent;color:inherit;border:1px solid #8888;border-radius:6px;cursor:pointer}
    button:disabled{opacity:.5;cursor:not-allowed} .summary,.notice{padding:8px 12px;font-size:12px;overflow-wrap:anywhere}
    .notice:empty{display:none}.notice{border-bottom:1px solid #8886}.body{overflow:auto;flex:1;padding:12px}
    .node{border:1px solid #8886;border-radius:8px;margin-bottom:12px;padding:10px}.meta{font-size:12px;opacity:.8;display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px}
    textarea{font:16px/1.5 ui-monospace,monospace;width:100%;min-height:100px;resize:vertical;background:transparent;color:inherit;border:1px solid #8886;border-radius:5px;padding:8px}
    pre{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.5 ui-monospace,monospace;margin:6px 0}.actions{display:flex;gap:8px;flex-wrap:wrap;padding-top:8px}.output{border-top:1px solid #8886;margin-top:8px;padding-top:8px}
    .error{border-left:3px solid #b66}.dirty{border-left:3px solid #b98b3b}.empty{padding:20px}.hint{font-size:12px;opacity:.8;padding:8px 12px;border-top:1px solid #8886}
  `,
  );
  const pane = textElement("section", undefined, "pane");
  pane.setAttribute("aria-label", "Docloop notebook");
  const header = textElement("header");
  const title = textElement("h2", "Notebook / Org");
  const mode = document.createElement("select");
  mode.setAttribute("aria-label", "Document view");
  for (const [value, label] of [
    ["notebook", "Notebook"],
    ["outline", "Outline"],
  ]) {
    const option = textElement("option", label);
    option.value = value;
    mode.append(option);
  }
  const refresh = button("Refresh", () => load(true));
  const addNote = button("+ Note", () => create("markdown"));
  const addCode = button("+ Cell", () => create("code"));
  header.append(title, mode, refresh, addNote, addCode);
  const summary = textElement("div", "Loading the session-bound document…", "summary");
  const notice = textElement("div", "", "notice");
  notice.setAttribute("role", "status");
  notice.setAttribute("aria-live", "polite");
  const body = textElement("div", undefined, "body");
  const hint = textElement(
    "div",
    "Use Chat to ask the agent to change the notebook, run cells, or work with files.",
    "hint",
  );
  pane.append(header, summary, notice, body, hint);
  root.append(style, pane);

  let snapshot = null,
    disposed = false,
    timer = null,
    reading = false,
    saving = false,
    generation = 0;
  let readController = null,
    writeController = null;
  const state = draftsBySession.get(sessionId) || { binding: null, drafts: new Map() };
  draftsBySession.set(sessionId, state);
  const endpoint = `/v1/sessions/${encodeURIComponent(sessionId)}/docloop/document`;
  const nodes = new Map();

  function report(message, error = false) {
    notice.textContent = message;
    notice.classList.toggle("error", error);
  }
  function busy() {
    refresh.disabled = reading || saving;
    const blocked = !snapshot || saving || reading || state.drafts.size > 0;
    addNote.disabled = blocked;
    addCode.disabled = blocked;
    if (state.pendingCreate) {
      addNote.textContent = "Retry add";
      addCode.disabled = true;
    } else addNote.textContent = "+ Note";
  }
  async function readJSON(response) {
    let value;
    try {
      value = await response.json();
    } catch {
      throw new Error(`Invalid notebook response (${response.status})`);
    }
    if (!response.ok) {
      const message = value.error || value.detail || `Notebook request failed (${response.status})`;
      const error = new Error(typeof message === "string" ? message : JSON.stringify(message));
      error.status = response.status;
      throw error;
    }
    return value;
  }
  function accept(value) {
    if (!validSnapshot(value, sessionId))
      throw new Error("Notebook response does not match this session or supported schema");
    if (
      state.binding &&
      state.binding !== value.binding_id &&
      (state.drafts.size || state.pendingCreate)
    ) {
      // Never transfer old-document drafts to a replacement binding.
      body.replaceChildren();
      nodes.clear();
      for (const [id, draft] of state.drafts) {
        const retained = textElement("article", undefined, "node");
        retained.append(
          textElement("strong", `Retained old-document draft: ${id}`),
          textElement("pre", draft.source),
        );
        body.append(retained);
      }
      body.append(
        button("Discard old-document drafts and reload", () => {
          if (
            !confirm(
              "Discard these drafts and any pending save/creation receipt? Unknown writes may already have been applied to the OLD document.",
            )
          )
            return;
          state.drafts.clear();
          state.pendingCreate = null;
          state.binding = null;
          snapshot = null;
          load(true);
        }),
      );
      snapshot = null;
      throw new Error(
        "Session document binding changed. Old drafts are shown below for copying; they cannot be applied to the new document.",
      );
    }
    state.binding = value.binding_id;
    snapshot = value;
    title.textContent = value.document_name;
    summary.textContent = `Session ${sessionId} · ${value.format.toUpperCase()} · revision ${value.revision.slice(0, 12)}`;
    render();
  }
  async function load(manual = false) {
    if (disposed || reading || saving) return;
    reading = true;
    busy();
    const current = ++generation;
    readController = new AbortController();
    try {
      const response = await fetcher(endpoint, {
        signal: readController.signal,
        cache: "no-store",
      });
      const value = await readJSON(response);
      if (disposed || generation !== current) return;
      accept(value);
      if (manual)
        report(
          state.drafts.size
            ? "Refreshed; your unsaved drafts are retained."
            : "Document is current.",
        );
      else if (!state.drafts.size) report("");
    } catch (error) {
      if (!disposed && error.name !== "AbortError") report(error.message, true);
    } finally {
      if (!disposed) {
        reading = false;
        // accept() rendered while reading was true. Refresh the cards as well
        // as the header, or retained pending saves stay permanently disabled.
        render();
        busy();
      }
    }
  }
  function draftFor(node) {
    let draft = state.drafts.get(node.id);
    if (!draft) {
      draft = {
        source: node.source,
        original: node.source,
        revision: snapshot.revision,
        binding: snapshot.binding_id,
        pending: null,
      };
      state.drafts.set(node.id, draft);
    }
    return draft;
  }
  function updateCard(card, node) {
    const draft = state.drafts.get(node.id);
    card.node = node;
    card.article.classList.toggle("dirty", Boolean(draft));
    card.meta.textContent = `${node.kind} · ${node.id}${node.role ? ` · ${node.role}` : ""}`;
    card.area.readOnly = !node.editable || saving || Boolean(draft?.pending);
    card.area.setAttribute("aria-label", `Source ${node.id}`);
    if (!draft && card.area.value !== node.source) card.area.value = node.source;
    if (draft && card.area.value !== draft.source) card.area.value = draft.source;
    const changed =
      draft && (node.source !== draft.original || snapshot.revision !== draft.revision);
    card.status.textContent = node.source_truncated
      ? "Source is truncated: open the native file to edit."
      : !node.editable
        ? "Recorded or pinned source — read-only."
        : draft?.pending
          ? "Save outcome unknown. Retry the same save to reconcile; the draft is frozen."
          : changed
            ? "Document advanced since this draft. Compare with current before retrying."
            : draft
              ? "Unsaved draft."
              : "";
    card.save.textContent = draft?.pending ? "Retry same save" : "Save";
    card.save.disabled = !draft || saving || reading || !node.editable;
    card.discard.disabled = !draft || saving;
    card.compare.hidden = !changed;
    card.output.textContent = node.output_text || "";
    if (node.output_truncated) card.output.textContent += "\n[Output truncated]";
    card.output.hidden = !node.output_text && !node.output_truncated;
  }
  function render() {
    if (!snapshot) return;
    const present = new Set();
    const focused = root.activeElement;
    const selection =
      focused?.tagName === "TEXTAREA" ? [focused.selectionStart, focused.selectionEnd] : null;
    let next = body.firstElementChild;
    for (const node of snapshot.nodes) {
      present.add(node.id);
      let card = nodes.get(node.id);
      if (!card) {
        const article = textElement("article", undefined, "node");
        article.dataset.nodeId = node.id;
        const meta = textElement("div", "", "meta");
        const area = document.createElement("textarea");
        area.spellcheck = false;
        const status = textElement("div", "", "summary");
        const actions = textElement("div", undefined, "actions");
        const save = button("Save", () => saveNode(node.id));
        const discard = button("Discard draft", () => {
          if (
            !confirm(
              "Discard this unsaved draft? A save with unknown outcome may already have reached the server.",
            )
          )
            return;
          state.drafts.delete(node.id);
          render();
          load(true);
        });
        const compare = button("Compare / rebase", () => rebase(node.id));
        const output = textElement("pre", "", "output");
        output.setAttribute("aria-label", `Output ${node.id}`);
        card = { article, meta, area, status, actions, save, discard, compare, output, node };
        area.addEventListener("input", () => {
          const draft = draftFor(card.node);
          if (draft.pending) return;
          draft.source = area.value;
          if (draft.source === draft.original) state.drafts.delete(node.id);
          updateCard(card, card.node);
          busy();
        });
        actions.append(save, discard, compare);
        article.append(meta, area, status, actions, output);
        nodes.set(node.id, card);
      }
      updateCard(card, node);
      card.article.hidden =
        mode.value === "outline" && !["section", "markdown", "item"].includes(node.kind);
      // Moving an already focused textarea can lose its selection in some browsers.
      if (card.article !== next) body.insertBefore(card.article, next);
      next = card.article.nextElementSibling;
    }
    for (const [id, card] of nodes)
      if (!present.has(id)) {
        if (state.drafts.has(id)) {
          card.status.textContent =
            "Node was deleted remotely. Copy or discard this retained draft.";
          card.save.disabled = true;
        } else {
          card.article.remove();
          nodes.delete(id);
        }
      }
    if (selection && focused?.isConnected) {
      focused.focus({ preventScroll: true });
      focused.setSelectionRange(...selection);
    }
    busy();
  }
  async function postEdit(payload) {
    writeController = new AbortController();
    return readJSON(
      await fetcher(endpoint, {
        method: "PATCH",
        signal: writeController.signal,
        headers: { "Content-Type": "application/json", "X-Docloop-Edit": "1" },
        body: JSON.stringify(payload),
      }),
    );
  }
  async function saveNode(id) {
    const draft = state.drafts.get(id);
    if (!draft || saving || reading || disposed) return;
    if (new TextEncoder().encode(draft.source).length > LIMIT) {
      report("Draft exceeds the editable size limit.", true);
      return;
    }
    saving = true;
    ++generation;
    readController?.abort();
    const payload = draft.pending || {
      revision: draft.revision,
      binding_id: draft.binding,
      change_id: crypto.randomUUID(),
      changes: [{ op: "update", id, source: draft.source }],
    };
    draft.pending = payload;
    render();
    try {
      const result = await postEdit(payload);
      if (disposed || state.drafts.get(id) !== draft || draft.pending !== payload) return;
      if (
        !validSnapshot(result.document, sessionId) ||
        result.document.binding_id !== payload.binding_id
      )
        throw new Error("Invalid save receipt binding; retry the same save to reconcile.");
      const remote = result.document.nodes.find((node) => node.id === id);
      if (remote?.source === payload.changes[0].source) state.drafts.delete(id);
      else {
        draft.pending = null;
        report(
          "The save was accepted but the document changed again. Your draft is retained.",
          true,
        );
      }
      accept(result.document);
      if (!state.drafts.has(id))
        report(
          result.replayed
            ? "Previous save confirmed; no duplicate edit was applied."
            : "Saved. The next model inference reads this document.",
        );
    } catch (error) {
      if (disposed || state.drafts.get(id) !== draft || draft.pending !== payload) return;
      if ([400, 403, 404, 409, 413, 415].includes(error.status)) draft.pending = null;
      if (!disposed)
        report(
          error.status === 409
            ? "Save conflict: your draft is retained. Refresh and compare before rebasing."
            : error.message,
          true,
        );
    } finally {
      if (!disposed) {
        saving = false;
        render();
        busy();
      }
    }
  }
  function rebase(id) {
    const draft = state.drafts.get(id),
      current = snapshot?.nodes.find((node) => node.id === id);
    if (!draft || !current || draft.pending) return;
    const comparedRevision = snapshot.revision,
      comparedBinding = snapshot.binding_id;
    const dialog = document.createElement("dialog");
    dialog.append(
      textElement("h3", "Compare before rebasing"),
      textElement("strong", "Current document"),
      textElement("pre", current.source),
      textElement("strong", "Your draft"),
      textElement("pre", draft.source),
    );
    dialog.append(
      button("Keep editing; do not rebase", () => dialog.close()),
      button("Rebase my draft onto this revision", () => {
        draft.original = current.source;
        draft.revision = comparedRevision;
        draft.binding = comparedBinding;
        dialog.close();
        render();
        report("Draft rebased. Review it, then press Save.");
      }),
    );
    dialog.addEventListener("close", () => dialog.remove());
    root.append(dialog);
    dialog.showModal();
  }
  async function create(kind) {
    if (!snapshot || disposed || saving || reading || state.drafts.size) return;
    const id = `ui-${crypto.randomUUID()}`;
    const payload = state.pendingCreate || {
      revision: snapshot.revision,
      binding_id: snapshot.binding_id,
      change_id: crypto.randomUUID(),
      changes: [
        {
          op: "create",
          id,
          kind,
          source: kind === "code" ? 'print("Hello from Docloop")' : "New note",
          language: "python",
        },
      ],
    };
    // Creation is a single explicit action. Unknown outcomes never trigger an automatic second creation.
    state.pendingCreate = payload;
    saving = true;
    busy();
    try {
      const result = await postEdit(payload);
      // Disposing aborts delivery, not necessarily the server commit. Only the
      // current mount/request may clear this retained idempotency receipt.
      if (disposed || state.pendingCreate !== payload) return;
      if (
        !validSnapshot(result.document, sessionId) ||
        result.document.binding_id !== payload.binding_id
      )
        throw new Error("Invalid creation receipt binding");
      state.pendingCreate = null;
      if (!disposed) {
        accept(result.document);
        report("Added to the shared document.");
      }
    } catch (error) {
      if (disposed || state.pendingCreate !== payload) return;
      if ([400, 403, 404, 409, 413, 415].includes(error.status)) state.pendingCreate = null;
      if (!disposed)
        report(
          `${error.message}. Retry add reconciles the same creation; it does not create a second node.`,
          true,
        );
    } finally {
      if (!disposed) {
        saving = false;
        render();
      }
    }
  }
  mode.addEventListener("change", render);
  async function poll() {
    await load();
    if (!disposed) timer = setTimeout(poll, Math.max(1000, pollMs));
  }
  poll();
  busy();
  return {
    refresh: () => load(true),
    dispose() {
      disposed = true;
      ++generation;
      clearTimeout(timer);
      readController?.abort();
      writeController?.abort();
      root.replaceChildren();
      if (!state.drafts.size && !state.pendingCreate) draftsBySession.delete(sessionId);
    },
  };
}
