import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { FileRowTooltip } from "./FileRowTooltip";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function renderTooltip() {
  return render(
    <TooltipProvider delayDuration={0}>
      <FileRowTooltip
        path="src/deep/app.ts"
        status="modified"
        linesAdded={18}
        linesRemoved={6}
        modifiedAt={1_700_000_000}
        bytes={2048}
      >
        <button type="button">app.ts</button>
      </FileRowTooltip>
    </TooltipProvider>,
  );
}

describe("FileRowTooltip", () => {
  it("opens on keyboard focus with path, status, diff, size, and edited time", async () => {
    vi.spyOn(Date.prototype, "toLocaleString").mockReturnValue("Nov 14, 10:13 PM");
    renderTooltip();

    const row = screen.getByRole("button", { name: "app.ts" });
    fireEvent.focus(row);

    const tooltip = await screen.findByRole("tooltip");
    expect(row).toHaveAttribute("aria-describedby", tooltip.id);
    expect(tooltip).toHaveTextContent("src/deep/app.ts");
    expect(tooltip).toHaveTextContent("Modified · +18 −6 · 2.0 KB");
    expect(tooltip).toHaveTextContent(/Edited .* · Nov 14, 10:13 PM/);
  });

  it("closes when its scroll container moves", async () => {
    renderTooltip();
    fireEvent.focus(screen.getByRole("button", { name: "app.ts" }));
    expect(await screen.findByRole("tooltip")).toBeInTheDocument();

    fireEvent.scroll(document);

    await waitFor(() => expect(screen.queryByRole("tooltip")).not.toBeInTheDocument());
  });
});
