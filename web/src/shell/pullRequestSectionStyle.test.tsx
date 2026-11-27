import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { useSectionPicker } from "./pullRequestSectionStyle";

function Section() {
  const { open, sectionProps } = useSectionPicker(true);
  return (
    <>
      <p>Text elsewhere on the page</p>
      <section {...sectionProps}>
        <h3>Labels</h3>
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
});
