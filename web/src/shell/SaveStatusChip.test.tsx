import { render, screen } from "@testing-library/react";
import { createRef } from "react";
import { describe, expect, it } from "vitest";

import { SaveStatusChip } from "./SaveStatusChip";

describe("SaveStatusChip", () => {
  it("hands its element to the toolbar's ref, so the overflow measure counts its width", () => {
    const chip = createRef<HTMLSpanElement>();
    render(<SaveStatusChip status="saving" chipRef={chip} />);
    expect(chip.current).toBe(screen.getByText("Saving…").closest("span[aria-live]"));
  });

  it("shows nothing while idle", () => {
    const { container } = render(<SaveStatusChip status="idle" />);
    expect(container).toBeEmptyDOMElement();
  });
});
