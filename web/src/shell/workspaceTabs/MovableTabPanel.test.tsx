import { render } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import { MovableTabPanel } from "./MovableTabPanel";

function Counter() {
  const [count] = useState(() => Math.random());
  return <span data-testid="content" data-count={count} />;
}

describe("MovableTabPanel", () => {
  it("moves its content between places without mounting it again", () => {
    const rail = document.body.appendChild(document.createElement("div"));
    const pane = document.body.appendChild(document.createElement("div"));
    const view = render(
      <MovableTabPanel target={rail}>
        <Counter />
      </MovableTabPanel>,
    );
    const content = rail.querySelector("[data-testid=content]");
    expect(content).toBeTruthy();
    const count = content!.getAttribute("data-count");

    view.rerender(
      <MovableTabPanel target={pane}>
        <Counter />
      </MovableTabPanel>,
    );
    expect(rail.querySelector("[data-testid=content]")).toBeNull();
    // The same element, with the same state: it moved rather than mounting again.
    expect(pane.querySelector("[data-testid=content]")).toBe(content);
    expect(content!.getAttribute("data-count")).toBe(count);

    view.unmount();
    expect(pane.childElementCount).toBe(0);
    rail.remove();
    pane.remove();
  });

  it("takes its content off the page while it has no place, and keeps its state", () => {
    const rail = document.body.appendChild(document.createElement("div"));
    const pane = document.body.appendChild(document.createElement("div"));
    const view = render(
      <MovableTabPanel target={rail}>
        <Counter />
      </MovableTabPanel>,
    );
    const content = rail.querySelector("[data-testid=content]")!;
    const count = content.getAttribute("data-count");
    view.rerender(
      <MovableTabPanel target={null}>
        <Counter />
      </MovableTabPanel>,
    );
    expect(document.querySelector("[data-testid=content]")).toBeNull();
    view.rerender(
      <MovableTabPanel target={pane}>
        <Counter />
      </MovableTabPanel>,
    );
    expect(pane.querySelector("[data-testid=content]")).toBe(content);
    expect(content.getAttribute("data-count")).toBe(count);
    view.unmount();
    rail.remove();
    pane.remove();
  });

  it("waits to render until it has a place", () => {
    const view = render(
      <MovableTabPanel target={null}>
        <Counter />
      </MovableTabPanel>,
    );
    expect(document.querySelector("[data-testid=content]")).toBeNull();
    view.unmount();
  });
});
