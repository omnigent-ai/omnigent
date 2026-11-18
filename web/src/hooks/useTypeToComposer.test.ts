import { cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

let embedded = false;
vi.mock("@/lib/embedded", () => ({ useIsEmbedded: () => embedded }));

import { setEmbedRoot } from "@/lib/host";
import { typesIntoComposer, useTypeToComposer } from "./useTypeToComposer";

afterEach(() => {
  cleanup();
  embedded = false;
  setEmbedRoot(null);
  document.body.innerHTML = "";
});

function key(init: KeyboardEventInit): KeyboardEvent {
  return new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init });
}

/** A conversation area holding a focusable button, plus a sidebar button outside it. */
function layout() {
  document.body.innerHTML = `
    <nav><button id="sidebar">Session</button></nav>
    <div id="conversation"><p>reply</p><button id="copy">Copy</button><input id="other" /></div>
    <div role="dialog"><button id="dialog">OK</button></div>`;
  const byId = (id: string) => document.getElementById(id) as HTMLElement;
  return { conversation: byId("conversation"), byId };
}

describe("typesIntoComposer", () => {
  it("takes a typed character from the conversation, or from nowhere", () => {
    const { conversation, byId } = layout();
    expect(typesIntoComposer(key({ key: "h" }), conversation)).toBe(true);
    byId("copy").focus();
    expect(typesIntoComposer(key({ key: "H", shiftKey: true }), conversation)).toBe(true);
  });

  it("leaves text fields, other areas and dialogs their keys", () => {
    const { conversation, byId } = layout();
    for (const id of ["other", "sidebar", "dialog"]) {
      byId(id).focus();
      expect(typesIntoComposer(key({ key: "h" }), conversation), id).toBe(false);
    }
  });

  it("leaves a focused dropdown its type-to-select", () => {
    const { conversation } = layout();
    const select = document.createElement("select");
    select.innerHTML = "<option>alpha</option><option>beta</option>";
    conversation.append(select);
    select.focus();
    expect(typesIntoComposer(key({ key: "b" }), conversation)).toBe(false);
  });

  it("leaves keys alone behind an open fullscreen view, with focus still in the conversation", () => {
    const { conversation, byId } = layout();
    byId("copy").focus();
    const fullscreen = document.createElement("div");
    fullscreen.setAttribute("data-fullscreen-overlay", "");
    document.body.append(fullscreen);
    expect(typesIntoComposer(key({ key: "h" }), conversation)).toBe(false);
    fullscreen.remove();
    expect(typesIntoComposer(key({ key: "h" }), conversation)).toBe(true);
  });

  it("ignores chords, Space, named keys, claimed keys and IME composition", () => {
    const { conversation } = layout();
    const claimed = key({ key: "1" });
    claimed.preventDefault();
    for (const event of [
      key({ key: "k", metaKey: true }),
      key({ key: "k", ctrlKey: true }),
      key({ key: "k", altKey: true }),
      key({ key: " " }),
      key({ key: "Enter" }),
      key({ key: "ArrowDown" }),
      key({ key: "a", isComposing: true }),
      claimed,
    ]) {
      expect(typesIntoComposer(event, conversation), event.key).toBe(false);
    }
  });
});

describe("useTypeToComposer", () => {
  it("focuses the composer for a typed character until disabled", () => {
    const { conversation } = layout();
    const focusComposer = vi.fn();
    const { rerender } = renderHook(
      ({ enabled }) => useTypeToComposer(focusComposer, { current: conversation }, enabled),
      { initialProps: { enabled: true } },
    );

    window.dispatchEvent(key({ key: "h" }));
    expect(focusComposer).toHaveBeenCalledTimes(1);
    rerender({ enabled: false });
    window.dispatchEvent(key({ key: "h" }));
    expect(focusComposer).toHaveBeenCalledTimes(1);
  });
});

describe("useTypeToComposer embedded", () => {
  it("takes typing from inside the embedded app, not from the host page around it", () => {
    document.body.innerHTML = `
      <p id="host">Host page</p>
      <div id="app"><div id="conversation"><p>reply</p></div></div>`;
    const app = document.getElementById("app")!;
    embedded = true;
    setEmbedRoot(app);
    const focusComposer = vi.fn();
    renderHook(() =>
      useTypeToComposer(focusComposer, { current: document.getElementById("conversation") }),
    );

    document.getElementById("host")!.dispatchEvent(key({ key: "h" }));
    expect(focusComposer).not.toHaveBeenCalled();
    document.getElementById("conversation")!.dispatchEvent(key({ key: "h" }));
    expect(focusComposer).toHaveBeenCalledTimes(1);
  });

  it("takes typing after a click on transcript text focused the embed's root", () => {
    document.body.innerHTML = `
      <div id="app" tabindex="-1"><div id="conversation"><p>reply</p></div></div>`;
    const app = document.getElementById("app")!;
    embedded = true;
    setEmbedRoot(app);
    const focusComposer = vi.fn();
    renderHook(() =>
      useTypeToComposer(focusComposer, { current: document.getElementById("conversation") }),
    );
    app.focus();
    expect(document.activeElement).toBe(app);

    app.dispatchEvent(key({ key: "h" }));
    expect(focusComposer).toHaveBeenCalledTimes(1);
  });
});
