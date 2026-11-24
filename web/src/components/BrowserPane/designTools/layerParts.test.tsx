import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { DrawDock } from "./layerParts";

afterEach(cleanup);

const RECT = { left: 0, top: 0, width: 260, height: 400 };

describe("a design tool's bar", () => {
  it("keeps its x in view when the page is too narrow for the rest, which scrolls instead", () => {
    render(
      <DrawDock
        rect={RECT}
        kind="rect"
        frozen={false}
        undoDisabled
        sendDisabled={false}
        color={null}
        note={null}
        onClose={() => {}}
      />,
    );
    const close = screen.getByRole("button", { name: "Close Draw" });
    const send = screen.getByRole("button", { name: "Send to agent" });
    const contents = send.parentElement!;
    // The x sits beside the contents, not in them: they shrink and scroll, it doesn't.
    expect(contents).not.toBe(close.parentElement);
    expect(contents.parentElement).toBe(close.parentElement);
    expect(contents).toHaveClass("min-w-0", "overflow-x-auto");
    expect(close).toHaveClass("shrink-0");
  });
});
