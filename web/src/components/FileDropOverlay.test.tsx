import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { FileDropOverlay } from "./FileDropOverlay";

afterEach(cleanup);
it.each(["attach", "upload"] as const)(
  "shows decorative %s action copy and its distinct treatment",
  (variant) => {
    render(
      <FileDropOverlay
        container={document.body}
        variant={variant}
        label={variant === "upload" ? "Upload to workspace" : "Attach to message"}
        destination={variant === "upload" ? "assets" : undefined}
        subtext={
          variant === "upload" ? "Saved to this session's files" : "Sent with your next message"
        }
      />,
    );
    const overlay = screen.getByTestId("file-drop-overlay");
    expect(overlay.getAttribute("aria-hidden")).toBe("true");
    expect(
      overlay.querySelector(variant === "upload" ? ".lucide-upload" : ".lucide-paperclip"),
    ).toBeTruthy();
    if (variant === "upload") {
      expect(screen.getByText("to assets")).toBeTruthy();
      expect(overlay.textContent).not.toMatch(/attach/i);
      expect(overlay.className).toContain("border-info");
    } else {
      expect(screen.getByText("Sent with your next message")).toBeTruthy();
      expect(overlay.className).toContain("backdrop-blur");
    }
  },
);
