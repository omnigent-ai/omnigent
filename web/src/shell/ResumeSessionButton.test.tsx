import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { Toaster } from "@/components/ui/toast";
import { buildResumeCommand } from "@/lib/resumeCommand";
import { ResumeSessionButton } from "./ResumeSessionButton";

function renderButton(conversationId = "conv-abc123") {
  return render(
    <TooltipProvider>
      <ResumeSessionButton conversationId={conversationId} />
      <Toaster />
    </TooltipProvider>,
  );
}

describe("buildResumeCommand", () => {
  it("emits a single-line `omnigent resume` naming the id and server", () => {
    const command = buildResumeCommand({
      conversationId: "conv-abc123",
      serverUrl: "https://example.databricksapps.com",
    });
    expect(command).toBe("omnigent resume conv-abc123 --server https://example.databricksapps.com");
    expect(command).not.toContain("\n");
    expect(command).not.toContain("\\");
  });
});

describe("ResumeSessionButton", () => {
  let writeText: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    cleanup();
  });

  it("copies the resume command for the open session", async () => {
    renderButton("conv-abc123");

    screen.getByTestId("copy-resume-command").click();

    await waitFor(() => {
      expect(writeText).toHaveBeenCalledWith(
        `omnigent resume conv-abc123 --server ${window.location.origin}`,
      );
    });
  });

  it("confirms the copy in the label and a toast naming the command", async () => {
    renderButton("conv-abc123");

    const button = screen.getByRole("button", { name: "Copy resume command" });
    button.click();

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Resume command copied" })).toBeInTheDocument();
    });
    expect(screen.getByTestId("toast")).toHaveTextContent("Resume command copied");
    expect(screen.getByTestId("toast")).toHaveTextContent("omnigent resume conv-abc123");
  });

  it("reports a failed copy instead of claiming success", async () => {
    writeText.mockRejectedValue(new Error("denied"));
    vi.spyOn(console, "warn").mockImplementation(() => {});
    renderButton();

    screen.getByTestId("copy-resume-command").click();

    await waitFor(() => {
      expect(screen.getByTestId("toast")).toHaveTextContent("Couldn't copy the resume command");
    });
    expect(screen.queryByRole("button", { name: "Resume command copied" })).toBeNull();
  });
});
