import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useSectionPicker } from "./pullRequestSectionStyle";

let outside: ((event: { target: EventTarget | null; preventDefault: () => void }) => void) | null =
  null;

function Section({ enabled = true }: { enabled?: boolean }) {
  const { open, sectionProps, onPointerDownOutside } = useSectionPicker(enabled);
  outside = onPointerDownOutside;
  return (
    <>
      <p>Text elsewhere on the page</p>
      <section {...sectionProps}>
        <h3>Labels</h3>
        <button type="button">Remove bug</button>
        <span>{open ? "open" : "closed"}</span>
      </section>
    </>
  );
}

function select(node: Node) {
  const range = document.createRange();
  range.selectNodeContents(node);
  window.getSelection()?.removeAllRanges();
  window.getSelection()?.addRange(range);
}

afterEach(() => window.getSelection()?.removeAllRanges());

describe("useSectionPicker", () => {
  it("opens from a press on the section while text elsewhere stays selected", () => {
    render(<Section />);
    select(screen.getByText("Text elsewhere on the page"));
    act(() => fireEvent.click(screen.getByText("Labels")));
    expect(screen.getByText("open")).toBeTruthy();
  });

  it("leaves the picker alone when the press ends selecting the section's own text", () => {
    render(<Section />);
    select(screen.getByText("Labels"));
    act(() => fireEvent.click(screen.getByText("Labels")));
    expect(screen.getByText("closed")).toBeTruthy();
  });

  it("stays closed while it's off, such as for a read-only pull request", () => {
    render(<Section enabled={false} />);
    act(() => fireEvent.click(screen.getByText("Labels")));
    expect(screen.getByText("closed")).toBeTruthy();
  });

  it("leaves a press on a control inside the section to that control", () => {
    render(<Section />);
    act(() => fireEvent.click(screen.getByRole("button", { name: "Remove bug" })));
    expect(screen.getByText("closed")).toBeTruthy();
  });

  it("keeps an open picker open for a press on its section, and lets one elsewhere close it", () => {
    render(<Section />);
    const onSection = { target: screen.getByText("Labels"), preventDefault: vi.fn() };
    outside?.(onSection);
    expect(onSection.preventDefault).toHaveBeenCalled();
    const elsewhere = {
      target: screen.getByText("Text elsewhere on the page"),
      preventDefault: vi.fn(),
    };
    outside?.(elsewhere);
    expect(elsewhere.preventDefault).not.toHaveBeenCalled();
    const onControl = {
      target: screen.getByRole("button", { name: "Remove bug" }),
      preventDefault: vi.fn(),
    };
    outside?.(onControl);
    expect(onControl.preventDefault).not.toHaveBeenCalled();
  });
});
