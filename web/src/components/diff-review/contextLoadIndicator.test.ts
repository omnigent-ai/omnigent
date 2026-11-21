import { describe, expect, it, vi } from "vitest";
import { markContextLoads } from "./contextLoadIndicator";

/** A rendered file: a host whose shadow root holds an expand control. */
function renderedFile(id: string) {
  const element = document.createElement("div");
  const root = element.attachShadow({ mode: "open" });
  root.innerHTML = `<div data-separator><div data-expand-button data-expand-up></div><div data-separator-content><span data-unmodified-lines>38 unmodified lines</span></div></div>`;
  const expand = root.querySelector<HTMLElement>("[data-expand-button]")!;
  const label = root.querySelector<HTMLElement>("[data-unmodified-lines]")!;
  const content = root.querySelector<HTMLElement>("[data-separator-content]")!;
  const clicks = vi.fn();
  // The library listens inside the shadow root, below the host.
  root.addEventListener("click", clicks);
  document.body.append(element);
  return { id, element, expand, label, content, clicks };
}

describe("markContextLoads", () => {
  it("marks only the file whose lines load or failed, and clears it after", () => {
    const loading = renderedFile("a");
    const idle = renderedFile("b");
    const failed = renderedFile("c");
    const states = new Map([
      ["a", "loading" as const],
      ["c", "failed" as const],
    ]);
    markContextLoads([loading, idle, failed], (id) => states.get(id));
    expect(loading.element.hasAttribute("data-context-loading")).toBe(true);
    expect(idle.element.hasAttribute("data-context-loading")).toBe(false);
    expect(idle.element.hasAttribute("data-context-failed")).toBe(false);
    expect(failed.element.hasAttribute("data-context-failed")).toBe(true);

    // A recycled host shows its new file's state.
    markContextLoads([loading, idle, failed], () => undefined);
    for (const file of [loading, idle, failed]) {
      expect(file.element.hasAttribute("data-context-loading")).toBe(false);
      expect(file.element.hasAttribute("data-context-failed")).toBe(false);
    }
  });

  it("keeps a loading file's expand controls from expanding more, and only those", () => {
    const loading = renderedFile("a");
    const idle = renderedFile("b");
    markContextLoads([loading, idle], (id) => (id === "a" ? "loading" : undefined));
    // Twice, as a repeated click or an assistive tool's activation would.
    loading.expand.click();
    loading.expand.click();
    loading.label.click();
    expect(loading.clicks).not.toHaveBeenCalled();
    // Other parts of the loading file, and the other file, still get their clicks.
    loading.content.click();
    expect(loading.clicks).toHaveBeenCalledTimes(1);
    idle.expand.click();
    expect(idle.clicks).toHaveBeenCalledTimes(1);

    markContextLoads([loading, idle], () => undefined);
    loading.expand.click();
    expect(loading.clicks).toHaveBeenCalledTimes(2);
  });
});
