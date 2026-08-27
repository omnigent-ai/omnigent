import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

// Stub the real viewer: this suite is about the boundary in front of it, not
// the TipTap / ProseMirror stack it pulls in.
vi.mock("./FileViewer", () => ({
  FileViewer: ({ path, frameless }: { path: string; frameless?: boolean }) => (
    <div data-testid="file-viewer-stub" data-frameless={frameless ? "true" : "false"}>
      {path}
    </div>
  ),
}));

import { LazyFileViewer } from "./LazyFileViewer";

describe("LazyFileViewer", () => {
  it("shows the loading fallback until the viewer chunk resolves", async () => {
    render(<LazyFileViewer open conversationId="session-1" path="design.md" onClose={() => {}} />);

    // First paint happens before the dynamic import settles.
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    expect(screen.queryByTestId("file-viewer-stub")).not.toBeInTheDocument();

    expect(await screen.findByTestId("file-viewer-stub")).toBeInTheDocument();
    expect(screen.queryByText("Loading…")).not.toBeInTheDocument();
  });

  it("forwards its props to the loaded viewer", async () => {
    const onClose = vi.fn();
    render(
      <LazyFileViewer
        open
        frameless
        conversationId="session-1"
        path="src/deep/nested.ts"
        onClose={onClose}
      />,
    );

    const stub = await screen.findByTestId("file-viewer-stub");
    expect(stub).toHaveTextContent("src/deep/nested.ts");
    expect(stub).toHaveAttribute("data-frameless", "true");
  });
});
