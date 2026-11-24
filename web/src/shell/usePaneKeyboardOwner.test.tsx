import { act, cleanup, render } from "@testing-library/react";
import { useRef } from "react";
import { afterEach, describe, expect, it } from "vitest";

import { usePaneKeyboardOwner } from "./usePaneKeyboardOwner";

let read: () => string | null = () => null;

function Host({ panes }: { panes: readonly string[] }) {
  const hostRef = useRef<HTMLDivElement>(null);
  read = usePaneKeyboardOwner(hostRef);
  return (
    <div ref={hostRef}>
      {panes.map((key) => (
        <div key={key} data-terminal-pane={key}>
          <textarea aria-label={key} />
        </div>
      ))}
    </div>
  );
}

const input = (label: string) =>
  document.querySelector<HTMLTextAreaElement>(`textarea[aria-label="${label}"]`)!;

afterEach(cleanup);

describe("usePaneKeyboardOwner", () => {
  it("names the pane focus went into, and follows it between panes", () => {
    render(<Host panes={["a", "b"]} />);
    expect(read()).toBeNull();
    act(() => input("a").focus());
    expect(read()).toBe("a");
    act(() => input("b").focus());
    expect(read()).toBe("b");
  });

  it("still names a pane that went away with the keyboard in it", () => {
    const view = render(<Host panes={["a", "b"]} />);
    act(() => input("b").focus());
    view.rerender(<Host panes={["a"]} />);
    expect(document.activeElement).toBe(document.body);
    expect(read()).toBe("b");
  });

  it("names none once the keyboard is elsewhere in the page", () => {
    const outside = document.body.appendChild(document.createElement("input"));
    try {
      render(<Host panes={["a"]} />);
      act(() => input("a").focus());
      act(() => outside.focus());
      expect(read()).toBeNull();
    } finally {
      outside.remove();
    }
  });

  it("names none once the keyboard left the panes for nowhere, such as empty space", async () => {
    render(<Host panes={["a", "b"]} />);
    act(() => input("a").focus());
    await act(async () => {
      input("a").blur();
      await Promise.resolve();
    });
    expect(document.activeElement).toBe(document.body);
    expect(read()).toBeNull();
  });

  it("keeps the pane when the window loses focus with the keyboard in it", async () => {
    render(<Host panes={["a"]} />);
    act(() => input("a").focus());
    // The window blurring leaves the page's focus where it was.
    await act(async () => {
      input("a").dispatchEvent(new FocusEvent("focusout", { bubbles: true, relatedTarget: null }));
      await Promise.resolve();
    });
    expect(read()).toBe("a");
  });
});
