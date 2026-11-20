import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { MessageResponse } from "@/components/ai-elements/message";
import { TooltipProvider } from "@/components/ui/tooltip";
import { CODE_BLOCK_WORD_WRAP_STORAGE_KEY } from "@/lib/codeWrapPreferences";
import { CodePanel, OutputSection } from "./ToolCodePanel";

afterEach(() => {
  cleanup();
  localStorage.removeItem(CODE_BLOCK_WORD_WRAP_STORAGE_KEY);
});

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

describe("CodePanel word wrap", () => {
  it("shares one wrap choice with chat code blocks", async () => {
    render(
      <TooltipProvider>
        <CodePanel copyLabel="Copy result" copyText="{}" text='{"ok": true}' title="Result" />
        <MessageResponse>{"```ts\nconst value = 1;\n```"}</MessageResponse>
      </TooltipProvider>,
    );
    const chatToggle = await screen.findByRole("button", { name: "Toggle word wrap" });
    expect(chatToggle).toHaveAttribute("aria-pressed", "true");

    fireEvent.click(screen.getByRole("button", { name: "Disable word wrap" }));
    expect(screen.getByRole("button", { name: "Enable word wrap" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    expect(chatToggle).toHaveAttribute("aria-pressed", "false");
    expect(localStorage.getItem(CODE_BLOCK_WORD_WRAP_STORAGE_KEY)).toBe("false");

    fireEvent.click(chatToggle);
    expect(screen.getByRole("button", { name: "Disable word wrap" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });
});
