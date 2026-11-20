import { afterEach, describe, expect, it } from "vitest";

import { isFocusInWorkspaceSidebar, keepSidebarFocusAfterClose } from "./keepSidebarFocus";

/** A sidebar strip with tabs `ids` plus a composer outside it; focuses `focusId`'s tab. */
function layout(ids: string[], focusId: string) {
  document.body.innerHTML = `
    <div data-command-region="conversation"><textarea aria-label="Composer"></textarea></div>
    <aside data-command-region="workspace">
      ${ids.map((id) => `<button role="tab" data-workspace-tab-id="${id}">${id}</button>`).join("")}
    </aside>`;
  document.querySelector<HTMLElement>(`[data-workspace-tab-id="${focusId}"]`)!.focus();
}

const removeTab = (id: string) =>
  document.querySelector(`[data-workspace-tab-id="${id}"]`)!.remove();
const focusedTab = () => document.activeElement?.getAttribute("data-workspace-tab-id") ?? null;

afterEach(() => {
  document.body.innerHTML = "";
});

describe("keepSidebarFocusAfterClose", () => {
  it("moves focus to the tab selected next when the closed tab took it away", async () => {
    layout(["a", "b"], "a");
    const closing = keepSidebarFocusAfterClose(
      Promise.resolve().then(() => {
        removeTab("a");
        return { ok: true };
      }),
      () => "b",
    );
    await closing;
    expect(focusedTab()).toBe("b");
    expect(isFocusInWorkspaceSidebar()).toBe(true);
  });

  it("leaves focus alone when something in the sidebar already holds it", async () => {
    layout(["a", "b", "c"], "a");
    await keepSidebarFocusAfterClose(
      Promise.resolve().then(() => {
        removeTab("a");
        document.querySelector<HTMLElement>('[data-workspace-tab-id="c"]')!.focus();
        return { ok: true };
      }),
      () => "b",
    );
    expect(focusedTab()).toBe("c");
  });

  it("doesn't pull focus into the sidebar when it started elsewhere, or when the close was cancelled", async () => {
    layout(["a", "b"], "a");
    document.querySelector<HTMLElement>("textarea")!.focus();
    await keepSidebarFocusAfterClose(Promise.resolve({ ok: true }), () => "b");
    expect(document.activeElement?.tagName).toBe("TEXTAREA");

    layout(["a", "b"], "a");
    await keepSidebarFocusAfterClose(
      Promise.resolve().then(() => {
        (document.activeElement as HTMLElement).blur();
        return { ok: false };
      }),
      () => "b",
    );
    expect(focusedTab()).toBeNull();
  });

  it("restores sidebar focus after an embedded page's close, though the page's own focus is stale", async () => {
    layout(["a", "b"], "a");
    document.querySelector<HTMLElement>("textarea")!.focus();
    await keepSidebarFocusAfterClose(
      Promise.resolve().then(() => {
        removeTab("a");
        return { ok: true };
      }),
      () => "b",
      true,
    );
    expect(focusedTab()).toBe("b");
  });

  it("leaves focus where the user moved it while the close was pending", async () => {
    layout(["a", "b"], "a");
    let finish!: (result: { ok: boolean }) => void;
    const closing = keepSidebarFocusAfterClose(
      new Promise<{ ok: boolean }>((resolve) => {
        finish = resolve;
      }),
      () => "b",
    );
    // Such as typing in the composer while a shell ends.
    document.querySelector<HTMLElement>("textarea")!.focus();
    removeTab("a");
    finish({ ok: true });
    await closing;
    expect(document.activeElement?.tagName).toBe("TEXTAREA");
  });

  it("still restores focus after a confirmation dialog took it on the way", async () => {
    layout(["a", "b"], "a");
    let finish!: (result: { ok: boolean }) => void;
    const closing = keepSidebarFocusAfterClose(
      new Promise<{ ok: boolean }>((resolve) => {
        finish = resolve;
      }),
      () => "b",
    );
    document.body.insertAdjacentHTML(
      "beforeend",
      '<div role="alertdialog"><button>Close shell</button></div>',
    );
    document.querySelector<HTMLElement>('[role="alertdialog"] button')!.focus();
    document.querySelector('[role="alertdialog"]')!.remove();
    removeTab("a");
    finish({ ok: true });
    await closing;
    expect(focusedTab()).toBe("b");
  });
});
