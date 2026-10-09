import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WorkspaceTabsScroller } from "./WorkspaceTabsScroller";

type ResizeCallback = (entries: { target: Element }[]) => void;

describe("WorkspaceTabsScroller", () => {
  const OriginalResizeObserver = globalThis.ResizeObserver;
  let callbacks: ResizeCallback[];
  let observed: Element[];

  beforeEach(() => {
    callbacks = [];
    observed = [];
    globalThis.ResizeObserver = class {
      constructor(callback: ResizeCallback) {
        callbacks.push(callback);
      }
      observe(target: Element): void {
        observed.push(target);
      }
      unobserve(): void {}
      disconnect(): void {}
    } as unknown as typeof ResizeObserver;
  });

  afterEach(() => {
    globalThis.ResizeObserver = OriginalResizeObserver;
    vi.restoreAllMocks();
  });

  it("re-reveals the selected tab when the slot resizes, not when only the tabs change", () => {
    const scrollIntoView = vi
      .spyOn(Element.prototype, "scrollIntoView")
      .mockImplementation(() => {});
    render(
      <WorkspaceTabsScroller>
        <div role="button" aria-current="false">
          first
        </div>
        <div role="button" aria-current="true">
          selected
        </div>
      </WorkspaceTabsScroller>,
    );
    const selected = screen.getByText("selected");
    const viewport = selected.closest("[data-workspace-tabs-viewport]")!;
    const content = viewport.firstElementChild!;
    const container = viewport.parentElement!;
    expect(observed).toEqual(expect.arrayContaining([container, viewport, content]));
    expect(callbacks).toHaveLength(1);
    scrollIntoView.mockClear();

    // Closing another tab or a label resolving only resizes the content; the
    // user's manual scroll position must survive that.
    callbacks[0]([{ target: content }]);
    expect(scrollIntoView).not.toHaveBeenCalled();

    // The rail (slot) changing width re-reveals the selected tab.
    callbacks[0]([{ target: container }]);
    expect(scrollIntoView).toHaveBeenCalledTimes(1);
    expect(scrollIntoView.mock.instances[0]).toBe(selected);

    // Arrows mounting or unmounting change the viewport's width too.
    callbacks[0]([{ target: viewport }, { target: content }]);
    expect(scrollIntoView).toHaveBeenCalledTimes(2);
  });
});
