import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { OutputSection } from "./ToolCodePanel";

afterEach(cleanup);

const lines = (count: number) =>
  Array.from({ length: count }, (_, n) => `build step ${n} ok`).join("\n");

describe("OutputSection", () => {
  it("stays expanded while the output grows", () => {
    const { rerender } = render(<OutputSection output={lines(100)} renderPanel={(text) => text} />);
    fireEvent.click(screen.getByRole("button", { name: "Expand" }));

    rerender(<OutputSection output={lines(120)} renderPanel={(text) => text} />);

    expect(screen.getByText(/Showing full output/)).toBeTruthy();
  });
});
